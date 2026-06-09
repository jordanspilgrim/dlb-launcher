"""Shared fixtures: isolated SQLite DLB store per test."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

SCHEMA = """
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


@pytest.fixture(autouse=True)
def fake_dlb_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Materialise a DLB-shaped sqlite at $TMP and point the watcher at it."""
    store = tmp_path / "store.sqlite3"
    conn = sqlite3.connect(str(store))
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("DLB_STORE", str(store))
    return store


def insert_message(
    store: Path,
    *,
    recipient: str,
    sender: str = "tester",
    body: str = "hello",
    read: bool = False,
) -> int:
    """Helper: drop a message into the fake store and return its id."""
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
