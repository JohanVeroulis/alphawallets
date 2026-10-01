"""DuckDB writer for historical token prices.

One table, `token_price`, holding hour-aligned USD prices joined by the PnL
pipeline on date_trunc('hour', block_timestamp).

Primary key: (chain, token_address, ts, source) — four columns.

    `source` is in the key on purpose. DefiLlama covers every V1 token today
    (ADR 0008), but a fallback provider is explicitly reserved for tokens
    outside the tracked set. With source in the key, a CoinGecko row for the
    same (chain, token, hour) coexists with the DefiLlama one rather than
    colliding, so the pipeline can choose a provider per query and the lineage
    of each price stays visible. A three-column key would have forced either
    silent overwriting or a separate table per provider.

No secondary indexes. AW_02 established the pattern: EXPLAIN on 50k rows chose
SEQ_SCAN over every index tried, including a composite candidate, because
DuckDB is columnar with zone maps and vectorized scans beat b-tree lookups at
V1 row counts. Indexes cost ~1% on writes for no measured read benefit. Do not
add speculative indexes here — measure first, at realistic scale.

Idempotency: INSERT OR IGNORE, so re-running an overlapping span is safe. The
mapper can legitimately emit two rows with the same `ts` (two observations
inside one hour), and this is where that collision resolves: the first row
wins, the second is ignored. The orchestrator counts the difference so the
overlap stays visible.

Price storage: price_usd is DOUBLE, not VARCHAR. Unlike token amounts (uint256,
which overflows HUGEINT), a USD price fits a float comfortably and needs
arithmetic in SQL.
"""

from __future__ import annotations

import logging

from duckdb import DuckDBPyConnection

from alphawallets.db import assert_table_matches_ddl
from alphawallets.fetchers.prices.models import TokenPrice

logger = logging.getLogger(__name__)


# ---------- Schema ----------


# Semantic note for PnL developers reading this table: when two observations
# fall inside the same hour, INSERT OR IGNORE keeps the FIRST one written.
# A row therefore represents the earliest observed price in that hour, not the
# last and not an average. Verified in test_writer.py::TestSameHourCollision.
TOKEN_PRICE_DDL = """
CREATE TABLE IF NOT EXISTS token_price (
    chain          VARCHAR     NOT NULL,
    token_address  VARCHAR     NOT NULL,
    ts             TIMESTAMPTZ NOT NULL,
    price_usd      DOUBLE      NOT NULL,
    confidence     DOUBLE,
    source         VARCHAR     NOT NULL DEFAULT 'defillama',
    fetched_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (chain, token_address, ts, source)
);
"""


# Expected shape of the table above, for the schema-drift guard in
# create_tables(). Deliberately hand-written next to the DDL rather than parsed
# out of it: a reviewer changing one and not the other is the drift this catches,
# and a parser would happily agree with a typo.
TOKEN_PRICE_COLUMNS: list[tuple[str, str]] = [
    ("chain", "VARCHAR"),
    ("token_address", "VARCHAR"),
    ("ts", "TIMESTAMPTZ"),
    ("price_usd", "DOUBLE"),
    ("confidence", "DOUBLE"),
    ("source", "VARCHAR"),
    ("fetched_at", "TIMESTAMPTZ"),
]


def create_tables(conn: DuckDBPyConnection) -> None:
    """Create the token_price table if it doesn't exist. Safe to call every run.

    No secondary indexes — see the module docstring for why (AW_02's EXPLAIN
    finding). PRIMARY KEY uniqueness is enforced automatically.

    The table is then checked against its DDL — see assert_table_matches_ddl.
    """
    conn.execute(TOKEN_PRICE_DDL)
    assert_table_matches_ddl(conn, "token_price", TOKEN_PRICE_COLUMNS)


# ---------- Writes ----------


def _row_count(conn: DuckDBPyConnection) -> int:
    """Return the current row count of token_price."""
    result = conn.execute("SELECT COUNT(*) FROM token_price").fetchone()
    assert result is not None, "COUNT(*) on token_price returned no rows"
    return int(result[0])


def write_token_prices(conn: DuckDBPyConnection, prices: list[TokenPrice]) -> int:
    """Insert price rows with INSERT OR IGNORE. Returns rows actually inserted.

    Idempotent: re-running with overlapping data inserts only new rows. Two
    rows sharing (chain, token_address, ts, source) collapse to the first one
    written — the caller compares the return value against len(prices) to see
    how many were absorbed.

    Args:
        conn: An open DuckDB connection with the table created.
        prices: Hour-aligned TokenPrice rows.

    Returns:
        The number of rows actually inserted.
    """
    if not prices:
        return 0

    before = _row_count(conn)
    rows = [
        (
            price.chain,
            price.token_address,
            price.ts,
            price.price_usd,
            price.confidence,
            price.source,
            price.fetched_at,
        )
        for price in prices
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO token_price VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    inserted = _row_count(conn) - before
    logger.info(
        "token_price: %d rows submitted, %d inserted, %d skipped as duplicates",
        len(prices),
        inserted,
        len(prices) - inserted,
    )
    return inserted
