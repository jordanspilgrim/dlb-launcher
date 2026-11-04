"""Deterministic tests for the watcher thread's logic (`launcher.WakeLoop`).

Every test drives `WakeLoop.step()` with a fake clock: the driver advances
time by exactly the wait `step()` returns, so retry, ceiling and heartbeat
timings are asserted to the instant, with no sleeps and no threads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dlb_launcher import launcher, wake, watcher

from . import dlb_schema
from .conftest import insert_agent_v9, insert_message_v9


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class StubIdle:
    """Stands in for IdleTracker: idle exactly when the test says so."""

    def __init__(self, idle: bool = False) -> None:
        self.idle = idle

    def is_idle(self) -> bool:
        return self.idle


class SpyHeartbeat:
    def __init__(self, clock: FakeClock, result: str = watcher.HB_STAMPED) -> None:
        self.clock = clock
        self.result = result
        self.calls: list[tuple[float, str | None, str | None]] = []

    def __call__(self, name: str, *, db, session_id, token_hash) -> str:  # noqa: ANN001
        self.calls.append((self.clock.t, session_id, token_hash))
        return self.result


class Harness:
    def __init__(
        self,
        store: Path,
        *,
        idle: bool = False,
        baseline: int = 0,
        heartbeat=None,  # noqa: ANN001
        owner: launcher.HeartbeatOwner | None = None,
    ) -> None:
        self.store = store
        self.clock = FakeClock()
        self.idle = StubIdle(idle)
        self.line = wake.InputLineTracker()
        self.writes: list[tuple[float, bytes]] = []
        self.steps: list[float] = []
        self.spy = SpyHeartbeat(self.clock, watcher.HB_UNSUPPORTED)
        self.loop = launcher.WakeLoop(
            "w",
            write=lambda b: self.writes.append((self.clock.t, b)),
            idle=self.idle,  # type: ignore[arg-type]
            line=self.line,
            baseline=baseline,
            owner=owner or launcher.HeartbeatOwner("w", expected_session_id="s1", db=store),
            clock=self.clock,
            heartbeat=heartbeat or self.spy,
            db=store,
        )

    def step(self) -> float:
        self.steps.append(self.clock.t)
        wait = self.loop.step()
        self.clock.t += wait
        return wait

    def run_until(self, t: float) -> None:
        while self.clock.t < t:
            self.step()

    def run_until_written(self, limit: float = 1000.0) -> float:
        while not self.writes:
            assert self.clock.t < limit, "never delivered"
            self.step()
        return self.writes[0][0]


@pytest.fixture()
def log_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "launcher.log"
    monkeypatch.setenv("DLB_LAUNCHER_LOG", str(path))
    monkeypatch.delenv("DLB_LAUNCHER_DEBUG", raising=False)
    return path


def _log_text(path: Path) -> str:
    return path.read_text() if path.exists() else ""


# -- pending retry / backoff (finding 2, 9) ---------------------------------


def test_retry_fires_at_pending_retry_and_not_before(fake_dlb_store_v9: Path) -> None:
    h = Harness(fake_dlb_store_v9)
    insert_message_v9(fake_dlb_store_v9, recipient="w", sender="boss", msg_type="task")
    first_wait = h.step()
    assert first_wait == pytest.approx(launcher.PENDING_RETRY_S)
    assert h.writes == []
    # The child goes idle just before the retry is due; nothing re-polls
    # earlier, and the retry at exactly PENDING_RETRY_S delivers.
    h.idle.idle = True
    h.step()
    assert len(h.writes) == 1
    assert h.writes[0][0] == pytest.approx(launcher.PENDING_RETRY_S)
    assert b"boss" in h.writes[0][1]


def test_pending_retries_back_off_to_cap(fake_dlb_store_v9: Path) -> None:
    h = Harness(fake_dlb_store_v9)
    insert_message_v9(fake_dlb_store_v9, recipient="w", sender="boss")
    waits = [h.step() for _ in range(7)]
    assert waits == pytest.approx([0.2, 0.4, 0.8, 1.6, 2.0, 2.0, 2.0])
    assert h.steps == pytest.approx([0.0, 0.2, 0.6, 1.4, 3.0, 5.0, 7.0])
    assert h.writes == []


def test_ceiling_forces_delivery_with_warning(fake_dlb_store_v9: Path, log_file: Path) -> None:
    h = Harness(fake_dlb_store_v9, idle=False)
    insert_message_v9(fake_dlb_store_v9, recipient="w", sender="boss")
    t = h.run_until_written()
    assert t >= launcher.PENDING_CEILING_S
    assert t < launcher.PENDING_CEILING_S + launcher.PENDING_BACKOFF_MAX_S + 1e-9
    assert all(s < launcher.PENDING_CEILING_S for s in h.steps[:-1])
    assert "forced" in _log_text(log_file)


def test_backoff_resets_after_delivery(fake_dlb_store_v9: Path) -> None:
    h = Harness(fake_dlb_store_v9, idle=False)
    insert_message_v9(fake_dlb_store_v9, recipient="w", sender="a")
    h.run_until(5.0)
    h.idle.idle = True
    h.run_until_written()
    h.idle.idle = False
    insert_message_v9(fake_dlb_store_v9, recipient="w", sender="b")
    # The next pending batch starts again from the first retry interval,
    # not from the 2.0s cap the previous batch reached.
    assert h.step() == pytest.approx(launcher.PENDING_RETRY_S)
    assert h.step() == pytest.approx(2 * launcher.PENDING_RETRY_S)


# -- unsent user input (finding 12) -----------------------------------------


def test_dirty_line_defers_past_ceiling_then_delivers(
    fake_dlb_store_v9: Path, log_file: Path
) -> None:
    h = Harness(fake_dlb_store_v9, idle=True)
    h.line.feed(b"half-typed")
    insert_message_v9(fake_dlb_store_v9, recipient="w", sender="boss")
    h.run_until(3 * launcher.PENDING_CEILING_S)
    assert h.writes == [], "must never inject over the user's unsent input"
    assert _log_text(log_file).count("unsent input") == 1
    h.line.feed(b"\r")  # user submits their line
    h.step()
    assert len(h.writes) == 1 and b"boss" in h.writes[0][1]


def test_backspace_to_empty_line_allows_delivery(fake_dlb_store_v9: Path) -> None:
    h = Harness(fake_dlb_store_v9, idle=True)
    h.line.feed(b"ab")
    insert_message_v9(fake_dlb_store_v9, recipient="w", sender="boss")
    h.step()
    assert h.writes == []
    h.line.feed(b"\x7f\x7f")
    h.step()
    assert len(h.writes) == 1


# -- work-only waking in the loop (finding 5) --------------------------------


def test_cancelled_messages_do_not_wake(fake_dlb_store_v9: Path) -> None:
    h = Harness(fake_dlb_store_v9, idle=True)
    insert_message_v9(fake_dlb_store_v9, recipient="w", sender="boss", cancelled=True)
    h.run_until(10.0)
    assert h.writes == []
    assert h.loop.watermark == 0


def test_receipts_do_not_wake(fake_dlb_store_v9: Path) -> None:
    h = Harness(fake_dlb_store_v9, idle=True)
    insert_message_v9(fake_dlb_store_v9, recipient="w", sender="boss", msg_type="receipt")
    h.run_until(10.0)
    assert h.writes == []


def test_receipts_do_not_wake_v3_to_v8(fake_dlb_store_v3_to_v8: Path) -> None:
    h = Harness(fake_dlb_store_v3_to_v8, idle=True)
    dlb_schema.insert_message(fake_dlb_store_v3_to_v8, recipient="w", msg_type="receipt")
    h.run_until(10.0)
    assert h.writes == []
    dlb_schema.insert_message(fake_dlb_store_v3_to_v8, recipient="w", sender="boss")
    h.run_until(20.0)
    assert len(h.writes) == 1 and b"boss" in h.writes[0][1]


def test_work_after_non_work_wakes_exactly_once(fake_dlb_store_v9: Path) -> None:
    h = Harness(fake_dlb_store_v9, idle=True)
    insert_message_v9(fake_dlb_store_v9, recipient="w", msg_type="receipt")
    insert_message_v9(fake_dlb_store_v9, recipient="w", cancelled=True)
    h.run_until(5.0)
    insert_message_v9(fake_dlb_store_v9, recipient="w", sender="boss", msg_type="task")
    h.run_until(30.0)
    assert len(h.writes) == 1
    assert b"1 unread" in h.writes[0][1]


def test_read_before_wake_advances_without_injecting(fake_dlb_store_v9: Path) -> None:
    h = Harness(fake_dlb_store_v9, idle=True)
    m = insert_message_v9(fake_dlb_store_v9, recipient="w", sender="boss", read=True)
    h.step()
    assert h.writes == []
    assert h.loop.watermark == m


# -- baseline and watermark (findings 1, 11) ----------------------------------


def test_baseline_excludes_preexisting_mail(fake_dlb_store_v9: Path) -> None:
    insert_message_v9(fake_dlb_store_v9, recipient="w", sender="old", read=True)
    baseline = watcher.poll("w", 0).handled_up_to(0)
    h = Harness(fake_dlb_store_v9, idle=True, baseline=baseline)
    h.run_until(5.0)
    assert h.writes == []
    insert_message_v9(fake_dlb_store_v9, recipient="w", sender="new")
    h.run_until(10.0)
    assert len(h.writes) == 1 and b"new" in h.writes[0][1]


def test_row_committed_after_delivering_poll_still_wakes(
    fake_dlb_store_v9: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watermark must stop at rows the delivering poll saw.

    A message committed right after the poll (before the wake is written)
    would be skipped forever if the watermark jumped to a fresh MAX(id).
    """
    h = Harness(fake_dlb_store_v9, idle=True)
    first = insert_message_v9(fake_dlb_store_v9, recipient="w", sender="a")
    real_poll = watcher.poll
    late: list[int] = []

    def poll_then_commit(*args, **kwargs):  # noqa: ANN002, ANN003
        snap = real_poll(*args, **kwargs)
        if not late:
            late.append(insert_message_v9(fake_dlb_store_v9, recipient="w", sender="late"))
        return snap

    monkeypatch.setattr(launcher.watcher, "poll", poll_then_commit)
    h.step()
    assert len(h.writes) == 1 and b"from a." in h.writes[0][1]
    assert h.loop.watermark == first < late[0]
    monkeypatch.setattr(launcher.watcher, "poll", real_poll)
    h.run_until(5.0)
    assert len(h.writes) == 2 and b"late" in h.writes[1][1]


