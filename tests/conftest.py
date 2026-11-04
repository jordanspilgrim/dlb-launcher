"""Shared fixtures: isolated SQLite DLB store per test.

By default (auto-used fixture `fake_dlb_store`), each test gets a v1-shaped
store. Tests that need a newer schema request `fake_dlb_store_v2`,
`fake_dlb_store_v3_to_v8` (parametrized) or `fake_dlb_store_v9`, which
rebuild the same file (same DLB_STORE) from dlb-mcp's real DDL, copied
verbatim into `tests/dlb_schema.py`. DLB_STORE always points into tmp_path:
no test ever touches the live store at ~/.dlb.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from . import dlb_schema

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
    """Rebuild the (auto-created) v1 store as a real dlb-mcp v2 store."""
    fake_dlb_store.unlink()
    return dlb_schema.build_store(fake_dlb_store, 2)


@pytest.fixture()
def fake_dlb_store_v9(fake_dlb_store: Path) -> Path:
    """Rebuild the (auto-created) v1 store as a real dlb-mcp v9 store.

    Built from the verbatim dlb-mcp DDL in `tests/dlb_schema.py` (v2 schema
    plus every migration through v8->v9), not a hand-written approximation.
    """
    fake_dlb_store.unlink()
    return dlb_schema.build_store(fake_dlb_store, 9)


@pytest.fixture(params=[3, 4, 5, 6, 7, 8])
def fake_dlb_store_v3_to_v8(request: pytest.FixtureRequest, fake_dlb_store: Path) -> Path:
    """The same store rebuilt at each pre-v9 schema that has `msg_type`."""
    fake_dlb_store.unlink()
    return dlb_schema.build_store(fake_dlb_store, request.param)


def insert_message_v9(
    store: Path,
    *,
    recipient: str,
    sender: str = "tester",
    body: str = "hello",
    read: bool = False,
    msg_type: str | None = None,
    cancelled: bool = False,
    deadline_ms: int | None = None,
    expires_at_ms: int = 4070908800000,
) -> int:
    """Insert into a real v9 store (lifecycle columns set explicitly)."""
    return dlb_schema.insert_message(
        store,
        recipient=recipient,
        sender=sender,
        body=body,
        read_at_ms=1767225600000 if read else None,
        expires_at_ms=expires_at_ms,
        msg_type=msg_type,
        cancelled_at_ms=1767225600000 if cancelled else None,
        deadline_ms=deadline_ms,
    )


def insert_agent_v9(
    store: Path, name: str, *, session_id: str | None = None, token_hash: str = "hash-1"
) -> None:
    dlb_schema.insert_agent(store, name, session_id=session_id, token_hash=token_hash)


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
