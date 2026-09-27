"""DuckDB writer for Uniswap V3 Swap data.

Two tables:
- raw_uniswap_v3_swap: verbatim audit trail from Alchemy, keyed on
  (chain, block_hash, log_index) so reorged variants of the same log
  can coexist without violating the PK.
- uniswap_v3_swap: decoded, pipeline-ready rows, keyed on
  (chain, tx_hash, log_index). Reorged rows are the caller's
  responsibility to exclude (via the raw table's `removed` flag).

Idempotency: writes use INSERT OR IGNORE so re-runs against overlapping
block ranges are safe.

Big-integer storage: amount0/amount1 (int256), sqrt_price_x96 (uint160),
and liquidity (uint128) are stored as VARCHAR. None of them fit DuckDB's
signed HUGEINT (2^127) reliably. See models.py docstring for reasoning.
"""

from __future__ import annotations

import logging

from duckdb import DuckDBPyConnection

from alphawallets.fetchers.uniswap_v3.models import RawSwapLog, UniswapV3Swap

logger = logging.getLogger(__name__)


# ---------- Schema ----------


RAW_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS raw_uniswap_v3_swap (
    chain              VARCHAR   NOT NULL,
    block_number       BIGINT    NOT NULL,
    block_hash         VARCHAR   NOT NULL,
    tx_hash            VARCHAR   NOT NULL,
    log_index          INTEGER   NOT NULL,
    transaction_index  INTEGER   NOT NULL,
    address            VARCHAR   NOT NULL,
    topics             VARCHAR[] NOT NULL,
    data               VARCHAR   NOT NULL,
    removed            BOOLEAN   NOT NULL DEFAULT FALSE,
    PRIMARY KEY (chain, block_hash, log_index)
);
"""

DECODED_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS uniswap_v3_swap (
    chain            VARCHAR     NOT NULL,
    block_number     BIGINT      NOT NULL,
    block_timestamp  TIMESTAMPTZ NOT NULL,
    tx_hash          VARCHAR     NOT NULL,
    log_index        INTEGER     NOT NULL,
    pool_address     VARCHAR     NOT NULL,
    sender           VARCHAR     NOT NULL,
    recipient        VARCHAR     NOT NULL,
    amount0          VARCHAR     NOT NULL,
    amount1          VARCHAR     NOT NULL,
    sqrt_price_x96   VARCHAR     NOT NULL,
    liquidity        VARCHAR     NOT NULL,
    tick             INTEGER     NOT NULL,
    PRIMARY KEY (chain, tx_hash, log_index)
);
"""


def create_tables(conn: DuckDBPyConnection) -> None:
    """Create both tables if they don't already exist. Safe to call every run."""
    conn.execute(RAW_TABLE_DDL)
    conn.execute(DECODED_TABLE_DDL)


# ---------- Writes ----------


def _row_count(conn: DuckDBPyConnection, table: str) -> int:
    """Return the current row count of a table."""
    result = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert result is not None, f"COUNT(*) on {table} returned no rows"
    return int(result[0])


def write_raw_logs(conn: DuckDBPyConnection, logs: list[RawSwapLog]) -> int:
    """Insert raw logs with INSERT OR IGNORE. Returns rows actually inserted.

    Idempotent: re-running with overlapping data inserts only new rows.
    """
    if not logs:
        return 0

    before = _row_count(conn, "raw_uniswap_v3_swap")
    rows = [
        (
            log.chain,
            log.block_number,
            log.block_hash,
            log.tx_hash,
            log.log_index,
            log.transaction_index,
            log.address,
            log.topics,
            log.data,
            log.removed,
        )
        for log in logs
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO raw_uniswap_v3_swap VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    inserted = _row_count(conn, "raw_uniswap_v3_swap") - before
    logger.info(
        "raw_uniswap_v3_swap: %d rows submitted, %d inserted, %d skipped as duplicates",
        len(logs),
        inserted,
        len(logs) - inserted,
    )
    return inserted


def write_decoded_swaps(conn: DuckDBPyConnection, swaps: list[UniswapV3Swap]) -> int:
    """Insert decoded swaps with INSERT OR IGNORE. Returns rows actually inserted.

    Idempotent. Big integers (amount0, amount1, sqrt_price_x96, liquidity) are
    stored as VARCHAR — see module docstring.
    """
    if not swaps:
        return 0

    before = _row_count(conn, "uniswap_v3_swap")
    rows = [
        (
            swap.chain,
            swap.block_number,
            swap.block_timestamp,
            swap.tx_hash,
            swap.log_index,
            swap.pool_address,
            swap.sender,
            swap.recipient,
            str(swap.amount0),
            str(swap.amount1),
            str(swap.sqrt_price_x96),
            str(swap.liquidity),
            swap.tick,
        )
        for swap in swaps
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO uniswap_v3_swap VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    inserted = _row_count(conn, "uniswap_v3_swap") - before
    logger.info(
        "uniswap_v3_swap: %d rows submitted, %d inserted, %d skipped as duplicates",
        len(swaps),
        inserted,
        len(swaps) - inserted,
    )
    return inserted
