"""DuckDB connection helper.

Chain-generic (used by every fetcher). Opens the on-disk cache at the
path from config, creating the parent directory if it doesn't exist.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb
from duckdb import DuckDBPyConnection

from alphawallets.config import get_cache_db_path


@contextmanager
def connect(db_path: Path | str | None = None) -> Iterator[DuckDBPyConnection]:
    """Open a DuckDB connection to the cache file.

    Args:
        db_path: Optional override. Defaults to get_cache_db_path() from config.
            Pass ':memory:' for a transient DB (used in tests).

    Yields:
        A live DuckDBPyConnection. Closed automatically on exit.

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
        yield conn
    finally:
        conn.close()
