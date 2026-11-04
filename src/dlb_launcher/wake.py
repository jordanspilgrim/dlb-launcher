"""Wake-prompt composition, idle gating, and unsent-input tracking.

Pure functions and small stateful trackers. Kept here (separate from
launcher.py) so the synthetic-prompt format is easy to tune in one place
and the gating logic is unit-testable with a fake clock.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

Clock = Callable[[], float]

# CSI / OSC / two-byte escape sequences. Stripped before prompt-marker
# matching so a TUI's colour codes don't hide its prompt.
_ANSI_RE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

# How much recent child output to keep for prompt-marker matching.
_TAIL_BYTES = 512


@dataclass
class IdleTracker:
    """Decides whether the wrapped CLI is idle enough to take a wake prompt.

    The child counts as idle when EITHER:

    - it has produced no output for `idle_threshold_s`, OR
    - `prompt_marker` (a bytes regex) matches the end of its recent output
      (ANSI codes stripped) and it has been quiet for `marker_quiet_s`.
      This lets a CLI that repaints a status line or spinner while it waits
      for input still be recognised as sitting at its prompt.

    `last_output_time` defaults to construction time, so a child that
    prints nothing at all still becomes idle once the threshold elapses.

    `clock` is injectable so tests can drive time deterministically.
    """

    idle_threshold_s: float = 1.0
    prompt_marker: re.Pattern[bytes] | None = None
    marker_quiet_s: float = 0.2
    clock: Clock = time.monotonic
    last_output_time: float = field(default=-1.0)
    _tail: bytes = field(default=b"", repr=False)
    _marker_seen: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.last_output_time < 0:
            self.last_output_time = self.clock()

    def mark_output(self, data: bytes = b"") -> None:
        self.last_output_time = self.clock()
        if self.prompt_marker is not None and data:
            self._tail = (self._tail + data)[-_TAIL_BYTES:]
            clean = _ANSI_RE.sub(b"", self._tail).rstrip(b"\r\n")
            self._marker_seen = self.prompt_marker.search(clean) is not None

    def quiet_for(self) -> float:
        return self.clock() - self.last_output_time

    def at_prompt(self) -> bool:
        return self._marker_seen

    def is_idle(self) -> bool:
        quiet = self.quiet_for()
        if quiet >= self.idle_threshold_s:
            return True
        return self._marker_seen and quiet >= self.marker_quiet_s


class InputLineTracker:
    """Tracks whether the user has typed something they have not submitted.

    Fed every byte the user sends to the child. A wake prompt injected
    while the line is dirty would be glued onto the user's half-typed text
    and submitted with it, so the launcher defers the wake until the line
    is empty again. Deferring is the safe option: clearing and restoring the
    user's line would need to know each CLI's line-editing keys, and a wrong
    guess destroys what they typed.

    Rules (conservative: when unsure, the line counts as dirty):

    - a printable character (or the lead byte of a UTF-8 sequence) adds one
    - Backspace / DEL removes one, never below zero
    - Enter (CR or LF), Ctrl-C and Ctrl-U clear the line
    - escape sequences (arrow keys, function keys, Alt+key) are ignored
    - inside a bracketed paste every character counts and newlines do NOT
      submit, matching how TUIs treat a paste

    `lock` is also held by the launcher while it forwards keystrokes and
    while it injects a wake, so "line is clean, inject" cannot race a
    keystroke landing in between.
    """

    _CLEARS = frozenset({0x0D, 0x0A, 0x03, 0x15})  # CR, LF, Ctrl-C, Ctrl-U
    _ERASES = frozenset({0x7F, 0x08})  # DEL, Backspace
    _PASTE_START = b"\x1b[200~"
    _PASTE_END = b"\x1b[201~"

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._pending = 0
        self._in_paste = False
        self._esc = bytearray()  # partial escape sequence carried across reads

    @property
    def dirty(self) -> bool:
        with self.lock:
            return self._pending > 0

    def feed(self, data: bytes) -> None:
        with self.lock:
            for b in data:
                self._feed_byte(b)
            # A lone ESC at the end of a read is the Escape key itself (real
            # escape sequences arrive in one read). Drop it so the next key
            # is not swallowed as Alt+key.
            if bytes(self._esc) == b"\x1b":
                self._esc.clear()

    def _feed_byte(self, b: int) -> None:
        if self._esc:
            self._esc.append(b)
            seq = bytes(self._esc)
            if seq == self._PASTE_START:
                self._in_paste = True
                self._esc.clear()
            elif seq == self._PASTE_END:
                self._in_paste = False
                self._esc.clear()
            elif self._PASTE_START.startswith(seq) or self._PASTE_END.startswith(seq):
                pass  # might still become a paste marker
            elif len(seq) == 2 and b not in (ord("["), ord("O")):
                self._esc.clear()  # two-byte escape (Alt+key): ignored
            elif (len(seq) > 2 and 0x40 <= b <= 0x7E) or len(seq) > 32:
                self._esc.clear()  # end of CSI/SS3 sequence (or runaway): ignored
            return
        if b == 0x1B:
            self._esc.append(b)
            return
        if 0x80 <= b <= 0xBF:
            return  # UTF-8 continuation byte: its lead byte already counted
        if self._in_paste:
            self._pending += 1
        elif b in self._CLEARS:
            self._pending = 0
        elif b in self._ERASES:
            self._pending = max(0, self._pending - 1)
        elif b >= 0x20:
            self._pending += 1


def compose_wake_prompt(name: str, unread_count: int, senders: list[str]) -> str:
    """Build the synthetic prompt text injected into the child's PTY.

    Design notes:
    - The 🔔 marker is intended for the LLM to recognize as a launcher
      wake (not a normal user message) and respond by calling
      `mcp__dlb__read`. The user's project CLAUDE.md / AGENTS.md /
      GEMINI.md should describe this protocol.
    - Senders are deduplicated and capped at 5 to keep the prompt short.
    - The trailing newline submits the prompt; without it the user would
      have to press Enter themselves.
    """
    unique = list(dict.fromkeys(senders))[:5]  # preserve order, drop dups, cap
    sender_str = ", ".join(unique)
    if len(set(senders)) > len(unique):
        sender_str += f" (+{len(set(senders)) - len(unique)} more)"
    return (
        f"🔔 DLB-WAKE [{name}]: {unread_count} unread "
        f"from {sender_str}. Call mcp__dlb__read with name='{name}' "
        f"and your session_token to fetch them, then surface to the user.\n"
    )
