"""Unit tests for the watermark watcher + unread summary."""

from __future__ import annotations

from pathlib import Path

from dlb_launcher import watcher

from .conftest import insert_message


def test_max_id_is_zero_when_no_messages(fake_dlb_store: Path) -> None:
    assert watcher.max_message_id_for("alpha") == 0


def test_max_id_when_no_store_at_all(monkeypatch, tmp_path: Path) -> None:
    """Pointing at a non-existent store must not raise — return 0."""
    monkeypatch.setenv("DLB_STORE", str(tmp_path / "missing.sqlite3"))
    assert watcher.max_message_id_for("anyone") == 0


def test_max_id_increases_with_new_messages(fake_dlb_store: Path) -> None:
    id1 = insert_message(fake_dlb_store, recipient="alpha")
    id2 = insert_message(fake_dlb_store, recipient="alpha")
    assert watcher.max_message_id_for("alpha") == id2
    assert id2 > id1


def test_max_id_is_per_recipient(fake_dlb_store: Path) -> None:
    """Bravo's mail should not bump alpha's watermark — that would cause
    spurious wakes in alpha's session when bravo gets mail."""
    insert_message(fake_dlb_store, recipient="bravo")
    insert_message(fake_dlb_store, recipient="bravo")
    assert watcher.max_message_id_for("alpha") == 0
    assert watcher.max_message_id_for("bravo") > 0


def test_unread_summary_excludes_read_messages(fake_dlb_store: Path) -> None:
    insert_message(fake_dlb_store, recipient="alpha", sender="bob", read=True)
    insert_message(fake_dlb_store, recipient="alpha", sender="carol", read=False)
    count, senders = watcher.unread_summary_for("alpha")
    assert count == 1
    assert senders == ["carol"]


def test_unread_summary_preserves_send_order_and_keeps_dups(
    fake_dlb_store: Path,
) -> None:
    """The compose step dedupes; the summary returns full sender list so
    the compose step can show "+N more" semantics correctly."""
    insert_message(fake_dlb_store, recipient="alpha", sender="bob")
    insert_message(fake_dlb_store, recipient="alpha", sender="bob")
    insert_message(fake_dlb_store, recipient="alpha", sender="carol")
    count, senders = watcher.unread_summary_for("alpha")
    assert count == 3
    assert senders == ["bob", "bob", "carol"]
