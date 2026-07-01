"""Tests verifying the launcher's watcher works against dlb-mcp 0.2.0+
(INTEGER ms columns) as well as against 0.1.x (TEXT ISO strings).

dlb-mcp 0.2.0 renames sent_at→sent_at_ms, read_at→read_at_ms (etc.) and
backfills via in-place migration. Without compat handling the watcher
would silently see 0 unread for every recipient on an upgraded DB —
catastrophic for the entire wake mechanism.

The compat shim lives in watcher._detect_schema and is exercised by
every query. These tests use the explicit fake_dlb_store_v2 fixture
to materialize the v2 schema instead of the auto v1 default.
"""

from __future__ import annotations

from pathlib import Path

from dlb_launcher import watcher

from .conftest import insert_message_v2


def test_max_id_works_on_v2_schema(fake_dlb_store_v2: Path) -> None:
    """The id column hasn't changed shape — basic sanity."""
    id1 = insert_message_v2(fake_dlb_store_v2, recipient="alpha")
    id2 = insert_message_v2(fake_dlb_store_v2, recipient="alpha")
    assert watcher.max_message_id_for("alpha") == id2
    assert id2 > id1


def test_unread_summary_works_on_v2_schema(fake_dlb_store_v2: Path) -> None:
    """The whole point of this PR — without schema detection, this would
    return (0, []) because the query references the old `read_at` column."""
    insert_message_v2(fake_dlb_store_v2, recipient="alpha", sender="bob", read=True)
    insert_message_v2(fake_dlb_store_v2, recipient="alpha", sender="carol", read=False)
    insert_message_v2(fake_dlb_store_v2, recipient="alpha", sender="eve", read=False)
    count, senders = watcher.unread_summary_for("alpha")
    assert count == 2
    # Order preserved (sent_at_ms ASC; we used identical timestamps so insertion
    # order via id is the tiebreaker, which matches schema's intent)
    assert "carol" in senders
    assert "eve" in senders
    assert "bob" not in senders


def test_detect_schema_returns_v1_on_legacy(fake_dlb_store: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(str(fake_dlb_store))
    try:
        assert watcher._detect_schema(conn) == "v1"
    finally:
        conn.close()


def test_detect_schema_returns_v2_on_upgraded(fake_dlb_store_v2: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(str(fake_dlb_store_v2))
    try:
        assert watcher._detect_schema(conn) == "v2"
    finally:
        conn.close()


def test_watcher_picks_up_v2_message_after_initial_v1(tmp_path: Path, monkeypatch) -> None:
    """Real-world upgrade path: install starts on dlb-mcp 0.1, user
    upgrades to 0.2 mid-flight (migration adds *_ms columns to the
    SAME file). After migration the watcher must seamlessly pick up
    new messages on the v2 schema."""
    import sqlite3

    # `fake_dlb_store` (autouse) already created store.sqlite3 with v1
    # tables in tmp_path. Use a separate file so we can write a clean
    # migration sequence here, then point DLB_STORE at it.
    store = tmp_path / "store_migration.sqlite3"
    conn = sqlite3.connect(str(store))
    conn.executescript(
        """
        CREATE TABLE messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_name  TEXT NOT NULL,
            sender_name     TEXT NOT NULL,
            subject         TEXT,
            body            TEXT NOT NULL,
            sent_at         TEXT NOT NULL,
            read_at         TEXT,
            expires_at      TEXT NOT NULL
        );
        INSERT INTO messages (recipient_name, sender_name, subject, body,
            sent_at, read_at, expires_at)
        VALUES ('alpha', 'bob', NULL, 'v1 era',
            '2026-01-01T00:00:00+00:00', NULL, '2099-01-01T00:00:00+00:00');
        """
    )
    conn.commit()

    # Simulate the dlb-mcp 0.2.0 migration: ADD COLUMN + backfill
    conn.executescript(
        """
        ALTER TABLE messages ADD COLUMN sent_at_ms INTEGER;
        ALTER TABLE messages ADD COLUMN read_at_ms INTEGER;
        ALTER TABLE messages ADD COLUMN expires_at_ms INTEGER;
        UPDATE messages SET sent_at_ms = 1767225600000,
                            expires_at_ms = 4070908800000;
        INSERT INTO messages (recipient_name, sender_name, subject, body,
            sent_at, read_at, expires_at, sent_at_ms, read_at_ms, expires_at_ms)
        VALUES ('alpha', 'carol', NULL, 'v2 era',
            '2026-02-01T00:00:00+00:00', NULL, '2099-01-01T00:00:00+00:00',
            1769904000000, NULL, 4070908800000);
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DLB_STORE", str(store))

    # Schema now has BOTH sets of columns. _detect_schema should pick v2.
    count, senders = watcher.unread_summary_for("alpha")
    assert count == 2
    assert set(senders) == {"bob", "carol"}
