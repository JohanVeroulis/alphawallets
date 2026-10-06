"""PnL calculator orchestrator (ADR 0012 step 4).

This module will eventually drive the whole PnL computation: read events, resolve
prices, classify transfers, feed the FIFO engine, emit WalletPnL rows. **This PR
contains only the data-reading skeleton** — no FIFO logic, no price lookup, no
WalletPnL emission.

Reading is a step worth isolating because of one hard constraint the engine
imposes. `FIFOEngine` does not sort: it trusts that events arrive in the order
they happened, and feeding them out of order produces a wrong cost basis
*silently*. The engine documents that as a caller contract rather than defending
against it, which makes this module the place the contract is honoured — so the
ordering is specified here, deterministically, and tested here.

Reads DuckDB only (CLAUDE.md Section 6): the cache is what the fetchers fill, and
a pipeline stage never reaches a provider.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterator

from duckdb import DuckDBPyConnection

from alphawallets.config import Chain
from alphawallets.fetchers.erc20.models import ERC20Transfer

logger = logging.getLogger(__name__)


# Deterministic chronological order, and every clause earns its place.
#
# block_timestamp is the economic ordering — it is what the price grid joins on
# and what window slicing (ADR 0012 decision 9) compares against. It is not
# unique: a block holds many transfers, and two blocks can share a timestamp.
#
# block_number breaks a timestamp tie in the direction chain history actually
# ran. Base produces blocks every ~2s, so same-second blocks are routine there
# rather than exotic.
#
# log_index orders events within a block. It is NULLABLE: ADR 0007's Transfers
# API finding is that Alchemy omits logIndex for the ERC-20 category, so rows
# fetched by AW_02 carry None while anything decoded from eth_getLogs carries a
# value. NULLS LAST puts the unindexed rows after the indexed ones in a block
# instead of letting DuckDB choose.
#
# unique_id is the final tiebreak and the reason this ordering is *total* rather
# than merely sensible. It is part of erc20_transfer's primary key, so two rows
# can never tie on it, which means the sort has no residual freedom and two runs
# over identical data produce identical order. Without it, a block of
# NULL-log_index rows would come back in whatever order the scan happened to
# produce, and the FIFO cost basis would differ between runs over the same cache.
TRANSFER_ORDER_BY = "block_timestamp, block_number, log_index NULLS LAST, unique_id"

_TRANSFER_COLUMNS = (
    "chain, block_number, block_timestamp, tx_hash, log_index, unique_id, "
    "token_address, from_addr, to_addr, value_raw, token_decimals"
)


def _fetch_transfer_events(
    conn: DuckDBPyConnection,
    chains: list[Chain] | None = None,
    wallet_filter: list[str] | None = None,
) -> Iterator[ERC20Transfer]:
    """Yield ERC-20 transfers in deterministic chronological order.

    Args:
        conn: Open DuckDB connection to the cache.
        chains: Restrict to these chains. None reads every chain, which is
            correct for a single-file cache; once ADR 0011's per-chain split
            lands, the caller will open one file per chain and this argument
            becomes the guard against reading the wrong one.
        wallet_filter: Restrict to transfers where one of these addresses is the
            sender or the receiver. None reads every wallet. Lowercased here,
            matching the classifier and the engine, so a checksummed address
            cannot silently match nothing — that would return an empty result
            for a wallet with real activity, which looks like "no data" rather
            than like a bug.

    Yields:
        ERC20Transfer models, oldest first. Rows with a NULL token_decimals are
        skipped — see below.

    Notes:
        A generator rather than a list. A wallet-universe-wide read is large
        (the current cache holds 242k transfers) and the consumer processes
        events one at a time into the FIFO engine, so materialising the whole
        set buys nothing and costs memory proportional to the backfill depth.
    """
    predicates: list[str] = []
    params: list[object] = []

    if chains is not None:
        if not chains:
            # An empty list means "no chains", which is a different request from
            # None meaning "every chain". Returning nothing is the honest reading;
            # falling through would silently widen the query to everything.
            logger.debug("chains=[] — no chains requested, returning no events")
            return
        placeholders = ", ".join("?" for _ in chains)
        predicates.append(f"chain IN ({placeholders})")
        params.extend(chains)

    if wallet_filter is not None:
        if not wallet_filter:
            logger.debug("wallet_filter=[] — no wallets requested, returning no events")
            return
        wallets = [w.strip().lower() for w in wallet_filter]
        placeholders = ", ".join("?" for _ in wallets)
        predicates.append(
            f"(lower(from_addr) IN ({placeholders}) OR lower(to_addr) IN ({placeholders}))"
        )
        params.extend(wallets)
        params.extend(wallets)

    where = f"WHERE {' AND '.join(predicates)}" if predicates else ""
    query = f"SELECT {_TRANSFER_COLUMNS} FROM erc20_transfer {where} ORDER BY {TRANSFER_ORDER_BY}"

    rows = conn.execute(query, params).fetchall()

    # Counted per (chain, token) rather than logged per row: a token missing
    # decimals is missing them for every one of its transfers, so a per-row
    # warning would emit thousands of identical lines and bury the signal.
    skipped: Counter[tuple[str, str]] = Counter()
    yielded = 0

    for row in rows:
        (
            chain,
            block_number,
            block_timestamp,
            tx_hash,
            log_index,
            unique_id,
            token_address,
            from_addr,
            to_addr,
            value_raw,
            token_decimals,
        ) = row

        if token_decimals is None:
            # Skipped rather than defaulted to 18. The decimals set the scale of
            # every amount, so a wrong value puts the whole position out by
            # orders of magnitude — and an 18 that happened to be right for most
            # tokens would make the handful it is wrong for invisible. The
            # engine requires decimals for the same reason.
            skipped[(chain, token_address)] += 1
            continue

        yield ERC20Transfer(
            chain=chain,
            block_number=block_number,
            block_timestamp=block_timestamp,
            tx_hash=tx_hash,
            log_index=log_index,
            unique_id=unique_id,
            token_address=token_address,
            from_addr=from_addr,
            to_addr=to_addr,
            value_raw=value_raw,
            token_decimals=token_decimals,
        )
        yielded += 1

    for (chain, token_address), count in sorted(skipped.items()):
        logger.warning(
            "Skipped %d transfer(s) with NULL token_decimals: chain=%s token=%s. "
            "Amounts cannot be scaled without decimals, and defaulting to 18 "
            "would silently misprice any token that is not 18-decimal. Re-run "
            "AW_02 for this token to populate them.",
            count,
            chain,
            token_address,
        )

    if skipped:
        logger.warning(
            "Total transfers skipped for missing decimals: %d across %d (chain, token) pair(s)",
            sum(skipped.values()),
            len(skipped),
        )
    logger.info("Read %d transfer event(s) in chronological order", yielded)
