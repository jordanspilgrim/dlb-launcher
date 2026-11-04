"""End-to-end integration: run the real launcher over a real PTY.

We can't spawn `claude` in CI, so the child is a tiny Python script that
turns terminal ECHO off, prints CHILD-READY, then reports every line it
reads from stdin as `RECV<<line>>`. ECHO off matters: with it on, the
PTY line discipline echoes injected bytes straight back to the master,
so a test could "see" a wake that the child never read. A `RECV<<...>>`
line can only come from the child's own readline, so matching it proves
the full path: DLB store -> watcher -> idle gate -> PTY master -> child
stdin -> child stdout -> PTY master -> launcher stdout.

Hygiene: the launcher runs with a scrubbed environment (explicit PATH, a
temp HOME, DLB_STORE in tmp_path, warnings as errors), stderr is drained
on a thread so it can never fill and block the launcher, and every wait
polls against a deadline instead of sleeping a fixed time.
"""

from __future__ import annotations

import contextlib
import os
import re
import select
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import dlb_schema
from .conftest import insert_agent_v9, insert_message, insert_message_v9

ECHO_OFF_CHILD = (
    "import sys, termios\n"
    "a = termios.tcgetattr(0)\n"
    "a[3] &= ~termios.ECHO\n"
    "termios.tcsetattr(0, termios.TCSANOW, a)\n"
    "print('CHILD-READY', flush=True)\n"
    "for line in iter(sys.stdin.readline, ''):\n"
    "    print('RECV<<' + line.rstrip('\\r\\n') + '>>', flush=True)\n"
)

# The launcher runs in its own single-threaded process: pty.fork() from a
# pytest thread is unsafe, and a subprocess gives it real stdio fds.
LAUNCHER_DRIVER = (
    "import sys\n"
    "from dlb_launcher.launcher import run\n"
    "name = sys.argv[2] or None\n"
    "sid = sys.argv[3] or None\n"
    "argv = [sys.executable, '-u', '-c', sys.argv[1]]\n"
    "sys.exit(run(cli_argv=argv, name=name, session_id=sid) or 0)\n"
)

READY = re.compile(rb"CHILD-READY")
WAKE = re.compile(rb"RECV<<([^\r\n]*DLB-WAKE[^\r\n]*)>>")
DEADLINE_S = 15.0


