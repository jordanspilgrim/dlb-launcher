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
import sys
import time
from pathlib import Path
from threading import Thread

import pytest

from dlb_launcher.launcher import run

from .conftest import insert_message

# A child that just echoes any line it reads. Tiny enough to inline.
ECHO_CHILD = (
    "import sys\n"
    "for line in iter(sys.stdin.readline, ''):\n"
    "    sys.stdout.write('CHILD-SAW: ' + line)\n"
    "    sys.stdout.flush()\n"
)


@pytest.fixture()
def launcher_in_thread(fake_dlb_store: Path):
    """Run the launcher in a background thread, with stdin/stdout swapped
    for a captured pipe pair so the test can read the relayed child output.

    Returns (output_reader_fd, stop_callable).
    """
    # The launcher reads from sys.stdin and writes to sys.stdout. To
    # capture what the child echoes (which the launcher writes to
    # sys.stdout), redirect sys.stdout to a pipe we can read from.
    captured_r, captured_w = os.pipe()
    # We don't drive input in this test — the child only ever sees what
    # the watcher injects. Provide a stdin that's open but never readable.
    stdin_r, stdin_w = os.pipe()

    saved_stdin = sys.stdin
    saved_stdout = sys.stdout

    sys.stdin = os.fdopen(stdin_r, "r")
    sys.stdout = os.fdopen(captured_w, "w", buffering=1)

    exit_code: list[int] = []

    errors: list[BaseException] = []

    def target():
        try:
            code = run(
                cli_argv=[sys.executable, "-u", "-c", ECHO_CHILD],
                name="alpha",
            )
            exit_code.append(code)
        except BaseException as e:  # noqa: BLE001 — bubble up via list
            errors.append(e)
        finally:
            with contextlib.suppress(OSError):
                os.close(captured_w)

    t = Thread(target=target, daemon=True)
    t.start()

    def stop():
        # Close the synthetic stdin so the launcher's IO loop notices EOF
        # and exits cleanly; then restore the real stdio.
        with contextlib.suppress(OSError):
            os.close(stdin_w)
        t.join(timeout=3.0)
        sys.stdin = saved_stdin
        sys.stdout = saved_stdout
        with contextlib.suppress(OSError):
            os.close(captured_r)

    yield captured_r, stop, exit_code, errors

    # Always tear down even if the test raises.
    with contextlib.suppress(OSError):
        os.close(stdin_w)
    sys.stdin = saved_stdin
    sys.stdout = saved_stdout
    with contextlib.suppress(OSError):
        os.close(captured_r)


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


def test_wake_injection_reaches_child_stdin(
    fake_dlb_store: Path,
    launcher_in_thread,
) -> None:
    """The smoking-gun test: drop a message into the DLB store while the
    launcher is running, then observe the echo child relay our synthetic
    wake prompt back through stdout — proving the injection traveled
    launcher → PTY master → child stdin → child stdout → PTY master →
    launcher → captured pipe.
    """
    captured_r, stop, exit_code, errors = launcher_in_thread
    try:
        # Surface any launcher crash before asserting on output
        time.sleep(0.5)
        if errors:
            raise AssertionError(f"Launcher thread crashed: {errors[0]!r}") from errors[0]
        # Give the child a moment to spin up, then sit idle.
        time.sleep(1.5)
        # Drain any initial output (the echo child has none, but cover
        # the general case).
        _ = _read_for(captured_r, 0.2)

        # Post the message AFTER the launcher's initial watermark snapshot
        # so it counts as "new" — pre-existing messages are deliberately
        # ignored by the watcher (those are the LLM's SessionStart problem).
        insert_message(fake_dlb_store, recipient="alpha", sender="bravo", body="ping")

        # The watcher polls every 1.5s; the idle gate is 1s; allow a
        # generous window for both to elapse, then read what the echo
        # child relayed.
        out = _read_for(captured_r, 6.0)
        assert b"DLB-WAKE" in out, "Expected wake prompt to be echoed by child; got " + repr(
            out[:200]
        )
        assert b"[alpha]" in out
        assert b"bravo" in out  # sender shown
    finally:
        stop()


# Note: a second integration test ("no watcher when --name is omitted")
# was authored but caused fork/PTY state leakage that destabilized the
# happy-path test above. The no-name case is structurally guaranteed by
# launcher.py (the watcher thread is only spawned inside `if name:`); a
# unit test on the launcher would need process isolation (pytest-forked
# or similar). Skipping for v1 — covered by code inspection.
