"""Shared fixtures: isolated SQLite DLB store per test.

By default (auto-used fixture `fake_dlb_store`), each test gets a v1-shaped
store. Tests that need v2 schema can request the `fake_dlb_store_v2` fixture
which overrides via the same DLB_STORE env var.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name           TEXT PRIMARY KEY,
    working_on     TEXT,
    registered_at  TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    session_token  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_name  TEXT NOT NULL,
    sender_name     TEXT NOT NULL,
    subject         TEXT,
    body            TEXT NOT NULL,
    sent_at         TEXT NOT NULL,
    read_at         TEXT,
    expires_at      TEXT NOT NULL
);
"""

V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name              TEXT PRIMARY KEY,
    working_on        TEXT,
    registered_at_ms  INTEGER NOT NULL,
    last_seen_ms      INTEGER NOT NULL,
    session_token     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_name  TEXT NOT NULL,
    sender_name     TEXT NOT NULL,
    subject         TEXT,
    body            TEXT NOT NULL,
    sent_at_ms      INTEGER NOT NULL,
    read_at_ms      INTEGER,
    expires_at_ms   INTEGER NOT NULL
);
"""


@pytest.fixture(autouse=True)
def fake_dlb_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Materialise a v1-shaped DLB sqlite at $TMP and point the watcher at it.

    Default fixture used by every test. Schema-v2 tests should additionally
    request `fake_dlb_store_v2` which rebuilds the same file with v2 columns.
    """
    store = tmp_path / "store.sqlite3"
    conn = sqlite3.connect(str(store))
    try:
        conn.executescript(V1_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("DLB_STORE", str(store))
    return store


@pytest.fixture()
def fake_dlb_store_v2(fake_dlb_store: Path) -> Path:
    """Rebuild the (auto-created) v1 store as v2. Idempotent in test scope."""
    fake_dlb_store.unlink()
    conn = sqlite3.connect(str(fake_dlb_store))
    try:
        conn.executescript(V2_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return fake_dlb_store


def insert_message(
    store: Path,
    *,
    recipient: str,
    sender: str = "tester",
    body: str = "hello",
    read: bool = False,
) -> int:
    """Helper: drop a message into a v1-shape store and return its id."""
    conn = sqlite3.connect(str(store))
    try:
        cur = conn.execute(
            "INSERT INTO messages (recipient_name, sender_name, subject, body, "
            "sent_at, read_at, expires_at) "
            "VALUES (?, ?, NULL, ?, '2026-01-01T00:00:00+00:00', "
            "?, '2099-01-01T00:00:00+00:00')",
            (recipient, sender, body, "2026-01-01T00:00:00+00:00" if read else None),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def insert_message_v2(
    store: Path,
    *,
    recipient: str,
    sender: str = "tester",
    body: str = "hello",
    read: bool = False,
) -> int:
    """v2-shape variant: ms-integer columns. Uses fixed plausible timestamps."""
    conn = sqlite3.connect(str(store))
    try:
        cur = conn.execute(
            "INSERT INTO messages (recipient_name, sender_name, subject, body, "
            "sent_at_ms, read_at_ms, expires_at_ms) "
            "VALUES (?, ?, NULL, ?, 1767225600000, ?, 4070908800000)",
            (recipient, sender, body, 1767225600000 if read else None),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()
