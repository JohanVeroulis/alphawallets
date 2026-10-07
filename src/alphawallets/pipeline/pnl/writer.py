"""DuckDB writer for the wallet_pnl table (ADR 0012 schema).

One table, holding per-(chain, wallet, token, window) realized PnL with the
three caveat flags surfaced as columns rather than silently applied.

**INSERT OR REPLACE, not INSERT OR IGNORE.** Every other writer in this project
treats a row as an immutable observation and ignores a duplicate. This one does
the opposite on purpose: ADR 0012's final consequence records that a running
cost basis means a window's PnL legitimately changes when earlier events are
re-fetched — a deeper backfill, or a reorg repair. Ignoring the second write
would leave a stale figure in place and make `computed_at` a lie about which
data produced the row.

`balance_token` is VARCHAR. Token quantities are uint256-scale and overflow
HUGEINT, the same reason `value_raw` and `amount0` are VARCHAR in the fetcher
tables. The model carries a Decimal; this writer renders it.

No secondary indexes, per the AW_02 EXPLAIN finding: DuckDB's planner chose
SEQ_SCAN over every index tried at V1 row counts, because it is columnar with
zone maps and vectorized scans beat b-tree lookups at this scale. The leaderboard
sorts on `realized_pnl_trading_usd`, which would be the obvious candidate —
measure before adding it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from duckdb import DuckDBPyConnection

from alphawallets.db import assert_table_matches_ddl
from alphawallets.pipeline.pnl.models import WalletPnL

logger = logging.getLogger(__name__)


# ---------- Schema ----------


WALLET_PNL_DDL = """
CREATE TABLE IF NOT EXISTS wallet_pnl (
    chain                     VARCHAR     NOT NULL,
    wallet                    VARCHAR     NOT NULL,
    token_address             VARCHAR     NOT NULL,
    window_start              TIMESTAMPTZ NOT NULL,
    window_end                TIMESTAMPTZ NOT NULL,
    realized_pnl_usd          DOUBLE      NOT NULL,
    realized_pnl_trading_usd  DOUBLE      NOT NULL,
    realized_pnl_airdrop_usd  DOUBLE      NOT NULL,
    unrealized_pnl_usd        DOUBLE,
    bought_usd                DOUBLE      NOT NULL,
    sold_usd                  DOUBLE      NOT NULL,
    realization_count         INTEGER     NOT NULL,
    balance_token             VARCHAR     NOT NULL,
    avg_cost_basis_usd        DOUBLE,
    has_pre_window_activity   BOOLEAN     NOT NULL DEFAULT FALSE,
    has_unpriceable_events    BOOLEAN     NOT NULL DEFAULT FALSE,
    has_smart_wallet_signal   BOOLEAN     NOT NULL DEFAULT FALSE,
    computed_at               TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (chain, wallet, token_address, window_start, window_end)
);
"""

# Expected shape of the table above, for the schema-drift guard in
# create_tables(). Hand-written next to the DDL rather than parsed out of it: a
# parser would happily agree with a typo in both, whereas a reviewer changing one
# and not the other is exactly the drift this catches (ADR 0009).
EXPECTED_COLUMNS: list[tuple[str, str]] = [
    ("chain", "VARCHAR"),
    ("wallet", "VARCHAR"),
    ("token_address", "VARCHAR"),
    ("window_start", "TIMESTAMPTZ"),
    ("window_end", "TIMESTAMPTZ"),
    ("realized_pnl_usd", "DOUBLE"),
    ("realized_pnl_trading_usd", "DOUBLE"),
    ("realized_pnl_airdrop_usd", "DOUBLE"),
    ("unrealized_pnl_usd", "DOUBLE"),
    ("bought_usd", "DOUBLE"),
    ("sold_usd", "DOUBLE"),
    ("realization_count", "INTEGER"),
    ("balance_token", "VARCHAR"),
    ("avg_cost_basis_usd", "DOUBLE"),
    ("has_pre_window_activity", "BOOLEAN"),
    ("has_unpriceable_events", "BOOLEAN"),
    ("has_smart_wallet_signal", "BOOLEAN"),
    ("computed_at", "TIMESTAMPTZ"),
]

_COLUMN_COUNT = len(EXPECTED_COLUMNS)


def create_tables(conn: DuckDBPyConnection) -> None:
    """Create wallet_pnl if absent, then check it against the DDL.

    Safe to call every run. The drift check is the ADR 0009 pattern: CREATE TABLE
    IF NOT EXISTS does nothing to an existing table, so a cache written before a
    column was added keeps the old shape and fails later inside a write with an
    error that names neither the column nor the cause.
    """
    conn.execute(WALLET_PNL_DDL)
    assert_table_matches_ddl(conn, "wallet_pnl", EXPECTED_COLUMNS)


# ---------- Writes ----------


def write_wallet_pnl(conn: DuckDBPyConnection, rows: Iterable[WalletPnL]) -> int:
    """Write PnL rows, replacing any existing row with the same key.

    Consumes the iterable incrementally rather than materialising it, so a
    universe-wide run streams from compute_wallet_pnl's generator instead of
    buffering two rows per (wallet, token) pair first.

    Args:
        conn: An open DuckDB connection with the table created.
        rows: WalletPnL rows, in any order.

    Returns:
        The number of rows written, counting replacements.
    """
    written = 0
    per_window: dict[int, int] = {}
    flagged_pre_window = 0
    flagged_unpriceable = 0

    for row in rows:
        conn.execute(
            f"INSERT OR REPLACE INTO wallet_pnl VALUES ({', '.join(['?'] * _COLUMN_COUNT)})",
            [
                row.chain,
                row.wallet,
                row.token_address,
                row.window_start,
                row.window_end,
                row.realized_pnl_usd,
                row.realized_pnl_trading_usd,
                row.realized_pnl_airdrop_usd,
                row.unrealized_pnl_usd,
                row.bought_usd,
                row.sold_usd,
                row.realization_count,
                # Decimal to VARCHAR: uint256 quantities overflow HUGEINT, so the
                # string is the lossless representation. str() on a Decimal is
                # exact — it does not go through float.
                str(row.balance_token),
                row.avg_cost_basis_usd,
                row.has_pre_window_activity,
                row.has_unpriceable_events,
                row.has_smart_wallet_signal,
                row.computed_at,
            ],
        )
        written += 1
        days = (row.window_end - row.window_start).days
        per_window[days] = per_window.get(days, 0) + 1
        flagged_pre_window += row.has_pre_window_activity
        flagged_unpriceable += row.has_unpriceable_events

    if written:
        logger.info(
            "wallet_pnl: %d row(s) written; per window %s; %d flagged "
            "has_pre_window_activity, %d flagged has_unpriceable_events",
            written,
            {f"{days}d": count for days, count in sorted(per_window.items())},
            flagged_pre_window,
            flagged_unpriceable,
        )
    else:
        logger.info("wallet_pnl: nothing to write")
    return written
