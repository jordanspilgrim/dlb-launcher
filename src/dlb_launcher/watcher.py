"""DLB inbox-watcher logic.

Pure-stdlib polling watcher for new messages in `~/.dlb/store.sqlite3`.
Exists as a separate module so the polling logic is unit-testable without
needing to stand up a PTY.

The watcher does NOT inject anything itself — it just answers the question
"are there new messages for <name> since the last time I checked?" The
launcher process owns the PTY and is responsible for timing the injection
(see idle.py).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_STORE_PATH = Path.home() / ".dlb" / "store.sqlite3"


def store_path() -> Path:
    """Resolve the DLB SQLite store path, honouring the same env var DLB uses."""
    return Path(os.environ.get("DLB_STORE", str(DEFAULT_STORE_PATH))).expanduser()


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
