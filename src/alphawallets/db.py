"""DuckDB connection helper.

Chain-generic (used by every fetcher). Opens the on-disk cache at the
path from config, creating the parent directory if it doesn't exist.

Every connection is pinned to UTC. DuckDB defaults its session TimeZone to the
host locale, and date_trunc() on a TIMESTAMPTZ truncates in that zone, not in
UTC. Since token_price.ts is always hour-aligned UTC, the PnL join
date_trunc('hour', block_timestamp) = token_price.ts would silently match
nothing on a host at a half-hour offset: on Asia/Kolkata (UTC+5:30) a 14:53Z
swap truncates to 14:30Z, which is not an hour boundary and so is not a key any
price row has. Whole-hour-offset hosts happen to work, which is exactly what
makes it dangerous. Pinning here means no pipeline stage has to remember.

This module also provides assert_table_matches_ddl(), the schema-drift guard
every writer's create_tables() calls. CREATE TABLE IF NOT EXISTS is a no-op
against an existing table, so adding a column to a DDL leaves older cache files
silently on the old shape until a write fails with an opaque binder error. The
guard turns that into a named, immediate failure.

It surfaces drift. It does not migrate — migration is a decision, not a side
effect of calling create_tables().
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb
from duckdb import DuckDBPyConnection

from alphawallets.config import get_cache_db_path

# Session timezone every connection is pinned to. Not configurable on purpose:
# the stored instants are UTC, the price grid is UTC, and an operator's locale
# must not be able to change what a query means. See the module docstring.
SESSION_TIMEZONE = "UTC"


@contextmanager
def connect(db_path: Path | str | None = None) -> Iterator[DuckDBPyConnection]:
    """Open a DuckDB connection to the cache file.

    Args:
        db_path: Optional override. Defaults to get_cache_db_path() from config.
            Pass ':memory:' for a transient DB (used in tests).

    Yields:
        A live DuckDBPyConnection with its session TimeZone set to UTC, so
        date_trunc() and every timestamp rendering are locale-independent.
        Closed automatically on exit.

    Notes:
        DuckDB is single-writer per file. Callers scheduling concurrent
        fetchers must serialize their writes or use per-chain files
        (see ADR 0004). This helper does not enforce that.
    """
    if db_path is None:
        path = get_cache_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        db_path_str = str(path)
    elif str(db_path) == ":memory:":
        db_path_str = ":memory:"
    else:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        db_path_str = str(path)

    conn = duckdb.connect(db_path_str)
    try:
        conn.execute(f"SET TimeZone='{SESSION_TIMEZONE}'")
        yield conn
    finally:
        conn.close()


# ---------- Schema drift ----------


# DuckDB reports a canonical type name from PRAGMA table_info, which is not
# always the spelling used in a CREATE statement. Writers declare their expected
# columns in the DDL's spelling so the constant reads like the DDL beside it;
# these aliases let the guard compare the two.
_TYPE_ALIASES: dict[str, str] = {
    "TIMESTAMPTZ": "TIMESTAMP WITH TIME ZONE",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMP WITH TIME ZONE",
    "DATETIME": "TIMESTAMP",
    "TEXT": "VARCHAR",
    "STRING": "VARCHAR",
    "INT": "INTEGER",
    "INT4": "INTEGER",
    "INT8": "BIGINT",
    "FLOAT8": "DOUBLE",
    "BOOL": "BOOLEAN",
}


class SchemaDriftError(RuntimeError):
    """A live table's shape no longer matches the DDL that should define it.

    Raised by assert_table_matches_ddl(). The message names the table and every
    difference found, because the alternative — the error DuckDB raises on the
    first write — says only that the column counts disagree.
    """


def _canonical_type(type_name: str) -> str:
    """Normalise a DuckDB type name so DDL and PRAGMA spellings compare equal."""
    upper = type_name.strip().upper()
    return _TYPE_ALIASES.get(upper, upper)


def live_columns(conn: DuckDBPyConnection, table_name: str) -> list[tuple[str, str]]:
    """Return the live (name, type) pairs of a table, in declaration order.

    Args:
        conn: An open DuckDB connection.
        table_name: Table to inspect. Must already exist.

    Returns:
        (column_name, canonical_type) pairs as DuckDB reports them.
    """
    rows = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
    return [(row[1], _canonical_type(row[2])) for row in rows]


def assert_table_matches_ddl(
    conn: DuckDBPyConnection,
    table_name: str,
    expected_columns: list[tuple[str, str]],
) -> None:
    """Fail loudly if a live table's columns differ from what the DDL declares.

    Called by every writer's create_tables() right after its CREATE statements.
    CREATE TABLE IF NOT EXISTS does nothing to an existing table, so a cache file
    written before a column was added keeps the old shape and only fails later,
    inside a write, with a column-count mismatch that names nothing useful.

    Column order is not checked — writers insert by position, but a reordering
    without a rename or a type change is not something the DDL can drift into
    silently. Nullability, defaults and the primary key are likewise out of
    scope: they would each need their own comparison and none of them has caused
    a failure yet. Add them when one does.

    Args:
        conn: An open DuckDB connection.
        table_name: Table to check. Must already exist.
        expected_columns: (name, type) pairs from the writer's DDL, in the DDL's
            own spelling — TIMESTAMPTZ and TIMESTAMP WITH TIME ZONE both work.

    Raises:
        SchemaDriftError: On a missing column, an unexpected extra column, or a
            type mismatch. Never migrates — see the module docstring.
    """
    live = dict(live_columns(conn, table_name))
    expected = {name: _canonical_type(type_name) for name, type_name in expected_columns}

    missing = [name for name in expected if name not in live]
    extra = [name for name in live if name not in expected]
    mismatched = [
        (name, expected[name], live[name])
        for name in expected
        if name in live and live[name] != expected[name]
    ]

    if not (missing or extra or mismatched):
        return

    problems: list[str] = []
    if missing:
        problems.append(f"missing columns (in the DDL, absent from the table): {missing}")
    if extra:
        problems.append(f"extra columns (in the table, absent from the DDL): {extra}")
    if mismatched:
        detail = ", ".join(
            f"{name}: DDL says {want}, table has {got}" for name, want, got in mismatched
        )
        problems.append(f"type mismatches: {detail}")

    raise SchemaDriftError(
        f"Table {table_name!r} does not match its DDL — "
        + "; ".join(problems)
        + ". The cache file predates a schema change. This guard does not migrate: "
        "drop and re-fetch the table, or migrate it deliberately, then re-run."
    )
