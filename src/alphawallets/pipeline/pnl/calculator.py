"""PnL calculator orchestrator (ADR 0012 step 4).

This module will eventually drive the whole PnL computation: read events, resolve
prices, classify transfers, feed the FIFO engine, emit WalletPnL rows. **So far
it reads events, resolves prices, replays them through the classifier and the
engine, and books realizations for pool-destination sales** — no WalletPnL
emission and no window slicing yet.

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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from duckdb import DuckDBPyConnection

from alphawallets.config import Chain
from alphawallets.fetchers.erc20.models import ERC20Transfer
from alphawallets.pipeline.pnl.cost_basis import FIFOEngine, InsufficientBalanceError
from alphawallets.pipeline.pnl.models import Realization
from alphawallets.pipeline.pnl.transfer_treatment import (
    TransferTreatment,
    classify_transfer,
)
from alphawallets.tokens import V1_TOKENS

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


# ---------- Event replay ----------


# A transfer-OUT reduces the FIFO stack without realizing anything (ADR 0012
# decision 3), but the engine's only consumption API returns Realizations and
# requires a sale price to build them. We pass this sentinel and discard the
# result: the value never leaves the function, and OUT carries no cost basis by
# decision 3, so there is no number to get wrong. If a consume-without-realizing
# API is ever added to the engine, this is the call site to change.
_OUT_SALE_PRICE_UNUSED = 0.0

# Both treatments that consume from the stack. They differ only in what happens
# to the Realizations: a pool destination keeps them (ADR 0014), everything else
# discards them (ADR 0012 decision 3). Sharing one dispatch branch keeps the
# insufficient-balance handling in one place rather than two copies that can
# drift.
_OUTGOING_TREATMENTS = frozenset({TransferTreatment.OUT, TransferTreatment.TRADING_OUT_REALIZING})

PartitionKey = tuple[str, str, str]
"""(chain, wallet, token_address) — the grain ADR 0012 decision 8 computes at.

