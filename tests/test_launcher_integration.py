"""End-to-end integration: spawn a tiny echo-server child under the
launcher, post a DLB message, verify the wake prompt arrives in child
input.

We can't easily spawn `claude` in CI (no API key, would consume tokens),
so we use a one-line Python script as the child that echoes everything
it reads from stdin to stdout. The launcher's job is identical regardless
of what's inside the PTY — relay IO, watch DLB, inject when idle. If our
echo-child sees the synthetic 🔔 DLB-WAKE line on its stdin (which it
then echoes to stdout, which we observe), the injection mechanism works.

This is a real assertion of the wake path: PTY allocation → child exec →
DLB watermark detection → idle gating → bytes written to PTY master → bytes
visible at child stdin → echoed back through master to launcher.
"""

from __future__ import annotations

import contextlib
import os
import select
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from .conftest import insert_message

# A child that just echoes any line it reads. Tiny enough to inline.
ECHO_CHILD = (
    "import sys\n"
    "for line in iter(sys.stdin.readline, ''):\n"
    "    sys.stdout.write('CHILD-SAW: ' + line)\n"
    "    sys.stdout.flush()\n"
)

# The launcher runs in its own process. pty.fork() must happen in a
# single-threaded process: forking from a pytest worker thread (the old
# approach) is unsafe and Python 3.12+ warns about it. A subprocess also
# gives the launcher real stdin/stdout fds instead of swapped sys.std*
# objects, so nothing in the test process closes an fd twice.
LAUNCHER_DRIVER = (
    "import sys\n"
    "from dlb_launcher.launcher import run\n"
    "sys.exit(run(cli_argv=[sys.executable, '-u', '-c', sys.argv[1]], name='alpha') or 0)\n"
)


@contextlib.contextmanager
def launcher_process(store: Path) -> Iterator[subprocess.Popen[bytes]]:
    """Run the launcher wrapping ECHO_CHILD; yield the process.

    Read relayed child output from `proc.stdout`. On exit, stdin is closed
    (the launcher sees EOF and shuts down) and the process is reaped.
    """
    env = {**os.environ, "DLB_STORE": str(store)}
    proc = subprocess.Popen(
        [sys.executable, "-c", LAUNCHER_DRIVER, ECHO_CHILD],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        yield proc
    finally:
        assert proc.stdin is not None
        with contextlib.suppress(OSError):
            proc.stdin.close()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


def _assert_alive(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        raise AssertionError(f"Launcher exited early ({proc.returncode}): {err[-2000:]}")


def _read_for(fd: int, seconds: float) -> bytes:
    """Read everything available on `fd` for up to `seconds`."""
    deadline = time.time() + seconds
    buf = bytearray()
    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        rlist, _, _ = select.select([fd], [], [], min(0.2, remaining))
        if fd not in rlist:
            continue
        try:
            chunk = os.read(fd, 8192)
        except OSError:
            break
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def test_wake_injection_reaches_child_stdin(fake_dlb_store: Path) -> None:
    """The smoking-gun test: drop a message into the DLB store while the
    launcher is running, then observe the echo child relay our synthetic
    wake prompt back through stdout, proving the injection traveled
    launcher -> PTY master -> child stdin -> child stdout -> PTY master ->
    launcher -> captured pipe.
    """
    with launcher_process(fake_dlb_store) as proc:
        assert proc.stdout is not None
        out_fd = proc.stdout.fileno()
        # Let the child spin up and the watcher take its baseline snapshot.
        time.sleep(2.0)
        _assert_alive(proc)
        _ = _read_for(out_fd, 0.2)

        # Post AFTER the baseline so it counts as "new" (watcher poll path).
        # The startup-surface path is covered separately below.
        insert_message(fake_dlb_store, recipient="alpha", sender="bravo", body="ping")

        out = _read_for(out_fd, 6.0)
        assert b"DLB-WAKE" in out, "Expected wake prompt to be echoed by child; got " + repr(
            out[:200]
        )
        assert b"[alpha]" in out
        assert b"bravo" in out  # sender shown


def test_startup_surface_emits_existing_unread(fake_dlb_store: Path) -> None:
    """Mail that arrived BEFORE the launcher started must produce a wake
    prompt on launch (covers the gap between SessionStart hook fire and
    the watcher's initial baseline snapshot).
    """
    insert_message(fake_dlb_store, recipient="alpha", sender="early-bird", body="hello")
    with launcher_process(fake_dlb_store) as proc:
        assert proc.stdout is not None
        time.sleep(0.5)
        _assert_alive(proc)
        out = _read_for(proc.stdout.fileno(), 3.0)
        assert b"DLB-WAKE" in out, (
            "Expected startup-surface to wake on pre-existing mail; got " + repr(out[:200])
        )
        assert b"[alpha]" in out
        assert b"early-bird" in out


def test_startup_surface_silent_when_no_unread(fake_dlb_store: Path) -> None:
    """No pre-existing mail -> no startup-surface wake. Checked inside a
    window shorter than the watcher's 1.5s poll interval.
    """
    with launcher_process(fake_dlb_store) as proc:
        assert proc.stdout is not None
        time.sleep(0.5)
        _assert_alive(proc)
        out = _read_for(proc.stdout.fileno(), 1.0)
        assert b"DLB-WAKE" not in out, (
            "Did not expect a startup-surface wake when inbox is empty; got " + repr(out[:200])
        )


def test_no_watcher_without_name(fake_dlb_store: Path) -> None:
    """Without --name the launcher is a pure relay: pre-existing mail never
    produces a wake. Process isolation makes this safe to test now.
    """
    insert_message(fake_dlb_store, recipient="alpha", sender="early-bird", body="hello")
    env = {**os.environ, "DLB_STORE": str(fake_dlb_store)}
    driver = LAUNCHER_DRIVER.replace("name='alpha'", "name=None")
    proc = subprocess.Popen(
        [sys.executable, "-c", driver, ECHO_CHILD],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        assert proc.stdout is not None
        time.sleep(0.5)
        _assert_alive(proc)
        out = _read_for(proc.stdout.fileno(), 2.5)
        assert b"DLB-WAKE" not in out
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
