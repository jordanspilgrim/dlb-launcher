"""Watcher against dlb-mcp schema v9: work-only waking, heartbeat, pending batches."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from dlb_launcher import launcher, watcher

from .conftest import insert_agent_v9, insert_message_v9


def _past_ms() -> int:
    return int(time.time() * 1000) - 60_000


def test_non_work_messages_never_wake(fake_dlb_store_v9: Path) -> None:
    insert_message_v9(fake_dlb_store_v9, recipient="w", msg_type="receipt")
    insert_message_v9(fake_dlb_store_v9, recipient="w", cancelled=True)
    insert_message_v9(fake_dlb_store_v9, recipient="w", deadline_ms=_past_ms())
    insert_message_v9(fake_dlb_store_v9, recipient="w", expires_at_ms=_past_ms())
    assert watcher.max_message_id_for("w") == 0
    assert watcher.unread_summary_for("w") == (0, [])


def test_live_work_still_counts(fake_dlb_store_v9: Path) -> None:
    insert_message_v9(fake_dlb_store_v9, recipient="w", msg_type="receipt")
    task = insert_message_v9(fake_dlb_store_v9, recipient="w", sender="boss", msg_type="task")
    insert_message_v9(fake_dlb_store_v9, recipient="w", msg_type="receipt")
    plain = insert_message_v9(
        fake_dlb_store_v9, recipient="w", sender="peer", expires_at_ms=None, deadline_ms=None
    )
    assert watcher.max_message_id_for("w") == max(task, plain)
    assert watcher.unread_summary_for("w") == (2, ["boss", "peer"])


def _heartbeat(store: Path, name: str) -> int | None:
    conn = sqlite3.connect(str(store))
    try:
        row = conn.execute(
            "SELECT monitor_heartbeat_ms FROM agents WHERE name = ?", (name,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_heartbeat_updates_registered_row(fake_dlb_store_v9: Path) -> None:
    insert_agent_v9(fake_dlb_store_v9, "w")
    before = int(time.time() * 1000)
    assert watcher.record_heartbeat("w") is True
    hb = _heartbeat(fake_dlb_store_v9, "w")
    assert hb is not None and hb >= before


def test_heartbeat_ignores_unregistered_name(fake_dlb_store_v9: Path) -> None:
    assert watcher.record_heartbeat("ghost") is False
    assert _heartbeat(fake_dlb_store_v9, "ghost") is None


def test_heartbeat_no_op_on_old_schemas(fake_dlb_store: Path) -> None:
    assert watcher.record_heartbeat("w") is False  # v1


def test_heartbeat_no_op_on_v2(fake_dlb_store_v2: Path) -> None:
    assert watcher.record_heartbeat("w") is False


def test_heartbeat_never_creates_store(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "store.sqlite3"
    assert watcher.record_heartbeat("w", db=missing) is False
    assert not missing.exists()


class _FakeIdle:
    def __init__(self) -> None:
        self.idle = False

    def is_idle(self) -> bool:
        return self.idle


def _drain(fd: int) -> bytes:
    os.set_blocking(fd, False)
    try:
        return os.read(fd, 65536)
    except BlockingIOError:
        return b""


def test_busy_child_keeps_batch_pending(
    fake_dlb_store_v9: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(launcher, "PENDING_RETRY_S", 0.02)
    insert_agent_v9(fake_dlb_store_v9, "w")
    idle = _FakeIdle()
    stop = threading.Event()
    r, w = os.pipe()
    t = threading.Thread(target=launcher._watcher_loop, args=("w", w, idle, stop), daemon=True)
    t.start()
    try:
        insert_message_v9(fake_dlb_store_v9, recipient="w", sender="boss", msg_type="task")
        # Busy well past several poll cycles: nothing written, batch not dropped.
        time.sleep(0.4)
        assert _drain(r) == b""
        idle.idle = True
        deadline = time.time() + 3.0
        out = b""
        while time.time() < deadline and not out:
            out = _drain(r)
            time.sleep(0.02)
        assert b"boss" in out
        # Exactly once: no repeat on later polls.
        time.sleep(0.3)
        assert _drain(r) == b""
        assert _heartbeat(fake_dlb_store_v9, "w") is not None
    finally:
        stop.set()
        t.join(timeout=2.0)
        os.close(r)
        os.close(w)