@dataclass
class Launcher:
    proc: subprocess.Popen[bytes]
    out: bytearray = field(default_factory=bytearray)
    err: bytearray = field(default_factory=bytearray)
    pos: int = 0  # read cursor into `out` for read_until

    def _pump(self, timeout: float) -> bool:
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        rlist, _, _ = select.select([fd], [], [], timeout)
        if not rlist:
            return True
        try:
            chunk = os.read(fd, 8192)
        except OSError:
            return False
        if not chunk:
            return False
        self.out.extend(chunk)
        return True

    def _diag(self) -> str:
        return (
            f"\n--- stdout ---\n{bytes(self.out)[-2000:]!r}"
            f"\n--- stderr ---\n{bytes(self.err)[-2000:].decode(errors='replace')}"
        )

    def read_until(self, pattern: re.Pattern[bytes], timeout: float = DEADLINE_S) -> re.Match:
        deadline = time.monotonic() + timeout
        while True:
            m = pattern.search(self.out, self.pos)
            if m:
                self.pos = m.end()
                return m
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"timed out waiting for {pattern.pattern!r}{self._diag()}")
            if not self._pump(min(0.1, remaining)):
                raise AssertionError(
                    f"launcher closed stdout (rc={self.proc.poll()}) before "
                    f"{pattern.pattern!r}{self._diag()}"
                )

    def collect(self, seconds: float) -> bytes:
        """Everything that arrives in the next `seconds` past the cursor."""
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            if not self._pump(min(0.1, remaining)):
                break
        got = bytes(self.out[self.pos :])
        self.pos = len(self.out)
        return got

    def send(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()


def _env(tmp_path: Path, store: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": os.path.dirname(sys.executable) + os.pathsep + os.defpath,
        "HOME": str(home),
        "DLB_STORE": str(store),
        "DLB_LAUNCHER_LOG": str(tmp_path / "launcher.log"),
        "PYTHONWARNINGS": "error",
        "PYTHONIOENCODING": "utf-8",
        "TERM": "dumb",
    }


@contextlib.contextmanager
def launcher(
    tmp_path: Path, store: Path, *, name: str | None = "alpha", session_id: str | None = None
) -> Iterator[Launcher]:
    proc = subprocess.Popen(
        [sys.executable, "-c", LAUNCHER_DRIVER, ECHO_OFF_CHILD, name or "", session_id or ""],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(tmp_path, store),
        cwd=str(tmp_path),
    )
    lc = Launcher(proc)

    def drain() -> None:
        assert proc.stderr is not None
        for chunk in iter(lambda: proc.stderr.read1(4096), b""):
            lc.err.extend(chunk)

    drainer = threading.Thread(target=drain, daemon=True)
    drainer.start()
    try:
        lc.read_until(READY)
        yield lc
    finally:
        with contextlib.suppress(OSError):
            assert proc.stdin is not None
            proc.stdin.close()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
        drainer.join(timeout=5.0)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        # PYTHONWARNINGS=error: a warning in the launcher would surface here.
        assert b"Warning" not in lc.err, lc._diag()


def _wait_for(pred, timeout: float = DEADLINE_S) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, "condition not met before deadline"
        time.sleep(0.05)


def test_wake_injection_reaches_child_stdin(tmp_path: Path, fake_dlb_store: Path) -> None:
    with launcher(tmp_path, fake_dlb_store) as lc:
        insert_message(fake_dlb_store, recipient="alpha", sender="bravo", body="ping")
        line = lc.read_until(WAKE).group(1)
        assert b"[alpha]" in line
        assert b"bravo" in line


def test_startup_surface_emits_existing_unread(tmp_path: Path, fake_dlb_store: Path) -> None:
    """Mail unread before the launcher starts gets one wake on launch."""
    insert_message(fake_dlb_store, recipient="alpha", sender="early-bird", body="hello")
    with launcher(tmp_path, fake_dlb_store) as lc:
        line = lc.read_until(WAKE).group(1)
        assert b"[alpha]" in line and b"early-bird" in line
        # The watcher's baseline covers it: no second wake for the same mail.
        assert WAKE.search(lc.collect(2 * 1.5 + 0.5)) is None


def test_startup_surface_silent_when_no_unread(tmp_path: Path, fake_dlb_store: Path) -> None:
    with launcher(tmp_path, fake_dlb_store) as lc:
        assert WAKE.search(lc.collect(2.0)) is None


def test_no_watcher_without_name(tmp_path: Path, fake_dlb_store: Path) -> None:
    """Without a name the launcher is a pure relay: mail never wakes it."""
    insert_message(fake_dlb_store, recipient="alpha", sender="early-bird", body="hello")
    with launcher(tmp_path, fake_dlb_store, name=None) as lc:
        lc.send(b"typed by user\n")
        assert lc.read_until(re.compile(rb"RECV<<typed by user>>"))
        assert WAKE.search(lc.collect(2.0)) is None


def test_v9_work_only_wake_and_owned_heartbeat(tmp_path: Path, fake_dlb_store_v9: Path) -> None:
    """v9 store: cancelled mail and receipts never wake; a task does; the
    launcher stamps its own agents row's heartbeat (matched by session_id)."""
    store = fake_dlb_store_v9
    insert_agent_v9(store, "alpha", session_id="sess-1")
    with launcher(tmp_path, store, session_id="sess-1") as lc:
        _wait_for(lambda: dlb_schema.heartbeat_of(store, "alpha") is not None)
        insert_message_v9(store, recipient="alpha", sender="canceller", cancelled=True)
        insert_message_v9(store, recipient="alpha", sender="receipter", msg_type="receipt")
        assert WAKE.search(lc.collect(2 * 1.5 + 0.5)) is None
        insert_message_v9(store, recipient="alpha", sender="boss", msg_type="task")
        line = lc.read_until(WAKE).group(1)
        assert b"boss" in line and b"1 unread" in line
        assert b"canceller" not in line and b"receipter" not in line


def test_v9_heartbeat_skips_row_owned_by_another_session(
    tmp_path: Path, fake_dlb_store_v9: Path
) -> None:
    store = fake_dlb_store_v9
    insert_agent_v9(store, "alpha", session_id="someone-else")
    with launcher(tmp_path, store, session_id="sess-1") as lc:
        insert_message_v9(store, recipient="alpha", sender="boss")
        lc.read_until(WAKE)  # the loop is running, so the first beat has happened
    assert dlb_schema.heartbeat_of(store, "alpha") is None
    assert "belongs to another session" in (tmp_path / "launcher.log").read_text()
