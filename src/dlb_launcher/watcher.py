"""DLB inbox-watcher logic.

Pure-stdlib polling watcher for new messages in `~/.dlb/store.sqlite3`.
Exists as a separate module so the polling logic is unit-testable without
needing to stand up a PTY.

The watcher does NOT inject anything itself. It just answers the question
"are there new messages for <name> since the last time I checked?" The
launcher process owns the PTY and is responsible for timing the injection
(see launcher.py).

Schema compat: dlb-mcp 0.1.x stored timestamps as TEXT ISO strings; 0.2.0+
uses INTEGER epoch-ms in `*_ms` columns. The watcher probes table_info on
every call (one PRAGMA, instant) and queries whichever shape is present,
so a launcher install works against either DLB version with no version
pinning.

Versioning: every feature is detected per column via `PRAGMA table_info`,
never via `PRAGMA user_version`. dlb-mcp is moving that pragma to mean
"minimum reader version" (with the schema version in a `dlb_meta` table),
so gating on it would break.

Work-only waking: read receipts (`msg_type`, schema v3+), messages past
expiry (`expires_at_ms`, v2+), and cancelled, superseded, or past-deadline
messages (`cancelled_at_ms`, `deadline_ms`, v9+) are not work. They never
move the watermark and are never counted in the wake summary. Each filter
clause is added only when its column exists, so older stores get exactly
the filters their schema can express. These match `list_threads`'
unread_count and the `dlb-inbox-check` hook.

One write: `record_heartbeat` stamps `agents.monitor_heartbeat_ms` (v9+)
for the launcher's name, so `list_threads` reports the session as
`wakeable`. It updates an existing row only, and only when that row still
belongs to this launcher's session (matched by session_id or token_hash
inside the UPDATE). Every connection opens with `mode=rw`, so the watcher
never creates the store, a table, or an agent.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_STORE_PATH = Path.home() / ".dlb" / "store.sqlite3"

# record_heartbeat results.
HB_STAMPED = "stamped"
HB_NO_STORE = "no_store"
HB_UNSUPPORTED = "unsupported"  # store predates monitor_heartbeat_ms (schema < v9)
HB_NOT_REGISTERED = "not_registered"
HB_NOT_OWNER = "not_owner"  # row exists but belongs to another session
HB_ERROR = "error"


def store_path() -> Path:
    """Resolve the DLB SQLite store path, honouring the same env var DLB uses."""
    return Path(os.environ.get("DLB_STORE", str(DEFAULT_STORE_PATH))).expanduser()


def _connect(p: Path) -> sqlite3.Connection:
    """Open an EXISTING store. Never creates the file.

    `mode=rw` makes SQLite fail with "unable to open database file" when
    the path is missing, instead of silently creating an empty store, which
    a plain `sqlite3.connect` does if the file vanishes after an exists()
    check.
    """
    uri = p.expanduser().absolute().as_uri() + "?mode=rw"
    return sqlite3.connect(uri, uri=True, timeout=2.0)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _detect_schema(conn: sqlite3.Connection) -> str:
    """Return 'v2' if the messages table has the *_ms columns, else 'v1'.

    Used by every query to pick the right column names. If the messages
    table is missing entirely (DLB never initialized), returns 'v2' as a
    benign default; the queries below all tolerate no-rows gracefully.
    """
    cols = _columns(conn, "messages")
    if not cols or "sent_at_ms" in cols:
        return "v2"
    return "v1"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _work_filter(conn: sqlite3.Connection, now_ms: int | None = None) -> tuple[str, list[object]]:
    """SQL fragment (leading ' AND ...' or '') excluding non-work messages."""
    cols = _columns(conn, "messages")
    now = _now_ms() if now_ms is None else now_ms
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


@dataclass(frozen=True)
class Poll:
    """One consistent read of a name's inbox.

    `new_ids`: work message ids above the caller's watermark, ascending.
    `unread`: (id, sender) for every unread work message, oldest first.
    Both come from the same read transaction, so a message landing mid-poll
    is in both or in neither.
    """

    new_ids: list[int] = field(default_factory=list)
    unread: list[tuple[int, str]] = field(default_factory=list)

    @property
    def max_id(self) -> int:
        return max(self.new_ids, default=0)

    @property
    def senders(self) -> list[str]:
        return [s for _, s in self.unread]

    def handled_up_to(self, watermark: int) -> int:
        """The watermark after acting on this poll.

        Advances only past rows this poll actually saw (its new work ids and
        the unread rows a wake built from it summarises), never to a MAX
        read separately, so a row committed after the poll stays above the
        watermark and wakes next time.
        """
        return max([watermark, *self.new_ids, *(i for i, _ in self.unread)])


def poll(
    name: str, after_id: int = 0, *, db: Path | None = None, now_ms: int | None = None
) -> Poll:
    """Read new work ids above `after_id` and the unread work summary at once."""
    p = db or store_path()
    if not p.exists():
        return Poll()
    try:
        conn = _connect(p)
        try:
            if _detect_schema(conn) == "v2":
                read_col, order_col = "read_at_ms", "sent_at_ms"
            else:
                read_col, order_col = "read_at", "sent_at"
            extra, params = _work_filter(conn, now_ms)
            # One read transaction across both SELECTs: a single snapshot in
            # WAL mode, a held shared lock in rollback-journal mode.
            conn.execute("BEGIN")
            try:
                new_ids = [
                    int(r[0])
                    for r in conn.execute(
                        "SELECT id FROM messages WHERE recipient_name = ? AND id > ?"
                        + extra
                        + " ORDER BY id ASC",
                        [name, after_id, *params],
                    ).fetchall()
                ]
                unread = [
                    (int(r[0]), str(r[1]))
                    for r in conn.execute(
                        f"SELECT id, sender_name FROM messages "
                        f"WHERE recipient_name = ? AND {read_col} IS NULL{extra} "
                        f"ORDER BY {order_col} ASC, id ASC",
                        [name, *params],
                    ).fetchall()
                ]
            finally:
                conn.rollback()
            return Poll(new_ids=new_ids, unread=unread)
        finally:
            conn.close()
    except sqlite3.Error:
        # Store missing the table (DLB not migrated yet), locked, or gone.
        return Poll()


def max_message_id_for(name: str, *, db: Path | None = None) -> int:
    """Return the highest work message.id addressed to `name`, or 0 if none/no-store.

    message.id is monotonic and never re-used by SQLite (AUTOINCREMENT), so
    a watermark on it is a stable progress indicator; read_at is not,
    because the recipient can mark mail read between two polls. Non-work
    messages are excluded so they never wake.
    """
    return poll(name, 0, db=db).max_id


def unread_summary_for(name: str, *, db: Path | None = None) -> tuple[int, list[str]]:
    """Return (unread_count, sender_names) for unread work messages.

    Used to compose the synthetic wake-prompt body shown to the LLM. We
    surface sender names only; bodies stay gated behind the MCP tool's
    session_token auth, so the LLM must call `mcp__dlb__read` to actually
    fetch content. This module never touches message bodies.
    """
    p = poll(name, 0, db=db)
    return (len(p.unread), p.senders)


@dataclass(frozen=True)
class AgentRow:
    """The identity columns of an agents row (whichever the schema has)."""

    session_id: str | None
    token_hash: str | None  # v7+ only; older schemas store the raw token, never read
    last_seen_ms: int | None


def agent_row(name: str, *, db: Path | None = None) -> AgentRow | None:
    """Return `name`'s identity columns, or None if unregistered / no store.

    Never reads a raw `session_token` column (pre-v7 stores): the launcher
    has no business holding another session's credential.
    """
    p = db or store_path()
    if not p.exists():
        return None
    try:
        conn = _connect(p)
        try:
            cols = _columns(conn, "agents")
            if not cols:
                return None
            sid = "session_id" if "session_id" in cols else "NULL"
            tok = "token_hash" if "token_hash" in cols else "NULL"
            seen = "last_seen_ms" if "last_seen_ms" in cols else "NULL"
            row = conn.execute(
                f"SELECT {sid}, {tok}, {seen} FROM agents WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return None
            return AgentRow(session_id=row[0], token_hash=row[1], last_seen_ms=row[2])
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def record_heartbeat(
    name: str,
    *,
    db: Path | None = None,
    session_id: str | None = None,
    token_hash: str | None = None,
    now_ms: int | None = None,
) -> str:
    """Stamp `agents.monitor_heartbeat_ms` for `name` if this launcher owns the row.

    Ownership is checked inside the UPDATE itself (one atomic statement):
    the row must match `session_id` when given, else `token_hash`. With
    neither, nothing is written: a launcher must never mark another
    session's name as wakeable.

    Returns one of the HB_* constants. Never creates the store.
    """
    p = db or store_path()
    if not p.exists():
        return HB_NO_STORE
    try:
        conn = _connect(p)
        try:
            cols = _columns(conn, "agents")
            if "monitor_heartbeat_ms" not in cols:
                return HB_UNSUPPORTED
            exists = conn.execute("SELECT 1 FROM agents WHERE name = ?", (name,)).fetchone()
            if exists is None:
                return HB_NOT_REGISTERED
            if session_id is not None and "session_id" in cols:
                owner_sql, owner_val = "session_id = ?", session_id
            elif token_hash is not None and "token_hash" in cols:
                owner_sql, owner_val = "token_hash = ?", token_hash
            else:
                return HB_NOT_OWNER
            stamp = _now_ms() if now_ms is None else now_ms
            cur = conn.execute(
                f"UPDATE agents SET monitor_heartbeat_ms = ? WHERE name = ? AND {owner_sql}",
                (stamp, name, owner_val),
            )
            conn.commit()
            return HB_STAMPED if cur.rowcount > 0 else HB_NOT_OWNER
        finally:
            conn.close()
    except sqlite3.Error:
        return HB_ERROR
