"""Watcher against dlb-mcp's real v2..v9 schemas: work-only filters, heartbeat."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from dlb_launcher import watcher

from . import dlb_schema
from .conftest import insert_agent_v9, insert_message_v9


def _past_ms() -> int:
    return int(time.time() * 1000) - 60_000


# -- work filter, v9 ---------------------------------------------------------


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
    future = int(time.time() * 1000) + 3_600_000
    plain = insert_message_v9(fake_dlb_store_v9, recipient="w", sender="peer", deadline_ms=future)
    assert watcher.max_message_id_for("w") == max(task, plain)
    assert watcher.unread_summary_for("w") == (2, ["boss", "peer"])


# -- work filter, v3..v8 (receipts and expiry; no cancel/deadline columns) ---


def test_v3_to_v8_receipts_and_expired_never_wake(fake_dlb_store_v3_to_v8: Path) -> None:
    store = fake_dlb_store_v3_to_v8
    assert "cancelled_at_ms" not in dlb_schema.columns(store, "messages")
    dlb_schema.insert_message(store, recipient="w", msg_type="receipt")
    dlb_schema.insert_message(store, recipient="w", expires_at_ms=_past_ms())
    assert watcher.max_message_id_for("w") == 0
    assert watcher.unread_summary_for("w") == (0, [])


def test_v3_to_v8_work_wakes(fake_dlb_store_v3_to_v8: Path) -> None:
    store = fake_dlb_store_v3_to_v8
    dlb_schema.insert_message(store, recipient="w", msg_type="receipt")
    task = dlb_schema.insert_message(store, recipient="w", sender="boss", msg_type="task")
    plain = dlb_schema.insert_message(store, recipient="w", sender="peer")
    assert watcher.max_message_id_for("w") == max(task, plain)
    assert watcher.unread_summary_for("w") == (2, ["boss", "peer"])


def test_v2_filters_only_expiry(fake_dlb_store_v2: Path) -> None:
    """v2 predates msg_type: every unexpired message is work."""
    m = dlb_schema.insert_message(fake_dlb_store_v2, recipient="w", sender="a")
    dlb_schema.insert_message(fake_dlb_store_v2, recipient="w", expires_at_ms=_past_ms())
    assert watcher.max_message_id_for("w") == m
    assert watcher.unread_summary_for("w") == (1, ["a"])


# -- poll snapshot / watermark ----------------------------------------------


def test_handled_up_to_covers_only_rows_the_poll_saw(fake_dlb_store_v9: Path) -> None:
    a = insert_message_v9(fake_dlb_store_v9, recipient="w", sender="x")
    snap = watcher.poll("w", 0)
    late = insert_message_v9(fake_dlb_store_v9, recipient="w", sender="y")
    assert snap.handled_up_to(0) == a
    assert watcher.poll("w", snap.handled_up_to(0)).new_ids == [late]


def test_handled_up_to_never_moves_backwards() -> None:
    assert watcher.Poll().handled_up_to(7) == 7
    assert watcher.Poll(new_ids=[3], unread=[(2, "s")]).handled_up_to(9) == 9


# -- heartbeat ---------------------------------------------------------------


def test_heartbeat_stamps_owned_row_by_session_id(fake_dlb_store_v9: Path) -> None:
    insert_agent_v9(fake_dlb_store_v9, "w", session_id="s1")
    before = int(time.time() * 1000)
    assert watcher.record_heartbeat("w", session_id="s1") == watcher.HB_STAMPED
    hb = dlb_schema.heartbeat_of(fake_dlb_store_v9, "w")
    assert hb is not None and hb >= before


def test_heartbeat_stamps_owned_row_by_token_hash(fake_dlb_store_v9: Path) -> None:
    insert_agent_v9(fake_dlb_store_v9, "w", token_hash="h-mine")
    assert watcher.record_heartbeat("w", token_hash="h-mine", now_ms=5) == watcher.HB_STAMPED
    assert dlb_schema.heartbeat_of(fake_dlb_store_v9, "w") == 5


def test_heartbeat_restamps(fake_dlb_store_v9: Path) -> None:
    insert_agent_v9(fake_dlb_store_v9, "w", session_id="s1")
    assert watcher.record_heartbeat("w", session_id="s1", now_ms=100) == watcher.HB_STAMPED
    assert watcher.record_heartbeat("w", session_id="s1", now_ms=200) == watcher.HB_STAMPED
    assert dlb_schema.heartbeat_of(fake_dlb_store_v9, "w") == 200


def test_heartbeat_refuses_foreign_row(fake_dlb_store_v9: Path) -> None:
    insert_agent_v9(fake_dlb_store_v9, "w", session_id="theirs", token_hash="h-theirs")
    assert watcher.record_heartbeat("w", session_id="mine") == watcher.HB_NOT_OWNER
    assert watcher.record_heartbeat("w", token_hash="h-mine") == watcher.HB_NOT_OWNER
    assert dlb_schema.heartbeat_of(fake_dlb_store_v9, "w") is None


def test_heartbeat_without_credentials_never_writes(fake_dlb_store_v9: Path) -> None:
    insert_agent_v9(fake_dlb_store_v9, "w", session_id="s1")
    assert watcher.record_heartbeat("w") == watcher.HB_NOT_OWNER
    assert dlb_schema.heartbeat_of(fake_dlb_store_v9, "w") is None


def test_heartbeat_ignores_unregistered_name(fake_dlb_store_v9: Path) -> None:
    assert watcher.record_heartbeat("ghost", session_id="s1") == watcher.HB_NOT_REGISTERED
    assert dlb_schema.heartbeat_of(fake_dlb_store_v9, "ghost") is None


def test_heartbeat_no_op_on_v1(fake_dlb_store: Path) -> None:
    assert watcher.record_heartbeat("w", session_id="s1") == watcher.HB_UNSUPPORTED


def test_heartbeat_no_op_on_v3_to_v8(fake_dlb_store_v3_to_v8: Path) -> None:
    dlb_schema.insert_agent(fake_dlb_store_v3_to_v8, "w", session_id="s1")
    assert watcher.record_heartbeat("w", session_id="s1") == watcher.HB_UNSUPPORTED


def test_heartbeat_never_creates_store(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "store.sqlite3"
    assert watcher.record_heartbeat("w", db=missing, session_id="s1") == watcher.HB_NO_STORE
    assert not missing.exists()


def test_connect_is_read_write_never_create(tmp_path: Path) -> None:
    """`mode=rw`: opening a path that vanished must fail, not create it."""
    missing = tmp_path / "gone.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        watcher._connect(missing)
    assert not missing.exists()


def test_agent_row_never_reads_plaintext_token(fake_dlb_store_v2: Path) -> None:
    dlb_schema.insert_agent(fake_dlb_store_v2, "w")
    row = watcher.agent_row("w")
    assert row is not None
    assert row.token_hash is None and row.session_id is None
