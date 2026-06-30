"""PTY-wrapping launcher with background DLB watcher.

Architecture:
    [user's real TTY]  ←→  [launcher process]  ←→  [PTY]  ←→  [child CLI]
                              ↑
                              └─ [DLB watcher thread]

The launcher is the parent of the child CLI; the child sees a normal TTY
(via pty.fork) and behaves exactly as it would running directly. The
launcher just acts as a transparent relay for IO in both directions, plus
injects synthetic wake prompts when the DLB watcher detects new mail AND
the child has been idle long enough that injection won't garble output.

This is the same mechanism `tmux send-keys` uses, packaged as a
single-purpose tool so it works without tmux.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import pty
import select
import signal
import sys
import termios
import threading
import time
import tty
from threading import Event, Thread

from . import wake, watcher

# How often the watcher polls the DLB SQLite store (seconds). 1.5s is
# below most users' perceived-latency threshold while keeping the CPU
# cost of a SELECT MAX(id) at one-per-1500ms, which is rounding-error.
POLL_INTERVAL_S = 1.5


def _set_window_size(fd: int) -> None:
    """Forward the user's terminal size to the child's PTY.

    Without this, the child renders into a default 80x24 box regardless
    of the user's actual terminal — breaks any TUI (Claude Code, Codex,
    Gemini all paint TUIs).
    """
    try:
        size = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
    except OSError:
        # Not a real TTY (e.g., piped) — child will fall back to defaults.
        pass


def _watcher_loop(
    name: str,
    master_fd: int,
    idle: wake.IdleTracker,
    stop: Event,
) -> None:
    """Background thread: poll DLB, inject wake prompts when safe.

    The watermark (`seen_max_id`) is initialized to the current max
    message id at startup — so messages that were already in the inbox
    BEFORE the launcher started don't trigger a wake (those are the LLM's
    SessionStart-hook problem, not the launcher's). We only fire on new
    arrivals while the launcher is running.
    """
    seen_max_id = watcher.max_message_id_for(name)
    _debug = os.environ.get("DLB_LAUNCHER_DEBUG") == "1"
    if _debug:
        sys.stderr.write(f"[dlb-launcher] watcher start, baseline id={seen_max_id}\n")
    while not stop.is_set():
        try:
            current_max = watcher.max_message_id_for(name)
            if _debug:
                sys.stderr.write(
                    f"[dlb-launcher] poll: current_max={current_max} seen={seen_max_id}\n"
                )
            if current_max > seen_max_id:
                # New messages — but only inject when child is idle, to
                # avoid splatting our prompt into the middle of streaming
                # output. We spin here (with short sleeps) until idle, then
                # inject. If the child stays busy for >30s we give up on
                # this batch and let the next poll re-detect.
                give_up_at = time.time() + 30.0
                while not stop.is_set() and time.time() < give_up_at:
                    if idle.is_idle():
                        count, senders = watcher.unread_summary_for(name)
                        if _debug:
                            sys.stderr.write(
                                f"[dlb-launcher] injecting wake: count={count} senders={senders}\n"
                            )
                        if count > 0:
                            prompt = wake.compose_wake_prompt(name, count, senders)
                            try:
                                os.write(master_fd, prompt.encode())
                                if _debug:
                                    sys.stderr.write(
                                        f"[dlb-launcher] wrote {len(prompt)} bytes to master\n"
                                    )
                            except OSError as e:
                                if _debug:
                                    sys.stderr.write(f"[dlb-launcher] write failed: {e}\n")
                                # Child PTY gone — parent IO loop will
                                # also notice; bail out.
                                return
                        seen_max_id = current_max
                        break
                    elif _debug:
                        sys.stderr.write("[dlb-launcher] not idle yet, waiting...\n")
                    time.sleep(0.2)
                else:
                    # Child stayed busy too long; advance watermark anyway
                    # so we don't keep re-checking the same batch forever.
                    # Next message arrival will re-trigger.
                    seen_max_id = current_max
        except Exception:
            # Watcher must never crash the launcher; swallow and continue.
            pass
        # Block on the stop event for the poll interval so shutdown is
        # responsive (vs. sleeping in a way the parent can't interrupt).
        stop.wait(POLL_INTERVAL_S)


def run(cli_argv: list[str], name: str | None) -> int:
    """Spawn `cli_argv` under a PTY; optionally wake on DLB mail.

    Returns the child's exit code.

    If `name` is None, no DLB watcher is started — the launcher degrades
    to a pure transparent PTY relay (the wake mechanism is opt-in per
    invocation via --name).
    """
    if not cli_argv:
        raise ValueError("cli_argv must not be empty")

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
    # interpreter; if the launcher is called from a worker thread
    # (e.g. a test harness or a supervising daemon), skip the dynamic
    # SIGWINCH handler and accept that resizes won't propagate. The
    # initial size still does.
    _set_window_size(master_fd)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGWINCH, lambda *_: _set_window_size(master_fd))

    # Put the real TTY into raw mode so keystrokes pass through unmodified
    # (no line editing, no Ctrl-C handling in our process — the child
    # gets the raw bytes and decides). Saved + restored in `finally`.
    stdin_was_tty = sys.stdin.isatty()
    old_tty = termios.tcgetattr(sys.stdin) if stdin_was_tty else None
    if stdin_was_tty:
        tty.setraw(sys.stdin.fileno())

    idle = wake.IdleTracker()
    stop = Event()
    watcher_thread: Thread | None = None
    if name:
        # Startup-surface: if there are pre-existing unread messages when the
        # launcher starts, inject ONE wake prompt now (then baseline). This
        # closes the gap between the SessionStart hook firing (which tells
        # the LLM "you have unread mail" if it's registered) and the
        # watcher's baseline (which would otherwise skip pre-existing mail
        # entirely). Without this, mail that arrived between hook-fire and
        # launcher-start is invisible to the launcher AND to any LLM that
        # wasn't registered at hook time.
        try:
            pre_count, pre_senders = watcher.unread_summary_for(name)
            if pre_count > 0:
                prompt = wake.compose_wake_prompt(name, pre_count, pre_senders)
                # PTY not ready yet? Should be — pty.fork() returned. Worst
                # case the LLM misses this one wake and the watcher picks up
                # the NEXT message normally.
                with contextlib.suppress(OSError):
                    os.write(master_fd, prompt.encode())
        except Exception:
            # A watcher hiccup at startup must not block the launcher.
            pass

        watcher_thread = Thread(
            target=_watcher_loop,
            args=(name, master_fd, idle, stop),
            daemon=True,
            name="dlb-watcher",
        )
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
                # Forward user keystrokes verbatim to child.
                try:
                    os.write(master_fd, data)
                except OSError:
                    break
                # User typed → child is about to produce output; reset
                # the idle clock so we don't inject during their turn.
                idle.mark_output()

            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 8192)
                except OSError:
                    # PTY closed — child exited.
                    break
                if not data:
                    break
                try:
                    os.write(sys.stdout.fileno(), data)
                except OSError:
                    break
                idle.mark_output()
    finally:
        stop.set()
        if stdin_was_tty and old_tty is not None:
            with contextlib.suppress(termios.error, OSError):
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty)
        # Close the PTY master — this orphans the slave end, sending
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
                # Still alive after 1s — escalate.
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
                _, exit_status = os.waitpid(pid, 0)
        except ChildProcessError:
            exit_status = 0
        if watcher_thread is not None:
            watcher_thread.join(timeout=1.0)
        # Stash the exit code in a function attribute so the caller can
        # pick it up after the `finally` runs — returning from `finally`
        # silences exceptions, which we don't want.
        run._last_exit_code = (  # type: ignore[attr-defined]
            os.waitstatus_to_exitcode(exit_status) if exit_status else 0
        )

    return run._last_exit_code  # type: ignore[attr-defined]


# Convenience signature for one-shot launch from CLI.
def main_entry(cli_argv: list[str], name: str | None) -> int:
    return run(cli_argv, name)
