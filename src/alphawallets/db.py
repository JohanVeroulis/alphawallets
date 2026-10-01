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
