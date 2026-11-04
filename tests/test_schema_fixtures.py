"""Divergence check: `tests/dlb_schema.py` must match dlb-mcp's own DDL.

The fixtures copy dlb-mcp's schema verbatim so the launcher's suite runs
without dlb-mcp installed. When dlb-mcp IS importable, this replays its real
`_create_v2_schema` + migration chain (and `_create_current_schema`) on a
temp database and asserts the copied fixtures produce the identical shape.
A failure means dlb-mcp changed its DDL: re-copy it into dlb_schema.py.

Only temp databases are touched; dlb-mcp's store module has no import-time
side effects and its migration helpers act on the connection they are given.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from . import dlb_schema

store = pytest.importorskip("dlb_mcp.store")

# The order `_apply_migrations` runs them in, from a v2 store.
MIGRATIONS = {
    3: "_migrate_v2_to_v3",
    4: "_migrate_v3_to_v4",
    5: "_migrate_v4_to_v5",
    6: "_migrate_v5_to_v6",
    7: "_migrate_v6_to_v7",
    8: "_migrate_v7_to_v8",
    9: "_migrate_v8_to_v9",
}

# dlb-mcp bookkeeping the launcher never reads; ignored on both sides.
IGNORED_TABLES = {"dlb_meta"}


def _shape(path: Path) -> dict[str, list[tuple[object, ...]]]:
    return {k: v for k, v in dlb_schema.table_shape(path).items() if k not in IGNORED_TABLES}


def _real(path: Path, version: int | str) -> Path:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        if version == "fresh":
            store._create_current_schema(conn)
        else:
            store._create_v2_schema(conn)
            for v in range(3, int(version) + 1):
                getattr(store, MIGRATIONS[v])(conn)
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.mark.parametrize("version", [*dlb_schema.MIGRATED_VERSIONS, "fresh"])
def test_fixture_matches_dlb_mcp(tmp_path: Path, version: int | str) -> None:
    real = _real(tmp_path / "real.sqlite3", version)
    fixture = dlb_schema.build_store(tmp_path / "fixture.sqlite3", version)
    assert _shape(fixture) == _shape(real)


def test_apply_migrations_tail_matches_v9(tmp_path: Path) -> None:
    """dlb-mcp's own `_apply_migrations` from v2 lands on the v9 fixture."""
    path = tmp_path / "real.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        store._create_v2_schema(conn)
        store._apply_migrations(conn)
        conn.commit()
    finally:
        conn.close()
    fixture = dlb_schema.build_store(tmp_path / "fixture.sqlite3", 9)
    assert _shape(fixture) == _shape(path)