FIFO is only meaningful within one asset, and a wallet's UNI stack has nothing
to do with its AAVE stack, so one engine per triple. The leaderboard aggregates
to per-wallet at report time.
"""


@dataclass
class PartitionState:
    """One (chain, wallet, token) partition's engine plus what went wrong in it.

    Mutable, unlike every model in this package: it accumulates across the replay
    rather than describing a finished fact.

    The flags are raised here and consumed in step D, where they become columns
    on the WalletPnL row (ADR 0012 decisions 5, 6, 7). They are counted as well
    as flagged because "one unpriced event out of 500" and "480 out of 500" are
    very different statements about how much of a PnL figure to trust, and a
    bare boolean cannot tell them apart.
    """

    engine: FIFOEngine
    has_unpriceable_events: bool = False
    has_insufficient_balance: bool = False
    unpriced_event_count: int = 0
    insufficient_balance_count: int = 0
    event_count: int = 0

    realizations: list[Realization] = field(default_factory=list)
    """Realizations the wallet actually booked, in consumption order.

    Populated by transfers out to a known Uniswap V3 pool (ADR 0014), which are
    the input legs of swaps and therefore sales at a knowable price. One entry
    per lot consumed, so a sale spanning three lots appends three — and each
    carries the source of the lot it consumed, which is how ADR 0012 decision
    4's trading/airdrop split survives into realized PnL.

    Transfer-OUT to anything else contributes nothing, by decision 3. Step D
    slices this list by realized_at to produce per-window PnL (decision 9)."""


def _process_events(
    conn: DuckDBPyConnection,
    chains: list[Chain] | None = None,
    wallet_filter: list[str] | None = None,
) -> dict[PartitionKey, PartitionState]:
    """Replay every transfer through the classifier and the FIFO engine.

    Reads events in the deterministic order from _fetch_transfer_events, which is
    what satisfies the engine's chronological-ordering contract — the engine does
    not sort, and out-of-order events produce a wrong cost basis silently.

    Args:
        conn: Open DuckDB connection to the cache.
        chains: Restrict to these chains. Passed through to both the event read
            and the price load. None means every chain; [] means none.
        wallet_filter: Only build partitions for these wallets, any case. Applied
            at wallet selection rather than in SQL — pushing it into the query is
            a later optimisation, and doing it here keeps one definition of which
            wallets a transfer touches.

    Returns:
        Every partition the replay created, keyed by (chain, wallet, token).

    Notes:
        No WalletPnL emission and no window slicing. A partition's state is the
        running cost basis over all visible history, which is exactly what ADR
        0012 decision 9 specifies: one engine state, and windows differ only in
        which realizations they count.
    """
    price_cache = _load_price_cache(conn, chains=chains)
    allowed = {w.strip().lower() for w in wallet_filter} if wallet_filter is not None else None

    partitions: dict[PartitionKey, PartitionState] = {}
    ignored_count = 0
    decimals_conflicts = 0

    for transfer in _fetch_transfer_events(conn, chains=chains):
        # A set, not a pair: a self-transfer has from_addr == to_addr, and
        # processing it once per side would apply it twice to the same partition
        # — adding the lot, then adding it again. classify_transfer returns
        # SELF_TRANSFER for that wallet, which is the single correct treatment.
        for wallet in {transfer.from_addr, transfer.to_addr}:
            if allowed is not None and wallet not in allowed:
                continue

            key: PartitionKey = (transfer.chain, wallet, transfer.token_address)
            # One lookup, two possible roles. The token's market price at this
            # hour is the cost basis if the wallet received, and the sale price
            # if it sent to a pool (ADR 0014) — the same number either way, so
            # resolving it twice would only invite the two to drift apart. The
            # classifier decides which role applies and nulls the other.
            price = _lookup_price(
                price_cache, transfer.chain, transfer.token_address, transfer.block_timestamp
            )
            classification = classify_transfer(
                transfer=transfer,
                wallet=wallet,
                token_symbol=_token_symbol_for(transfer.chain, transfer.token_address),
                unit_cost_usd=price,
                unit_sale_usd=price,
            )

            if classification.treatment is TransferTreatment.IGNORED:
                # The wallet came from this transfer's own addresses, so it is
                # always a party — reaching here means the selection above and
                # the classifier disagree, which is a bug in one of them.
                ignored_count += 1
                logger.debug(
                    "IGNORED for wallet %s on %s — wallet selection and classifier disagree",
                    wallet,
                    transfer.unique_id,
                )
                continue

            state = partitions.get(key)
            if state is None:
                state = PartitionState(
                    engine=FIFOEngine(
                        wallet=wallet,
                        token_address=transfer.token_address,
                        # From the row rather than a default: ADR 0012's whole
                        # premise is that a wrong scale produces a plausible
                        # number, and _fetch_transfer_events has already dropped
                        # rows where this is NULL.
                        token_decimals=transfer.token_decimals,
                    )
                )
                partitions[key] = state
            elif state.engine.token_decimals != transfer.token_decimals:
                # Same token reporting two different decimals across rows. Not
                # fatal — the first value keeps the stack self-consistent — but
                # it means one set of amounts is misscaled, so it must not pass
                # silently.
                decimals_conflicts += 1
                logger.warning(
                    "token_decimals conflict for %s on %s: engine built with %d, "
                    "row %s reports %d. Keeping the engine's value; amounts from "
                    "one of the two are misscaled.",
                    transfer.token_address,
                    transfer.chain,
                    state.engine.token_decimals,
                    transfer.unique_id,
                    transfer.token_decimals,
                )

            state.event_count += 1
            qty = Decimal(transfer.value_raw)

            if classification.treatment in _OUTGOING_TREATMENTS:
                realizing = classification.treatment is TransferTreatment.TRADING_OUT_REALIZING
                sale_price = classification.unit_sale_usd

                if realizing and sale_price is None:
                    # A pool destination with no price for that hour. The stack
                    # must still be reduced — the wallet genuinely sent the
                    # tokens, and leaving them in place would both overstate the
                    # balance and let a later realization consume lots that were
                    # already gone, misattributing their cost basis. So this
                    # degrades to decision 3's treatment: consume, realize
                    # nothing, and flag, because the realization we could not
                    # price is a real gap in the PnL rather than a non-event.
                    realizing = False
                    state.has_unpriceable_events = True
                    state.unpriced_event_count += 1
                    logger.warning(
                        "Pool-destination transfer with no price: %s at %s — pool=%s "
                        "chain=%s wallet=%s token=%s. Stack reduced but no "
                        "realization recorded; this wallet's PnL is understated.",
                        transfer.unique_id,
                        transfer.block_timestamp.isoformat(),
                        transfer.to_addr,
                        transfer.chain,
                        wallet,
                        transfer.token_address,
                    )

                try:
                    realized = state.engine.consume(
                        realized_at=transfer.block_timestamp,
                        qty_token=qty,
                        # The real sale price for a priced pool destination; the
                        # sentinel for everything else, whose Realizations are
                        # discarded because decision 3 cannot know whether the
                        # wallet sold, paid, bridged or self-custodied.
                        unit_sale_usd=sale_price if realizing else _OUT_SALE_PRICE_UNUSED,
                    )
                except InsufficientBalanceError as e:
                    # Expected rather than exceptional: a wallet holding a
                    # balance before the indexed window starts will send tokens
                    # the stack never received. That is ADR 0012 decision 5's
                    # has_pre_window_activity signal arriving early — step D
                    # should read this flag as evidence for it.
                    state.has_insufficient_balance = True
                    state.insufficient_balance_count += 1
                    logger.warning(
                        "Insufficient balance on %s: chain=%s wallet=%s token=%s — %s",
                        transfer.unique_id,
                        transfer.chain,
                        wallet,
                        transfer.token_address,
                        e,
                    )
                    continue

                if realizing:
                    # ADR 0014: the counterparty is a pool, so these are real
                    # trades. One Realization per lot consumed, each carrying
                    # its lot's source, which is what keeps decision 4's
                    # trading/airdrop split working on the realization side.
                    state.realizations.extend(realized)
                continue

            # Every remaining treatment creates a lot. AIRDROP_IN carries a
            # forced 0.0; TRADING_IN and SELF_TRANSFER carry the resolved price.
            if classification.unit_cost_usd is None:
                # Decision 6: keep the event, flag the row, do not invent a cost.
                # MKR's 4-hour grid guarantees this path runs on live data.
                state.has_unpriceable_events = True
                state.unpriced_event_count += 1
                logger.debug(
                    "No price for %s at %s; lot skipped and partition flagged",
                    transfer.token_address,
                    transfer.block_timestamp.isoformat(),
                )
                continue

            assert classification.source is not None, "lot-creating treatment without a source"
            state.engine.add_lot(
                acquired_at=transfer.block_timestamp,
                qty_token=qty,
                unit_cost_usd=classification.unit_cost_usd,
                source=classification.source,
            )

    if ignored_count:
        logger.warning(
            "%d event(s) classified IGNORED despite the wallet coming from the "
            "transfer's own addresses — investigate wallet selection",
            ignored_count,
        )
    if decimals_conflicts:
        logger.warning("%d token_decimals conflict(s) across the replay", decimals_conflicts)

    flagged_unpriced = sum(1 for s in partitions.values() if s.has_unpriceable_events)
    flagged_balance = sum(1 for s in partitions.values() if s.has_insufficient_balance)
    logger.info(
        "Replayed into %d partition(s): %d with unpriced events, %d with insufficient balance",
        len(partitions),
        flagged_unpriced,
        flagged_balance,
    )
    return partitions


def _token_symbol_for(chain: str, token_address: str) -> str:
    """Resolve a token address to its V1 registry symbol for the airdrop lookup.

    classify_transfer keys the airdrop registry on symbol and chain, so the
    address has to be mapped. An unknown address returns a sentinel that matches
    no registry entry, which routes the transfer to TRADING_IN — the correct
    default for a token with no airdrop in V1 scope.
    """
    for symbol, addresses in V1_TOKENS.items():
        if addresses.get(chain) == token_address.lower():
            return symbol
    return "__UNKNOWN__"
