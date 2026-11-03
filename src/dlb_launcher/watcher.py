"""DLB inbox-watcher logic.

Pure-stdlib polling watcher for new messages in `~/.dlb/store.sqlite3`.
Exists as a separate module so the polling logic is unit-testable without
needing to stand up a PTY.

The watcher does NOT inject anything itself — it just answers the question
"are there new messages for <name> since the last time I checked?" The
launcher process owns the PTY and is responsible for timing the injection
(see launcher.py).

Schema compat: dlb-mcp 0.1.x stored timestamps as TEXT ISO strings; 0.2.0+
uses INTEGER epoch-ms in `*_ms` columns. The watcher probes table_info on
every call (one PRAGMA, instant) and queries whichever shape is present,
so a launcher install works against either DLB version with no version
pinning.

Work-only waking (dlb-mcp schema v9+): read receipts, cancelled or
superseded messages, and messages past their deadline or expiry are not
work, so they never move the watermark and are never counted in the wake
summary. Each filter clause is added only when its column exists, so older
stores behave exactly as before. These match `list_threads`' unread_count
and the `dlb-inbox-check` hook.

One write: `record_heartbeat` stamps `agents.monitor_heartbeat_ms` for the
launcher's name, so `list_threads` reports the session as `wakeable` and the
inbox hook stops warning that no monitor is running. It updates an existing
row only; it never creates the store, a table, or an agent.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DEFAULT_STORE_PATH = Path.home() / ".dlb" / "store.sqlite3"


def store_path() -> Path:
    """Resolve the DLB SQLite store path, honouring the same env var DLB uses."""
    return Path(os.environ.get("DLB_STORE", str(DEFAULT_STORE_PATH))).expanduser()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _detect_schema(conn: sqlite3.Connection) -> str:
    """Return 'v2' if the messages table has the *_ms columns, else 'v1'.

    Used by every query to pick the right column names. If the messages
    table is missing entirely (DLB never initialized), returns 'v2' as a
    benign default — the queries below all tolerate no-rows gracefully.
    """
    cols = _columns(conn, "messages")
    if not cols or "sent_at_ms" in cols:
        return "v2"
    return "v1"


def _work_filter(conn: sqlite3.Connection) -> tuple[str, list[object]]:
    """SQL fragment (leading ' AND ...' or '') excluding non-work messages."""
    cols = _columns(conn, "messages")
    now = int(time.time() * 1000)
    where: list[str] = []
    params: list[object] = []
    if "msg_type" in cols:
        where.append("(msg_type IS NULL OR msg_type != 'receipt')")
    if "cancelled_at_ms" in cols:
        where.append("cancelled_at_ms IS NULL")
    if "deadline_ms" in cols:
        where.append("(deadline_ms IS NULL OR deadline_ms > ?)")
        params.append(now)
    if "expires_at_ms" in cols:
        where.append("(expires_at_ms IS NULL OR expires_at_ms > ?)")
        params.append(now)
    return ("".join(f" AND {w}" for w in where), params)


def max_message_id_for(name: str, *, db: Path | None = None) -> int:
    """Return the highest work message.id addressed to `name`, or 0 if none/no-store.

    Used as a low-cost watermark — the watcher remembers this number and
    compares against subsequent reads to detect "anything new since last
    poll." We deliberately don't query unread_count because read_at gets
    set as soon as the recipient calls `read` (which can race the launcher's
    next poll); message.id is monotonic and never re-used by SQLite, so a
    watermark on it is a stable progress indicator. Non-work messages
    (receipts, cancelled, past deadline) are excluded so they never wake.
    """
    p = db or store_path()
    if not p.exists():
        return 0
    try:
        conn = sqlite3.connect(str(p), timeout=2.0)
        try:
            extra, params = _work_filter(conn)
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM messages WHERE recipient_name = ?" + extra,
                [name, *params],
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        # Store missing the table (DLB not migrated yet) → behave as 0.
        return 0


def unread_summary_for(name: str, *, db: Path | None = None) -> tuple[int, list[str]]:
    """Return (unread_count, sender_names) for unread work messages.

    Used to compose the synthetic wake-prompt body shown to the LLM. We
    surface sender names only — bodies stay gated behind the MCP tool's
    session_token auth, so the LLM must call `mcp__dlb__read` to actually
    fetch content. This module never touches message bodies.
    """
    p = db or store_path()
    if not p.exists():
        return (0, [])
    try:
        conn = sqlite3.connect(str(p), timeout=2.0)
        try:
            if _detect_schema(conn) == "v2":
                read_col, order_col = "read_at_ms", "sent_at_ms"
            else:
                read_col, order_col = "read_at", "sent_at"
            extra, params = _work_filter(conn)
            rows = conn.execute(
                f"SELECT sender_name FROM messages "
                f"WHERE recipient_name = ? AND {read_col} IS NULL{extra} "
                f"ORDER BY {order_col} ASC, id ASC",
                [name, *params],
            ).fetchall()
            senders = [r[0] for r in rows]
            return (len(senders), senders)
        finally:
            conn.close()
    except sqlite3.Error:
        return (0, [])


def record_heartbeat(name: str, *, db: Path | None = None) -> bool:
    """Stamp `agents.monitor_heartbeat_ms` for `name`. True if a row was updated.

    False when the store is missing, predates heartbeats (schema < v9), the
    name is not registered, or on any error. Never creates the store.
    """
    p = db or store_path()
    if not p.exists():
        return False
    try:
        conn = sqlite3.connect(str(p), timeout=2.0)
        try:
            if "monitor_heartbeat_ms" not in _columns(conn, "agents"):
                return False
            cur = conn.execute(
                "UPDATE agents SET monitor_heartbeat_ms = ? WHERE name = ?",
                (int(time.time() * 1000), name),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except sqlite3.Error:
        return False
