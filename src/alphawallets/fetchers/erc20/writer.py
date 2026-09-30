"""DuckDB writer for ERC-20 transfer data.

Two tables, mirroring the AW_01 raw-vs-decoded pattern (CLAUDE.md §6):
- raw_erc20_transfer: verbatim-shaped audit trail of Transfers API entries.
- erc20_transfer: normalized, pipeline-ready rows.

Primary key: (chain, unique_id) on BOTH tables.

    Why not (chain, tx_hash, log_index) as AW_01 uses? The Transfers API omits
    logIndex for most entries, so it cannot carry dedup responsibility here — a
    nullable column inside a composite PK is either rejected or silently admits
    duplicate NULL rows depending on the engine. Alchemy guarantees uniqueId is
    unique per transfer, which is exactly the property a PK needs.

    The trade-off: unique_id is an opaque vendor string, so the PK couples the
    schema to Alchemy. Re-fetching the same transfer from a different provider
    would not collide with the existing row. Acceptable while Alchemy is the
    sole V1 source (ADR 0003); revisit if a second provider lands.

log_index is kept as a regular nullable column (indexed) so rows can still be
joined to raw event logs when Alchemy does supply it.

Idempotency: INSERT OR IGNORE, same as AW_01 — safe to re-run overlapping
block ranges.

Big integers: value_raw (uint256) is VARCHAR. It does not fit DuckDB's signed
HUGEINT for large-supply tokens.
"""

from __future__ import annotations

import logging

from duckdb import DuckDBPyConnection

from alphawallets.fetchers.erc20.models import ERC20Transfer, RawAssetTransfer

logger = logging.getLogger(__name__)


# ---------- Schema ----------


RAW_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS raw_erc20_transfer (
    chain             VARCHAR     NOT NULL,
    unique_id         VARCHAR     NOT NULL,
    block_num         BIGINT      NOT NULL,
    tx_hash           VARCHAR     NOT NULL,
    from_addr         VARCHAR     NOT NULL,
    to_addr           VARCHAR     NOT NULL,
    value_raw         VARCHAR     NOT NULL,
    value_decimal     DOUBLE,
    asset             VARCHAR,
    category          VARCHAR     NOT NULL,
    contract_address  VARCHAR     NOT NULL,
    contract_decimal  INTEGER,
    log_index         INTEGER,
    block_timestamp   TIMESTAMPTZ,
    PRIMARY KEY (chain, unique_id)
);
"""

DECODED_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS erc20_transfer (
    chain            VARCHAR     NOT NULL,
    block_number     BIGINT      NOT NULL,
    block_timestamp  TIMESTAMPTZ NOT NULL,
    tx_hash          VARCHAR     NOT NULL,
    log_index        INTEGER,
    unique_id        VARCHAR     NOT NULL,
    token_address    VARCHAR     NOT NULL,
    from_addr        VARCHAR     NOT NULL,
    to_addr          VARCHAR     NOT NULL,
    value_raw        VARCHAR     NOT NULL,
    token_decimals   INTEGER,
    PRIMARY KEY (chain, unique_id)
);
"""

# Secondary indexes. log_index supports joins to raw event logs; the address
# and timestamp indexes serve the wallet-activity and PnL queries that Week 3
# will run against this table.
INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_erc20_transfer_log_index "
    "ON erc20_transfer (chain, tx_hash, log_index);",
    "CREATE INDEX IF NOT EXISTS idx_erc20_transfer_from ON erc20_transfer (from_addr);",
    "CREATE INDEX IF NOT EXISTS idx_erc20_transfer_to ON erc20_transfer (to_addr);",
    "CREATE INDEX IF NOT EXISTS idx_erc20_transfer_block_ts ON erc20_transfer (block_timestamp);",
)


def create_tables(conn: DuckDBPyConnection) -> None:
    """Create both tables and their indexes if absent. Safe to call every run."""
    conn.execute(RAW_TABLE_DDL)
    conn.execute(DECODED_TABLE_DDL)
    for statement in INDEX_DDL:
        conn.execute(statement)


# ---------- Writes ----------


def _row_count(conn: DuckDBPyConnection, table: str) -> int:
    """Return the current row count of a table."""
    result = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert result is not None, f"COUNT(*) on {table} returned no rows"
    return int(result[0])


def write_raw_transfers(conn: DuckDBPyConnection, raws: list[RawAssetTransfer]) -> int:
    """Insert raw transfers with INSERT OR IGNORE. Returns rows actually inserted.

    Idempotent: re-running with overlapping data inserts only new rows.
    """
    if not raws:
        return 0

    before = _row_count(conn, "raw_erc20_transfer")
    rows = [
        (
            raw.chain,
            raw.unique_id,
            raw.block_num,
            raw.tx_hash,
            raw.from_addr,
            raw.to_addr,
            raw.value_raw,
            raw.value_decimal,
            raw.asset,
            raw.category,
            raw.contract_address,
            raw.contract_decimal,
            raw.log_index,
            raw.block_timestamp,
        )
        for raw in raws
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO raw_erc20_transfer "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    inserted = _row_count(conn, "raw_erc20_transfer") - before
    logger.info(
        "raw_erc20_transfer: %d rows submitted, %d inserted, %d skipped as duplicates",
        len(raws),
        inserted,
        len(raws) - inserted,
    )
    return inserted


def write_decoded_transfers(conn: DuckDBPyConnection, decoded: list[ERC20Transfer]) -> int:
    """Insert decoded transfers with INSERT OR IGNORE. Returns rows actually inserted.

    Idempotent. value_raw is stored as VARCHAR — see module docstring.
    """
    if not decoded:
        return 0

    before = _row_count(conn, "erc20_transfer")
    rows = [
        (
            transfer.chain,
            transfer.block_number,
            transfer.block_timestamp,
            transfer.tx_hash,
            transfer.log_index,
            transfer.unique_id,
            transfer.token_address,
            transfer.from_addr,
            transfer.to_addr,
            transfer.value_raw,
            transfer.token_decimals,
        )
        for transfer in decoded
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO erc20_transfer VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    inserted = _row_count(conn, "erc20_transfer") - before
    logger.info(
        "erc20_transfer: %d rows submitted, %d inserted, %d skipped as duplicates",
        len(decoded),
        inserted,
        len(decoded) - inserted,
    )
    return inserted
