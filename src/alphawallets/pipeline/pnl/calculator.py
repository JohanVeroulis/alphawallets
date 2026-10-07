"""PnL calculator orchestrator (ADR 0012 step 4).

This module will eventually drive the whole PnL computation: read events, resolve
prices, classify transfers, feed the FIFO engine, emit WalletPnL rows. **So far
it contains the data-reading and price layers only** — no FIFO logic, no
classification, no WalletPnL emission.

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
from datetime import UTC, datetime

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


# ---------- Price layer ----------


# Which provider wins when more than one has a price for the same hour.
#
# token_price's primary key is (chain, token_address, ts, source), so two
# providers can legitimately hold a row for one hour — that is exactly what the
# four-column key reserves space for (ADR 0008 keeps CoinGecko in reserve, and
# ADR 0010 kept `source` meaning *provider* rather than *route* for this reason).
#
# A dict keyed on (chain, token, hour) therefore has to choose, and choosing by
# scan order would make the PnL depend on DuckDB's row order. Preference is
# explicit and lowest-index-wins; an unlisted source sorts after every listed
# one, so adding a provider without updating this list degrades to "deprioritised"
# rather than to "undefined".
_SOURCE_PREFERENCE: tuple[str, ...] = ("defillama",)

PriceCacheKey = tuple[str, str, datetime]


def _hour_utc(ts: datetime) -> datetime:
    """Truncate a timestamp to its UTC hour — the price grid's join key.

    Done in Python rather than SQL so the key built at load time and the key
    built at lookup time come from the same code path. Deliberately a local
    helper rather than an import from pipeline/exploration: a few lines of
    duplication is cheaper than coupling two sibling pipeline packages, and this
    is the kind of function that must not change under one caller without the
    other noticing.
    """
    return ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _load_price_cache(
    conn: DuckDBPyConnection,
    chains: list[Chain] | None = None,
    tokens: list[str] | None = None,
) -> dict[PriceCacheKey, float]:
    """Load hourly prices into a dict for in-memory lookup.

    One query instead of one per event. The PnL calculator prices every
    acquisition and every realization, so a per-event query would be tens of
    thousands of round trips against a table small enough to hold in memory —
    the current cache holds 2,893 price rows.

    Args:
        conn: Open DuckDB connection to the cache.
        chains: Restrict to these chains. None loads every chain; [] loads
            nothing, matching _fetch_transfer_events' semantics.
        tokens: Restrict to these token addresses, any case. None loads every
            token; [] loads nothing.

    Returns:
        A mapping from (chain, token_address, hour) to price_usd, with the hour
        truncated to UTC. Hours with no price are simply absent — the lookup
        treats an absent key as "no price", which is the distinction ADR 0009's
        classification depends on.
    """
    predicates: list[str] = []
    params: list[object] = []

    if chains is not None:
        if not chains:
            logger.debug("chains=[] — no chains requested, price cache is empty")
            return {}
        placeholders = ", ".join("?" for _ in chains)
        predicates.append(f"chain IN ({placeholders})")
        params.extend(chains)

    if tokens is not None:
        if not tokens:
            logger.debug("tokens=[] — no tokens requested, price cache is empty")
            return {}
        lowered = [t.strip().lower() for t in tokens]
        placeholders = ", ".join("?" for _ in lowered)
        predicates.append(f"lower(token_address) IN ({placeholders})")
        params.extend(lowered)

    where = f"WHERE {' AND '.join(predicates)}" if predicates else ""
    rows = conn.execute(
        f"SELECT chain, token_address, ts, price_usd, source FROM token_price {where}",
        params,
    ).fetchall()

    cache: dict[PriceCacheKey, float] = {}
    chosen_source: dict[PriceCacheKey, str] = {}
    multi_source_keys = 0

    for chain, token_address, ts, price_usd, source in rows:
        key = (chain, token_address.lower(), _hour_utc(ts))
        incumbent = chosen_source.get(key)
        if incumbent is None:
            cache[key] = float(price_usd)
            chosen_source[key] = source
            continue

        multi_source_keys += 1
        if _source_rank(source) < _source_rank(incumbent):
            cache[key] = float(price_usd)
            chosen_source[key] = source

    if multi_source_keys:
        logger.info(
            "%d price hour(s) had more than one source; resolved by preference %s",
            multi_source_keys,
            list(_SOURCE_PREFERENCE),
        )

    pairs = {(chain, token) for chain, token, _hour in cache}
    logger.info(
        "Loaded %d price hour(s) across %d (chain, token) pair(s)",
        len(cache),
        len(pairs),
    )
    return cache


def _source_rank(source: str) -> int:
    """Preference index of a price source; unlisted sources sort last."""
    try:
        return _SOURCE_PREFERENCE.index(source)
    except ValueError:
        return len(_SOURCE_PREFERENCE)


def _lookup_price(
    cache: dict[PriceCacheKey, float],
    chain: str,
    token_address: str,
    event_ts: datetime,
) -> float | None:
    """Return the USD price for an event's hour, or None when there is none.

    None is a real answer, not an error. ADR 0012 decision 6 keeps an unpriced
    event in the output and flags the row, rather than dropping it or inventing
    a price — and MKR's 4-hour provider grid (PR #31, proposed as ADR 0013)
    guarantees this path is exercised on live data rather than only in theory.

    Args:
        cache: The mapping from _load_price_cache.
        chain: Chain name.
        token_address: Token contract, any case.
        event_ts: The event's timestamp. Truncated to its UTC hour here, by the
            same helper that built the keys.

    Returns:
        The price, or None when that hour has no row.
    """
    return cache.get((chain, token_address.lower(), _hour_utc(event_ts)))
