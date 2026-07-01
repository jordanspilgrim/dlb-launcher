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
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_STORE_PATH = Path.home() / ".dlb" / "store.sqlite3"


def store_path() -> Path:
    """Resolve the DLB SQLite store path, honouring the same env var DLB uses."""
    return Path(os.environ.get("DLB_STORE", str(DEFAULT_STORE_PATH))).expanduser()


def _detect_schema(conn: sqlite3.Connection) -> str:
    """Return 'v2' if the messages table has the *_ms columns, else 'v1'.

    Used by every query to pick the right column names. If the messages
    table is missing entirely (DLB never initialized), returns 'v2' as a
    benign default — the queries below all tolerate no-rows gracefully.
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    except sqlite3.Error:
        return "v2"
    if "sent_at_ms" in cols:
        return "v2"
    return "v1"


def max_message_id_for(name: str, *, db: Path | None = None) -> int:
    """Return the highest message.id addressed to `name`, or 0 if none/no-store.

    Used as a low-cost watermark — the watcher remembers this number and
    compares against subsequent reads to detect "anything new since last
    poll." We deliberately don't query unread_count because read_at gets
    set as soon as the recipient calls `read` (which can race the launcher's
    next poll); message.id is monotonic and never re-used by SQLite, so a
    watermark on it is a stable progress indicator.
    """
    p = db or store_path()
    if not p.exists():
        return 0
    try:
        conn = sqlite3.connect(str(p), timeout=2.0)
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM messages WHERE recipient_name = ?",
                (name,),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        # Store missing the table (DLB not migrated yet) → behave as 0.
        return 0


def unread_summary_for(name: str, *, db: Path | None = None) -> tuple[int, list[str]]:
    """Return (unread_count, sender_names) for messages currently unread.

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
            schema = _detect_schema(conn)
            if schema == "v2":
                rows = conn.execute(
                    """
                    SELECT sender_name FROM messages
                    WHERE recipient_name = ? AND read_at_ms IS NULL
                    ORDER BY sent_at_ms ASC
                    """,
                    (name,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT sender_name FROM messages
                    WHERE recipient_name = ? AND read_at IS NULL
                    ORDER BY sent_at ASC
                    """,
                    (name,),
                ).fetchall()
            senders = [r[0] for r in rows]
            return (len(senders), senders)
        finally:
            conn.close()
    except sqlite3.Error:
        return (0, [])
