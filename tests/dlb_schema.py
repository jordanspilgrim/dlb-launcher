"""Real dlb-mcp store schemas, v2 through v9, for test fixtures.

SOURCE: dlb-mcp `src/dlb_mcp/store.py` (dispatch-reliability, 53969a4).
Every DDL string below is copied VERBATIM from that file:

- V2_*            `_create_v2_schema`
- V3_MESSAGE_COLS `_migrate_v2_to_v3`
- V4              `_migrate_v3_to_v4`
- V5              `_migrate_v4_to_v5`
- V6              `_migrate_v5_to_v6`
- V7_AGENTS       `_migrate_v6_to_v7` (agents table rebuild)
- V8              `_migrate_v7_to_v8`
- V9_*, CLAIMS    `_migrate_v8_to_v9`, `_create_claims_table`
- CURRENT_*       `_create_current_schema` (a fresh v9 store)

`build_store(path, version)` replays them in the same order dlb-mcp's
`_apply_migrations` does, so a fixture at version N has exactly the
columns (and column order) a real store migrated to N has.
`test_schema_fixtures.py` checks that against dlb_mcp itself when it is
importable. If dlb-mcp changes its DDL, that test fails: re-copy here.

The launcher never reads `PRAGMA user_version` (dlb-mcp is repurposing
it), so these fixtures do not set it either.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

V2_AGENTS = """
        CREATE TABLE IF NOT EXISTS agents (
            name              TEXT PRIMARY KEY,
            working_on        TEXT,
            registered_at_ms  INTEGER NOT NULL,
            last_seen_ms      INTEGER NOT NULL,
            session_token     TEXT NOT NULL
        )
        """

V2_MESSAGES = """
        CREATE TABLE IF NOT EXISTS messages (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_name        TEXT NOT NULL,
            sender_name           TEXT NOT NULL,
            subject               TEXT,
            body                  TEXT NOT NULL,
            sent_at_ms            INTEGER NOT NULL,
            read_at_ms            INTEGER,
            expires_at_ms         INTEGER NOT NULL
        )
        """

V2_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_messages_recipient "
    "ON messages(recipient_name, read_at_ms, sent_at_ms DESC)"
)

V3_MESSAGE_COLS = [
    ("msg_type", "TEXT"),
    ("in_reply_to", "INTEGER"),
    ("status", "TEXT"),
    ("status_note", "TEXT"),
    ("status_updated_at_ms", "INTEGER"),
]

V4 = "ALTER TABLE messages ADD COLUMN headline TEXT"

V5_AGENT_COLS = ("status", "status_detail")  # each: ALTER TABLE agents ADD COLUMN {col} TEXT

V6 = "ALTER TABLE agents ADD COLUMN session_id TEXT"

V7_AGENTS = """
        CREATE TABLE agents (
            name              TEXT PRIMARY KEY,
            working_on        TEXT,
            registered_at_ms  INTEGER NOT NULL,
            last_seen_ms      INTEGER NOT NULL,
            token_hash        TEXT NOT NULL,
            status            TEXT,
            status_detail     TEXT,
            session_id        TEXT,
            reclaim_hash      TEXT
        )
        """

V8_COL = "ALTER TABLE messages ADD COLUMN authenticated INTEGER"
V8_INDEX = "CREATE INDEX IF NOT EXISTS idx_messages_expires ON messages(expires_at_ms)"

V9_AGENT_STMTS = [
    "ALTER TABLE agents ADD COLUMN monitor_heartbeat_ms INTEGER",
    "ALTER TABLE agents ADD COLUMN receipts_opt_out INTEGER",
]
V9_MESSAGE_COLS = (
    ("cancelled_at_ms", "INTEGER"),
    ("cancel_reason", "TEXT"),
    ("superseded_by", "INTEGER"),
    ("deadline_ms", "INTEGER"),
)

CLAIMS = """
        CREATE TABLE IF NOT EXISTS claims (
            key            TEXT PRIMARY KEY,
            holder         TEXT NOT NULL,
            claimed_at_ms  INTEGER NOT NULL,
            expires_at_ms  INTEGER NOT NULL,
            note           TEXT
        )
        """

CURRENT_AGENTS = """
        CREATE TABLE IF NOT EXISTS agents (
            name              TEXT PRIMARY KEY,
            working_on        TEXT,
            registered_at_ms  INTEGER NOT NULL,
            last_seen_ms      INTEGER NOT NULL,
            token_hash        TEXT NOT NULL,
            status            TEXT,
            status_detail     TEXT,
            session_id        TEXT,
            reclaim_hash      TEXT,
            monitor_heartbeat_ms  INTEGER,
            receipts_opt_out      INTEGER
        )
        """

CURRENT_MESSAGES = """
        CREATE TABLE IF NOT EXISTS messages (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_name        TEXT NOT NULL,
            sender_name           TEXT NOT NULL,
            subject               TEXT,
            body                  TEXT NOT NULL,
            sent_at_ms            INTEGER NOT NULL,
            read_at_ms            INTEGER,
            expires_at_ms         INTEGER NOT NULL,
            msg_type              TEXT,
            in_reply_to           INTEGER,
            status                TEXT,
            status_note           TEXT,
            status_updated_at_ms  INTEGER,
            headline              TEXT,
            authenticated         INTEGER,
            cancelled_at_ms       INTEGER,
            cancel_reason         TEXT,
            superseded_by         INTEGER,
            deadline_ms           INTEGER
        )
        """

MIGRATED_VERSIONS = (2, 3, 4, 5, 6, 7, 8, 9)


def _apply(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(V2_AGENTS)
    conn.execute(V2_MESSAGES)
    conn.execute(V2_INDEX)
    if version >= 3:
        for col, typ in V3_MESSAGE_COLS:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {typ}")
    if version >= 4:
        conn.execute(V4)
    if version >= 5:
        for col in V5_AGENT_COLS:
            conn.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT")
    if version >= 6:
        conn.execute(V6)
    if version >= 7:
        # The fixture store is empty, so the rebuild's row copy is a no-op.
        conn.execute("DROP TABLE agents")
        conn.execute(V7_AGENTS)
    if version >= 8:
        conn.execute(V8_COL)
        conn.execute(V8_INDEX)
    if version >= 9:
        for stmt in V9_AGENT_STMTS:
            conn.execute(stmt)
        for col, typ in V9_MESSAGE_COLS:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {typ}")
        conn.execute(CLAIMS)


def build_store(path: Path, version: int | str) -> Path:
    """Create a store at `path` with the real schema for `version`.

    `version` is 2..9 (migrated shape) or "fresh" (`_create_current_schema`).
    """
    conn = sqlite3.connect(str(path))
    try:
        if version == "fresh":
            conn.execute(CURRENT_AGENTS)
            conn.execute(CURRENT_MESSAGES)
            conn.execute(CLAIMS)
            conn.execute(V2_INDEX)
            conn.execute(V8_INDEX)
        else:
            assert isinstance(version, int) and version in MIGRATED_VERSIONS, version
            _apply(conn, version)
        conn.commit()
    finally:
        conn.close()
    return path


def table_shape(path: Path) -> dict[str, list[tuple[object, ...]]]:
    """Per table: (name, type, notnull, pk) for each column, in order; plus indexes."""
    conn = sqlite3.connect(str(path))
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        shape: dict[str, list[tuple[object, ...]]] = {
            t: [(r[1], r[2], r[3], r[5]) for r in conn.execute(f"PRAGMA table_info({t})")]
            for t in tables
        }
        shape["__indexes__"] = [
            (r[0],)
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return shape
    finally:
        conn.close()


def columns(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def insert_message(path: Path, *, recipient: str, sender: str = "tester", **cols: object) -> int:
    """Insert into a v2+ store, setting only columns the schema has.

    Required columns get plausible defaults; extra `cols` (msg_type,
    cancelled_at_ms, deadline_ms, read_at_ms, expires_at_ms...) are applied
    when present in the schema and must be, or the test is wrong.
    """
    have = columns(path, "messages")
    row: dict[str, object] = {
        "recipient_name": recipient,
        "sender_name": sender,
        "body": "hello",
        "sent_at_ms": 1767225600000,
        "expires_at_ms": 4070908800000,
    }
    for k, v in cols.items():
        assert k in have, f"column {k} not in this schema"
        row[k] = v
    conn = sqlite3.connect(str(path))
    try:
        names = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cur = conn.execute(f"INSERT INTO messages ({names}) VALUES ({marks})", list(row.values()))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def insert_agent(
    path: Path,
    name: str,
    *,
    session_id: str | None = None,
    token_hash: str = "hash-1",
    last_seen_ms: int = 1767225600000,
) -> None:
    """Insert an agents row in whichever identity shape the schema has."""
    have = columns(path, "agents")
    row: dict[str, object] = {
        "name": name,
        "registered_at_ms": 1767225600000,
        "last_seen_ms": last_seen_ms,
    }
    if "token_hash" in have:
        row["token_hash"] = token_hash
    else:
        row["session_token"] = "plaintext-token"
    if "session_id" in have:
        row["session_id"] = session_id
    conn = sqlite3.connect(str(path))
    try:
        names = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO agents ({names}) VALUES ({marks})", list(row.values()))
        conn.commit()
    finally:
        conn.close()


def update_agent(path: Path, name: str, **cols: object) -> None:
    conn = sqlite3.connect(str(path))
    try:
        sets = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE agents SET {sets} WHERE name = ?", [*cols.values(), name])
        conn.commit()
    finally:
        conn.close()


def heartbeat_of(path: Path, name: str) -> int | None:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT monitor_heartbeat_ms FROM agents WHERE name = ?", (name,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()