def test_run_takes_baseline_before_fork(
    fake_dlb_store_v9: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The startup snapshot and the owner fingerprint precede pty.fork."""
    order: list[str] = []
    real_poll, real_row = watcher.poll, watcher.agent_row

    class Forked(Exception):
        pass

    def spy_poll(*a, **k):  # noqa: ANN002, ANN003
        order.append("poll")
        return real_poll(*a, **k)

    def spy_row(*a, **k):  # noqa: ANN002, ANN003
        order.append("owner")
        return real_row(*a, **k)

    def fake_fork():  # noqa: ANN202
        order.append("fork")
        raise Forked

    monkeypatch.delenv("DLB_SESSION_ID", raising=False)
    monkeypatch.setattr(launcher.watcher, "poll", spy_poll)
    monkeypatch.setattr(launcher.watcher, "agent_row", spy_row)
    monkeypatch.setattr(launcher.pty, "fork", fake_fork)
    with pytest.raises(Forked):
        launcher.run(["true"], "w")
    assert order == ["poll", "owner", "fork"]


# -- heartbeat timing (finding 8) --------------------------------------------


def test_heartbeat_immediately_then_every_interval(fake_dlb_store_v9: Path) -> None:
    h = Harness(fake_dlb_store_v9, idle=True)
    h.spy.result = watcher.HB_STAMPED
    h.run_until(2 * launcher.HEARTBEAT_INTERVAL_S + 1.0)
    times = [t for t, _, _ in h.spy.calls]
    assert times == pytest.approx([0.0, 30.0, 60.0])
    assert all(sid == "s1" for _, sid, _ in h.spy.calls)


def test_heartbeat_not_due_waits_cap_at_next_beat(
    fake_dlb_store_v9: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a poll interval that does not divide 30s, the beat still lands on time."""
    monkeypatch.setattr(launcher, "POLL_INTERVAL_S", 7.0)
    h = Harness(fake_dlb_store_v9, idle=True)
    h.spy.result = watcher.HB_STAMPED
    h.run_until(31.0)
    assert [t for t, _, _ in h.spy.calls] == pytest.approx([0.0, 30.0])


def test_heartbeat_retried_each_poll_until_registered(fake_dlb_store_v9: Path) -> None:
    h = Harness(fake_dlb_store_v9, idle=True)
    h.spy.result = watcher.HB_NOT_REGISTERED
    h.run_until(4.0)
    assert [t for t, _, _ in h.spy.calls] == pytest.approx([0.0, 1.5, 3.0])


def test_heartbeat_real_restamp(fake_dlb_store_v9: Path) -> None:
    insert_agent_v9(fake_dlb_store_v9, "w", session_id="s1")
    h = Harness(fake_dlb_store_v9, idle=True, heartbeat=watcher.record_heartbeat)
    h.step()
    first = dlb_schema.heartbeat_of(fake_dlb_store_v9, "w")
    assert first is not None and first > 1
    dlb_schema.update_agent(fake_dlb_store_v9, "w", monitor_heartbeat_ms=1)
    h.run_until(launcher.HEARTBEAT_INTERVAL_S - 0.1)
    assert dlb_schema.heartbeat_of(fake_dlb_store_v9, "w") == 1
    h.run_until(launcher.HEARTBEAT_INTERVAL_S + 0.1)
    assert dlb_schema.heartbeat_of(fake_dlb_store_v9, "w") > 1


# -- heartbeat ownership (finding 10) ----------------------------------------


def test_heartbeat_skips_foreign_row_and_warns_once(
    fake_dlb_store_v9: Path, log_file: Path
) -> None:
    insert_agent_v9(fake_dlb_store_v9, "w", session_id="other")
    h = Harness(fake_dlb_store_v9, idle=True, heartbeat=watcher.record_heartbeat)
    h.run_until(3 * launcher.HEARTBEAT_INTERVAL_S + 1.0)
    assert dlb_schema.heartbeat_of(fake_dlb_store_v9, "w") is None
    assert _log_text(log_file).count("belongs to another session") == 1


def test_owner_binds_to_registration_made_after_start(fake_dlb_store_v9: Path) -> None:
    insert_agent_v9(fake_dlb_store_v9, "w", token_hash="stale")
    owner = launcher.HeartbeatOwner("w", db=fake_dlb_store_v9)
    assert owner.credentials() is None  # the pre-existing row is not ours
    dlb_schema.update_agent(fake_dlb_store_v9, "w", token_hash="fresh")
    assert owner.credentials() == (None, "fresh")
    # Once bound, a later takeover does not rebind.
    dlb_schema.update_agent(fake_dlb_store_v9, "w", token_hash="hijack")
    assert owner.credentials() == (None, "fresh")
    assert watcher.record_heartbeat("w", db=fake_dlb_store_v9, token_hash="fresh") == (
        watcher.HB_NOT_OWNER
    )


def test_owner_prefers_session_id_when_row_has_one(fake_dlb_store_v9: Path) -> None:
    owner = launcher.HeartbeatOwner("w", db=fake_dlb_store_v9)
    assert owner.credentials() is None  # unregistered
    insert_agent_v9(fake_dlb_store_v9, "w", session_id="sx", token_hash="t")
    assert owner.credentials() == ("sx", None)


def test_owner_with_expected_session_id_never_binds_elsewhere(fake_dlb_store_v9: Path) -> None:
    insert_agent_v9(fake_dlb_store_v9, "w", session_id="other")
    owner = launcher.HeartbeatOwner("w", expected_session_id="mine", db=fake_dlb_store_v9)
    assert owner.credentials() == ("mine", None)


# -- robustness ---------------------------------------------------------------


def test_write_failure_stops_loop(fake_dlb_store_v9: Path) -> None:
    h = Harness(fake_dlb_store_v9, idle=True)

    def broken(_b: bytes) -> None:
        raise OSError("pty closed")

    h.loop.write = broken
    insert_message_v9(fake_dlb_store_v9, recipient="w", sender="boss")
    h.step()
    assert h.loop.closed is True


def test_run_survives_step_exceptions(fake_dlb_store_v9: Path) -> None:
    import threading

    h = Harness(fake_dlb_store_v9, idle=True)
    calls = {"n": 0}

    def boom() -> float:
        calls["n"] += 1
        raise RuntimeError("boom")

    h.loop.step = boom  # type: ignore[method-assign]
    stop = threading.Event()

    def wait(_d: float) -> None:
        if calls["n"] >= 3:
            stop.set()

    h.loop.run(stop, wait=wait)
    assert calls["n"] == 3
