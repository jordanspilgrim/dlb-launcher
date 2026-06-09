"""Wake-prompt composition + idle gating.

Two pure functions and one stateful tracker. Kept here (separate from
launcher.py) so the synthetic-prompt format is easy to tune in one place
and the gating logic is unit-testable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class IdleTracker:
    """Tracks "time since the wrapped CLI last produced output."

    The launcher updates `last_output_time` on every byte read from the
    PTY master. A wake prompt is only safe to inject when the child has
    been silent for `idle_threshold_s` — otherwise we risk interleaving
    our injection with mid-turn output (visual mess + possible confusion
    of streaming parsers in some clients).

    The threshold is generous (1s default) because the cost of waiting an
    extra second to inject is invisible; the cost of injecting mid-stream
    is a corrupt-looking transcript.

    `last_output_time` defaults to construction time so a child that
    produces no output at all is still considered idle after the
    threshold elapses (otherwise we'd wait forever to inject into a
    quiet CLI).
    """

    idle_threshold_s: float = 1.0
    last_output_time: float = field(default_factory=time.time)

    def mark_output(self) -> None:
        self.last_output_time = time.time()

    def is_idle(self) -> bool:
        return (time.time() - self.last_output_time) >= self.idle_threshold_s


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
