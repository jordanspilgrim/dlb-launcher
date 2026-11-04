"""PTY-wrapping launcher with background DLB watcher.

Architecture:
    [user's real TTY]  <->  [launcher process]  <->  [PTY]  <->  [child CLI]
                              ^
                              +- [DLB watcher thread: WakeLoop]

The launcher is the parent of the child CLI; the child sees a normal TTY
(via pty.fork) and behaves exactly as it would running directly. The
launcher just acts as a transparent relay for IO in both directions, plus
injects synthetic wake prompts when the DLB watcher detects new mail AND
it is safe to type into the child: the child is idle and the user has no
half-typed input on the line.

This is the same mechanism `tmux send-keys` uses, packaged as a
single-purpose tool so it works without tmux.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import math
import os
import pty
import re
import select
import signal
import sys
import termios
import threading
import time
import tty
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread

from . import wake, watcher

# How often the watcher polls the DLB SQLite store (seconds). 1.5s is
# below most users' perceived-latency threshold while keeping the CPU
# cost of two indexed SELECTs at one-per-1500ms, which is rounding-error.
POLL_INTERVAL_S = 1.5

# First retry while a batch waits for a safe moment to inject. Each further
# retry doubles, up to PENDING_BACKOFF_MAX_S, so a long agent turn does not
# poll the store five times a second for minutes.
PENDING_RETRY_S = 0.2
PENDING_BACKOFF_MAX_S = 2.0

# A batch pending this long is delivered even if the idle tracker still
# says busy (a CLI that repaints a spinner or clock forever never goes
# quiet), with a warning. Never forced while the user has unsent input.
PENDING_CEILING_S = 60.0

# How often to stamp agents.monitor_heartbeat_ms. Well inside dlb-mcp's
# default 120s wake window, so the session never flickers to unwakeable.
HEARTBEAT_INTERVAL_S = 30.0

Clock = Callable[[], float]


def _log(msg: str, *, debug: bool = False) -> None:
    """Write a launcher diagnostic.

    Goes to the file named by DLB_LAUNCHER_LOG when set, else stderr (with
    CRLF, since the user's terminal is in raw mode). Debug lines are only
    written when DLB_LAUNCHER_DEBUG=1. Logging must never break the relay.
    """
    if debug and os.environ.get("DLB_LAUNCHER_DEBUG") != "1":
        return
    line = f"[dlb-launcher] {msg}"
    try:
        log_path = os.environ.get("DLB_LAUNCHER_LOG")
        if log_path:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        else:
            sys.stderr.write(line + "\r\n")
            sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _set_window_size(fd: int) -> None:
    """Forward the user's terminal size to the child's PTY.

    Without this, the child renders into a default 80x24 box regardless
    of the user's actual terminal, which breaks any TUI (Claude Code,
    Codex, Gemini all paint TUIs).
    """
    try:
        size = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
    except OSError:
        # Not a real TTY (e.g., piped); child will fall back to defaults.
        pass


class HeartbeatOwner:
    """Decides which agents row the launcher may stamp a heartbeat on.

    The name passed to --name is only a claim. Another session may hold
    it, or a stale row from an earlier run may still carry it, and stamping
    either would tell coordinators a dead or foreign session is wakeable.

    - With an expected session id (--session-id, or DLB_SESSION_ID in the
      launcher's env, which the child and its dlb-mcp server inherit), only
      a row whose session_id matches is ours.
    - Otherwise the launcher snapshots the row's fingerprint (session_id,
      token_hash) at startup and binds to the first DIFFERENT fingerprint
      it sees afterwards, i.e. the registration the wrapped child made.
      register (and force-takeover) always mints a new token, so a row
      registered before the launcher started is never mistaken for ours.

    Once bound, `record_heartbeat` matches on that identity inside its
    UPDATE; if someone else takes the name over, the stamp is skipped.
    """

    def __init__(
        self, name: str, *, expected_session_id: str | None = None, db: Path | None = None
    ) -> None:
        self.name = name
        self.db = db
        self.expected_session_id = expected_session_id or None
        self._bound: tuple[str | None, str | None] | None = None
        self._start_fp: tuple[str | None, str | None] | None = None
        if self.expected_session_id is None:
            row = watcher.agent_row(name, db=db)
            if row is not None:
                self._start_fp = (row.session_id, row.token_hash)

    def credentials(self) -> tuple[str | None, str | None] | None:
        """(session_id, token_hash) to match on, or None if not ours (yet)."""
        if self.expected_session_id is not None:
            return (self.expected_session_id, None)
        if self._bound is not None:
            return self._bound
        row = watcher.agent_row(self.name, db=self.db)
        if row is None:
            return None
        fp = (row.session_id, row.token_hash)
        if fp == self._start_fp or fp == (None, None):
            return None
        self._bound = (row.session_id, None) if row.session_id else (None, row.token_hash)
        return self._bound


class WakeLoop:
    """The watcher thread's logic, one `step()` per poll.

    `step()` does one unit of work and returns how long to wait before the
    next one; `run()` loops it on a stop event. Clock, writer, heartbeat
    function and store are injected, so tests drive it deterministically.

    Delivery rules:

    - The watermark starts at `baseline`, which `run()` takes BEFORE the
      child is spawned, in the same read as the startup surface. Anything
      committed after that read is above the watermark and wakes.
    - A batch is delivered only when the user's input line is clean AND the
      child is idle. While it waits, retries back off from PENDING_RETRY_S
      to PENDING_BACKOFF_MAX_S. After PENDING_CEILING_S of waiting it is
      delivered anyway if the line is clean (with a warning); with unsent
      user input it waits indefinitely (warned once), because injecting
      would submit the user's half-typed text with our prompt.
    - The watermark advances only past rows the delivering poll saw.
    - Heartbeat: attempted on the first step, then every
      HEARTBEAT_INTERVAL_S once stamped. Until the first stamp succeeds
      (the child has not registered yet) it is retried every poll.
    """

    def __init__(
        self,
        name: str,
        *,
        write: Callable[[bytes], object],
        idle: wake.IdleTracker,
        line: wake.InputLineTracker,
        baseline: int,
        owner: HeartbeatOwner,
        clock: Clock = time.monotonic,
        heartbeat: Callable[..., str] = watcher.record_heartbeat,
        db: Path | None = None,
    ) -> None:
        self.name = name
        self.write = write
        self.idle = idle
        self.line = line
        self.watermark = baseline
        self.owner = owner
        self.clock = clock
        self.heartbeat = heartbeat
        self.db = db
        self.closed = False
        self._next_hb = clock()  # first step stamps immediately
        self._hb_warned: set[str] = set()
        self._pending_since: float | None = None
        self._retry = PENDING_RETRY_S
        self._dirty_warned = False

    # -- heartbeat ---------------------------------------------------------

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._hb_warned:
            self._hb_warned.add(key)
            _log(msg)

    def _beat(self, now: float) -> None:
        creds = self.owner.credentials()
        if creds is None:
            # The child has not registered this name yet (or only an older
            # registration exists). Retry next poll; never stamp blind.
            self._next_hb = now + POLL_INTERVAL_S
            return
        session_id, token_hash = creds
        result = self.heartbeat(self.name, db=self.db, session_id=session_id, token_hash=token_hash)
        if result == watcher.HB_STAMPED:
            self._next_hb = now + HEARTBEAT_INTERVAL_S
            self._hb_warned.discard(watcher.HB_NOT_OWNER)
        elif result == watcher.HB_NOT_OWNER:
            self._warn_once(
                watcher.HB_NOT_OWNER,
                f"heartbeat skipped: agents row '{self.name}' belongs to another session",
            )
            self._next_hb = now + HEARTBEAT_INTERVAL_S
        elif result == watcher.HB_UNSUPPORTED:
            _log("store predates monitor_heartbeat_ms; heartbeat disabled", debug=True)
            self._next_hb = math.inf
        else:
            self._next_hb = now + POLL_INTERVAL_S

    # -- delivery ----------------------------------------------------------

    def _reset_pending(self) -> None:
        self._pending_since = None
        self._retry = PENDING_RETRY_S
        self._dirty_warned = False

    def _capped(self, wait: float, now: float) -> float:
        return max(0.0, min(wait, self._next_hb - now))

    def step(self) -> float:
        now = self.clock()
        if now >= self._next_hb:
            self._beat(now)
        snap = watcher.poll(self.name, self.watermark, db=self.db)
        if not snap.new_ids:
            self._reset_pending()
            return self._capped(POLL_INTERVAL_S, now)
        if not snap.unread:
            # New work arrived but was read (or went non-work) before we
            # could wake; those rows are handled.
            self.watermark = snap.handled_up_to(self.watermark)
            self._reset_pending()
            return self._capped(POLL_INTERVAL_S, now)

        if self._pending_since is None:
            self._pending_since = now
        waited = now - self._pending_since
        forced = False
        delivered = False
        # Hold the line lock across check + write so a keystroke cannot land
        # between "line is clean" and our prompt.
        with self.line.lock:
            if self.line.dirty:
                if waited >= PENDING_CEILING_S and not self._dirty_warned:
                    self._dirty_warned = True
                    _log(
                        f"wake for '{self.name}' pending {waited:.0f}s: "
                        "user has unsent input, waiting for it to be submitted or cleared"
                    )
            elif (child_idle := self.idle.is_idle()) or waited >= PENDING_CEILING_S:
                forced = not child_idle
                prompt = wake.compose_wake_prompt(self.name, len(snap.unread), snap.senders)
                try:
                    self.write(prompt.encode())
                except OSError as e:
                    _log(f"wake write failed, stopping watcher: {e}", debug=True)
                    self.closed = True
                    return POLL_INTERVAL_S
                delivered = True
        if delivered:
            if forced:
                _log(
                    f"wake for '{self.name}' forced after {waited:.0f}s: "
                    "child never looked idle (constant output?)"
                )
            _log(f"injected wake: {len(snap.unread)} unread", debug=True)
            self.watermark = snap.handled_up_to(self.watermark)
            self._reset_pending()
            return self._capped(POLL_INTERVAL_S, now)
        wait = self._retry
        self._retry = min(self._retry * 2, PENDING_BACKOFF_MAX_S)
        return self._capped(wait, now)

    def run(self, stop: Event, wait: Callable[[float], object] | None = None) -> None:
        do_wait = wait or stop.wait
        while not stop.is_set() and not self.closed:
            try:
                delay = self.step()
            except Exception as e:  # the watcher must never crash the launcher
                _log(f"watcher step failed: {e!r}", debug=True)
                delay = POLL_INTERVAL_S
            do_wait(delay)


def run(
    cli_argv: list[str],
    name: str | None,
    *,
    session_id: str | None = None,
    prompt_marker: str | None = None,
) -> int:
    """Spawn `cli_argv` under a PTY; optionally wake on DLB mail.

    Returns the child's exit code.

    If `name` is None, no DLB watcher is started: the launcher degrades
    to a pure transparent PTY relay (the wake mechanism is opt-in per
    invocation via --name).
    """
    if not cli_argv:
        raise ValueError("cli_argv must not be empty")

    # Take the startup snapshot BEFORE the child exists. It feeds both the
    # startup-surface prompt and the watcher's baseline, so no message can
    # fall between the two: anything committed after this read is above the
    # baseline and the watcher wakes on it.
    startup: watcher.Poll | None = None
    owner: HeartbeatOwner | None = None
    if name:
        try:
            startup = watcher.poll(name, 0)
        except Exception:
            startup = watcher.Poll()
        owner = HeartbeatOwner(
            name, expected_session_id=session_id or os.environ.get("DLB_SESSION_ID")
        )

    # Spawn child on a PTY. After this point, in the child, fd 0/1/2 are
    # the slave; in the parent, `master_fd` is the master end.
    pid, master_fd = pty.fork()
    if pid == 0:
        # Child: replace with the target CLI. exec keeps the inherited
        # TTY (the slave) as stdin/stdout/stderr.
        try:
            os.execvp(cli_argv[0], cli_argv)
        except FileNotFoundError:
            sys.stderr.write(f"dlb-launcher: command not found: {cli_argv[0]}\n")
            os._exit(127)
        except OSError as e:
            sys.stderr.write(f"dlb-launcher: exec failed: {e}\n")
            os._exit(126)

    # Parent: relay IO. First, propagate terminal size and forward future
    # window-resize signals so the child's PTY tracks the real terminal.
    # signal.signal() only works from the main thread of the main
    # interpreter; elsewhere, skip the dynamic SIGWINCH handler.
    _set_window_size(master_fd)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGWINCH, lambda *_: _set_window_size(master_fd))

    # Put the real TTY into raw mode so keystrokes pass through unmodified.
    stdin_was_tty = sys.stdin.isatty()
    old_tty = termios.tcgetattr(sys.stdin) if stdin_was_tty else None
    if stdin_was_tty:
        tty.setraw(sys.stdin.fileno())

    marker = re.compile(prompt_marker.encode()) if prompt_marker else None
    idle = wake.IdleTracker(prompt_marker=marker)
    line = wake.InputLineTracker()
    stop = Event()
    watcher_thread: Thread | None = None
    if name and startup is not None and owner is not None:
        # Startup surface: mail already unread when the launcher started
        # gets ONE wake prompt now. The line is clean (the user has not
        # typed into the new child yet).
        if startup.unread:
            prompt = wake.compose_wake_prompt(name, len(startup.unread), startup.senders)
            with contextlib.suppress(OSError):
                os.write(master_fd, prompt.encode())
        loop = WakeLoop(
            name,
            write=lambda b: os.write(master_fd, b),
            idle=idle,
            line=line,
            baseline=startup.handled_up_to(0),
            owner=owner,
        )
        _log(f"watcher start, baseline id={loop.watermark}", debug=True)
        watcher_thread = Thread(target=loop.run, args=(stop,), daemon=True, name="dlb-watcher")
        watcher_thread.start()

    try:
        while True:
            try:
                rlist, _, _ = select.select([sys.stdin, master_fd], [], [])
            except OSError as e:
                if e.errno == errno.EINTR:
                    continue
                raise

            if sys.stdin in rlist:
                try:
                    data = os.read(sys.stdin.fileno(), 8192)
                except OSError:
                    data = b""
                if not data:
                    break
                # Forward user keystrokes verbatim to the child, recording
                # them under the line lock so a wake cannot interleave.
                try:
                    with line.lock:
                        os.write(master_fd, data)
                        line.feed(data)
                except OSError:
                    break
                # User typed: the child is about to produce output; reset
                # the idle clock so we don't inject during their turn.
                idle.mark_output()

            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 8192)
                except OSError:
                    # PTY closed: child exited.
                    break
                if not data:
                    break
                try:
                    os.write(sys.stdout.fileno(), data)
                except OSError:
                    break
                idle.mark_output(data)
    finally:
        stop.set()
        if stdin_was_tty and old_tty is not None:
            with contextlib.suppress(termios.error, OSError):
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty)
        # Close the PTY master: this orphans the slave end, sending
        # SIGHUP to the child so it tears down cleanly even if it was
        # blocked in a read.
        with contextlib.suppress(OSError):
            os.close(master_fd)
        # Reap the child. Try graceful wait; force-kill if it doesn't
        # exit within ~1s of master close.
        exit_status = 0
        try:
            for _ in range(5):
                pid_done, exit_status = os.waitpid(pid, os.WNOHANG)
                if pid_done != 0:
                    break
                time.sleep(0.2)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
                _, exit_status = os.waitpid(pid, 0)
        except ChildProcessError:
            exit_status = 0
        if watcher_thread is not None:
            watcher_thread.join(timeout=1.0)
        # Stash the exit code in a function attribute so the caller can
        # pick it up after the `finally` runs; returning from `finally`
        # silences exceptions, which we don't want.
        run._last_exit_code = (  # type: ignore[attr-defined]
            os.waitstatus_to_exitcode(exit_status) if exit_status else 0
        )

    return run._last_exit_code  # type: ignore[attr-defined]


# Convenience signature for one-shot launch from CLI.
def main_entry(
    cli_argv: list[str],
    name: str | None,
    *,
    session_id: str | None = None,
    prompt_marker: str | None = None,
) -> int:
    return run(cli_argv, name, session_id=session_id, prompt_marker=prompt_marker)
