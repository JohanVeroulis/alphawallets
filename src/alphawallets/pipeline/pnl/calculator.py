"""PnL calculator orchestrator (ADR 0012 step 4).

This module will eventually drive the whole PnL computation: read events, resolve
prices, classify transfers, feed the FIFO engine, emit WalletPnL rows — and
`compute_wallet_pnl` now does all of it, end to end, returning rows ready for a
writer.

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

import heapq
import logging
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from duckdb import CatalogException, DuckDBPyConnection

from alphawallets.config import Chain
from alphawallets.fetchers.erc20.models import ERC20Transfer
from alphawallets.fetchers.uniswap_v3.models import UniswapV3Swap
from alphawallets.pipeline.pnl.cost_basis import FIFOEngine, InsufficientBalanceError
from alphawallets.pipeline.pnl.known_pools import get_pool_layout, get_pool_tokens
from alphawallets.pipeline.pnl.models import Realization, WalletPnL
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


@dataclass(frozen=True)
class LotEvent:
    """One acquisition as it was applied, kept so windows can be sliced later.

    bought_usd is a per-window figure, and the engine's lots cannot answer it:
    a lot that has been fully consumed is gone from the stack, and a partial one
    no longer carries the quantity originally acquired. Recording the event as it
    happens is both cheaper and more honest than reconstructing it.

    unit_cost_usd is None when the acquisition could not be priced. The event is
    still recorded — it is why the partition is flagged, and dropping it would
    lose the timestamp the per-window flag needs.
    """

    occurred_at: datetime
    unit_cost_usd: float | None
    qty_token: Decimal


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

    transfer_event_count: int = 0
    """Events applied from erc20_transfer."""

    swap_event_count: int = 0
    """Events applied from uniswap_v3_swap.

    Counted separately from transfers rather than inferred from event_count
    because ADR 0016's signal is precisely the ratio between the two, and a
    single total cannot express "traded this token but never held it"."""

    in_events: list[LotEvent] = field(default_factory=list)
    """Every acquisition applied to this partition, in chronological order.

    Feeds bought_usd per window, and its timestamps make has_unpriceable_events
    a per-window answer rather than a partition-wide approximation."""

    first_event_at: datetime | None = None
    """Earliest event seen. An event before a window's start means the wallet
    already held a position when the window opened, which is ADR 0012 decision
    5's has_pre_window_activity."""

    realizations: list[Realization] = field(default_factory=list)
    """Realizations the wallet actually booked, in consumption order.

    Populated by transfers out to a known Uniswap V3 pool (ADR 0014), which are
    the input legs of swaps and therefore sales at a knowable price. One entry
    per lot consumed, so a sale spanning three lots appends three — and each
    carries the source of the lot it consumed, which is how ADR 0012 decision
    4's trading/airdrop split survives into realized PnL.

    Transfer-OUT to anything else contributes nothing, by decision 3. Step D
    slices this list by realized_at to produce per-window PnL (decision 9)."""

    @property
    def has_smart_wallet_signal(self) -> bool:
        """True when this partition traded the token but never held it.

        ADR 0016. Zero events from `erc20_transfer` and at least one from
        `uniswap_v3_swap` means the wallet appears only as a swap's `tx_from` —
        it signed the transactions, but the token never entered or left its
        balance. For contract-mediated trading (arb bots, MEV searchers,
        aggregator executors) the custody address is the contract, not the
        signer, so the PnL is arithmetically correct for the wrong subject.

        Derived rather than assigned, for the same reason `has_pre_window_activity`
        is: the merge is streaming, so a partition is only "finished" once every
        event has been read, and a flag set mid-replay could be set from an
        incomplete picture. Reading it at emission time means one definition
        evaluated against the final counters.

        Example:
            >>> from alphawallets.pipeline.pnl.cost_basis import FIFOEngine
            >>> s = PartitionState(engine=FIFOEngine("0xabc", "0xdef", 18))
            >>> s.swap_event_count = 3
            >>> s.has_smart_wallet_signal
            True
            >>> s.transfer_event_count = 1
            >>> s.has_smart_wallet_signal
            False
        """
        return self.transfer_event_count == 0 and self.swap_event_count > 0


def _merge_sort_key(event: ERC20Transfer | UniswapV3Swap) -> tuple:
    """Chronological sort key shared by both event streams.

    The FIFO engine does not sort (PR #33), so merging two already-ordered
    streams has to preserve a single total order across both — the same
    discipline as TRANSFER_ORDER_BY, expressed in Python because the merge
    happens outside SQL.

    log_index is NULL on every transfer row (ADR 0007) and never NULL on a swap,
    so the (0, index) / (1, 0) pair reproduces NULLS LAST: within one block,
    indexed swaps come before unindexed transfers rather than in whatever order
    the merge happened to see them.

    The kind discriminator and the identifier make the order total. Without them
    a swap and a transfer tying on timestamp, block and index would merge
    non-deterministically, and the FIFO cost basis would differ between runs over
    the same cache — the failure the unique_id tiebreak exists to prevent on the
    transfer side.
    """
    if isinstance(event, UniswapV3Swap):
        return (event.block_timestamp, event.block_number, (0, event.log_index), 0, event.tx_hash)
    log_sort = (0, event.log_index) if event.log_index is not None else (1, 0)
    return (event.block_timestamp, event.block_number, log_sort, 1, event.unique_id)


def _swap_sides(swap: UniswapV3Swap) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Split a swap into (sold side, bought side), or None if the signs are wrong.

    Uniswap signs amounts from the pool's perspective: positive means the token
    flowed INTO the pool, so the wallet sold it; negative means it flowed out, so
    the wallet bought it. Exactly one of the two is positive in a well-formed
    swap.

    Returns None when that invariant does not hold — both signs equal, or an
    amount of zero. Skipping is the only safe response: there is no way to tell
    which side was sold, and guessing would invert a trade's direction.
    """
    layout = get_pool_layout(swap.chain, swap.pool_address)
    slots = (
        (swap.amount0, layout["token0"]),
        (swap.amount1, layout["token1"]),
    )
    positive = [(amount, meta) for amount, meta in slots if amount > 0]
    negative = [(amount, meta) for amount, meta in slots if amount < 0]
    if len(positive) != 1 or len(negative) != 1:
        return None

    sold_amount, sold_meta = positive[0]
    bought_amount, bought_meta = negative[0]
    return (
        {"amount": sold_amount, **sold_meta},
        {"amount": -bought_amount, **bought_meta},
    )


# Tokens whose hourly price anchors a swap's executed ratio. Every V1 pool is
# TOKEN/WETH (PR #28), and ADR 0015 relies on the quote side being
# "grid-reliable" — a deep, continuously-traded asset whose hourly price is a
# good estimate of its price at any instant inside that hour. USDC is listed for
# the reference pool and for future non-WETH pairs.
_QUOTE_SYMBOLS: frozenset[str] = frozenset({"WETH", "USDC"})


def _whole(side: dict[str, Any]) -> Decimal:
    """A side's amount in whole tokens, decimal-adjusted."""
    return Decimal(side["amount"]) / (Decimal(10) ** side["decimals"])


def _price_sides(
    sold: dict[str, Any],
    bought: dict[str, Any],
    price_cache: dict[PriceCacheKey, float],
    chain: str,
    event_ts: datetime,
) -> tuple[float, float] | None:
    """Price both sides of a swap from one grid anchor and the executed ratio.

    This is the point of ADR 0015. The hourly grid gives one price per hour, so
    an intra-hour buy and sell collapse to the same number and PnL comes out
    exactly zero — 65.4% of realizations in PR #42's measurement. The ratio the
    swap actually executed at recovers that movement.

    **The anchor is the quote side, not the bought side.** Anchoring on whichever
    token the wallet received discards the executed ratio in the buy direction:
    a wallet paying 1.5 WETH for 500 UNI with WETH at $2,000 and UNI at $12 on
    the grid really paid $3,000, so its cost basis is $6.00 per UNI — but
    pricing the bought side from the grid books $12.00, a $6,000 cost it never
    paid, and silently throws away exactly the information this ADR exists to
    use. Anchoring on the quote side prices both legs from one trusted number
    and the ratio.

    Returns:
        (sold_unit_usd, bought_unit_usd), or None when no anchor can be priced.

    All Decimal: the quantities are uint256-scale, and the division is where
    float error would compound into the number that lands in realized PnL.
    """
    # Prefer a quote asset; fall back to whichever side has a price at all, so a
    # future non-quote pair still prices rather than failing.
    candidates = [side for side in (sold, bought) if side["symbol"].upper() in _QUOTE_SYMBOLS] or [
        sold,
        bought,
    ]

    for anchor in candidates:
        anchor_usd = _lookup_price(price_cache, chain, anchor["address"], event_ts)
        if anchor_usd is None:
            continue
        other = bought if anchor is sold else sold
        other_usd = float(_whole(anchor) * Decimal(str(anchor_usd)) / _whole(other))
        if anchor is sold:
            return anchor_usd, other_usd
        return other_usd, anchor_usd

    return None


def _process_events(
    conn: DuckDBPyConnection,
    chains: list[Chain] | None = None,
    wallet_filter: list[str] | None = None,
    as_of: datetime | None = None,
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
        as_of: Stop replaying at this instant, exclusive. None replays
            everything. Events after it are not applied at all, rather than
            applied and filtered later: the engine's state is a running cost
            basis, so letting a future event into the stack would change the
            lots an in-window sale consumes.

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

    # Swaps are read in full first: the dedup set has to exist before the first
    # transfer is examined, since a transfer leg cannot be recognised as one
    # until its swap is known (ADR 0015). The swap table is three orders of
    # magnitude smaller than the transfer table, so this is cheap.
    swaps = list(_fetch_swap_events(conn, chains=chains))
    dedup_set = _build_swap_dedup_set(swaps)
    transfers = _filter_transfers_by_dedup(_fetch_transfer_events(conn, chains=chains), dedup_set)

    # heapq.merge over two already-sorted streams, rather than sorting their
    # concatenation: both readers emit in order, so the merge is linear and the
    # transfer side stays lazy.
    merged = heapq.merge(swaps, transfers, key=_merge_sort_key)

    for event in merged:
        if as_of is not None and event.block_timestamp >= as_of:
            # Events are chronologically ordered, so the first one at or past
            # the cutoff means every remaining one is too. window_end is
            # exclusive (WalletPnL), so the boundary instant itself is excluded.
            break

        if isinstance(event, UniswapV3Swap):
            _apply_swap(event, partitions, price_cache, allowed)
            continue

        transfer = event

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
            state.transfer_event_count += 1
            if state.first_event_at is None:
                # Events arrive chronologically (PR #35), so the first one seen
                # is the earliest — no min() needed, and relying on the order
                # keeps the reader's contract load-bearing rather than decorative.
                state.first_event_at = transfer.block_timestamp
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
                    #
                    # NOT dead code under ADR 0015. The dedup set only removes
                    # transfers whose swap is IN the cache, so a transfer to a
                    # known pool with no corresponding swap row — a fetcher gap,
                    # a decoding failure, AW_01 and AW_02 covering different
                    # block ranges — still reaches here and is priced on the
                    # hourly grid. This branch is the fallback for exactly the
                    # case where the executed price is unavailable.
                    state.realizations.extend(realized)
                continue

            # Every remaining treatment creates a lot. AIRDROP_IN carries a
            # forced 0.0; TRADING_IN and SELF_TRANSFER carry the resolved price.
            if classification.unit_cost_usd is None:
                # Decision 6: keep the event, flag the row, do not invent a cost.
                # MKR's 4-hour grid guarantees this path runs on live data.
                state.has_unpriceable_events = True
                state.unpriced_event_count += 1
                # Recorded despite creating no lot: the timestamp is what makes
                # the per-window flag precise instead of partition-wide.
                state.in_events.append(
                    LotEvent(
                        occurred_at=transfer.block_timestamp,
                        unit_cost_usd=None,
                        qty_token=qty,
                    )
                )
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
            state.in_events.append(
                LotEvent(
                    occurred_at=transfer.block_timestamp,
                    unit_cost_usd=classification.unit_cost_usd,
                    qty_token=qty,
                )
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


# ---------- Window emission ----------


WINDOW_DAYS: tuple[int, ...] = (30, 90)
"""The PnL windows V1 reports.

ADR 0012 decision 9: both windows share one engine state and differ only in
which Realizations they count. The cost basis is *not* reset at a window
boundary — a wallet that bought 60 days ago and sold 20 days ago shows the real
$10 cost in its 30-day row, not a zero cost that books the whole sale as profit.
"""


def compute_wallet_pnl(
    conn: DuckDBPyConnection,
    *,
    as_of: datetime | None = None,
    wallet_filter: list[str] | None = None,
    chains: list[Chain] | None = None,
) -> Iterator[WalletPnL]:
    """Compute per-(wallet, token, window) PnL rows from the cache.

    The end-to-end orchestrator: reads events, resolves prices, replays them
    through the classifier and the FIFO engine, then slices the resulting
    realizations into windows.

    Args:
        conn: Open DuckDB connection to the cache.
        as_of: The instant every window ends at, exclusive. Defaults to the
            newest block_timestamp in erc20_transfer — the edge of what we
            actually know, rather than wall-clock now, which would open a
            trailing gap between the last indexed block and the window end and
            make the result depend on when it was run.
        wallet_filter: Only compute these wallets, any case.
        chains: Restrict to these chains. None means every chain; [] means none.

    Yields:
        One WalletPnL per (partition, window) that had activity in that window.
        Nothing at all when the cache holds no transfers.

    Notes:
        A generator. The writer consumes incrementally, and a universe-wide run
        produces two rows per (wallet, token) pair — materialising them all
        before the first write buys nothing.
    """
    if as_of is None:
        resolved = _resolve_as_of(conn, chains=chains)
        if resolved is None:
            logger.info("No transfers in the cache; no PnL to compute")
            return
        as_of = resolved
        logger.info("as_of defaulted to the newest indexed block: %s", as_of.isoformat())

    partitions = _process_events(conn, chains=chains, wallet_filter=wallet_filter, as_of=as_of)

    emitted = 0
    for key, state in sorted(partitions.items()):
        for row in _emit_pnl_rows(key, state, as_of):
            emitted += 1
            yield row

    logger.info("Emitted %d WalletPnL row(s) from %d partition(s)", emitted, len(partitions))


def _resolve_as_of(
    conn: DuckDBPyConnection,
    chains: list[Chain] | None = None,
) -> datetime | None:
    """Return an as_of that includes every indexed event, or None when empty.

    The newest block timestamp plus one microsecond, not the timestamp itself.
    window_end is exclusive, so defaulting to MAX(block_timestamp) exactly would
    exclude every event in the newest block — and for a cache whose events all
    share one timestamp it would exclude everything and report no activity at
    all. "As of the data edge" has to mean "including the edge", so the bound
    sits just past it.

    An explicitly passed as_of keeps strict exclusive semantics: a caller naming
    an instant means up to but not including it.

    Scoped to the same chains the run covers, so a Base-only run is not pinned
    to Ethereum's head.
    """
    if chains is not None and not chains:
        return None

    where, params = "", []
    if chains is not None:
        placeholders = ", ".join("?" for _ in chains)
        where = f"WHERE chain IN ({placeholders})"
        params = list(chains)

    row = conn.execute(
        f"SELECT MAX(block_timestamp) FROM erc20_transfer {where}", params
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return row[0].astimezone(UTC) + timedelta(microseconds=1)


def _emit_pnl_rows(
    key: PartitionKey,
    state: PartitionState,
    as_of: datetime,
) -> Iterator[WalletPnL]:
    """Slice one partition's history into a row per window.

    Args:
        key: (chain, wallet, token_address).
        state: The replayed partition.
        as_of: Window end, shared by every window and exclusive.

    Yields:
        One WalletPnL per window with activity. A window with neither a
        realization nor an acquisition is skipped rather than emitted as a row
        of zeroes — an all-zero row and "this wallet did nothing here" are the
        same fact, and writing it would pad the table and the leaderboard with
        rows carrying no information.
    """
    chain, wallet, token_address = key
    divisor = Decimal(10) ** state.engine.token_decimals
    computed_at = datetime.now(tz=UTC)

    # Balance state is identical across windows: every window ends at as_of, so
    # the engine's final state *is* the state at window_end. Computed once.
    balance = state.engine.balance_token()
    avg_cost = state.engine.avg_cost_basis_usd()

    for days in WINDOW_DAYS:
        window_start = as_of - timedelta(days=days)

        realizations = [r for r in state.realizations if window_start <= r.realized_at < as_of]
        acquisitions = [e for e in state.in_events if window_start <= e.occurred_at < as_of]

        if not realizations and not acquisitions:
            continue

        trading = sum(r.pnl_usd for r in realizations if r.source == "trading")
        airdrop = sum(r.pnl_usd for r in realizations if r.source == "airdrop")
        sold = sum(Decimal(str(r.unit_sale_usd)) * (r.qty_token / divisor) for r in realizations)
        bought = sum(
            Decimal(str(e.unit_cost_usd)) * (e.qty_token / divisor)
            for e in acquisitions
            if e.unit_cost_usd is not None
        )

        # Per-window rather than partition-wide: an unpriced event three months
        # before a 30-day window says nothing about that window's completeness,
        # and flagging it anyway would make the caveat useless by making it
        # almost always true.
        unpriced_in_window = any(e.unit_cost_usd is None for e in acquisitions)

        # Two independent signals for the same condition. An event earlier than
        # the window means the wallet already held a position when it opened.
        # An insufficient balance means it sent tokens the stack never received,
        # which can only happen if it acquired them before our data starts —
        # so it is evidence of pre-window activity even when every visible event
        # falls inside the window.
        earlier_event = state.first_event_at is not None and state.first_event_at < window_start
        has_pre_window = earlier_event or state.has_insufficient_balance

        yield WalletPnL(
            chain=chain,
            wallet=wallet,
            token_address=token_address,
            window_start=window_start,
            window_end=as_of,
            realized_pnl_usd=float(trading + airdrop),
            realized_pnl_trading_usd=float(trading),
            realized_pnl_airdrop_usd=float(airdrop),
            # V1 ships realized PnL only; the column is reserved (ADR 0012).
            unrealized_pnl_usd=None,
            bought_usd=float(bought),
            sold_usd=float(sold),
            realization_count=len(realizations),
            balance_token=balance,
            avg_cost_basis_usd=avg_cost,
            has_pre_window_activity=has_pre_window,
            has_unpriceable_events=unpriced_in_window,
            # ADR 0016 activates decision 7's flag: a partition that traded
            # the token but never held it is a signing EOA, not the holder.
            has_smart_wallet_signal=state.has_smart_wallet_signal,
            computed_at=computed_at,
        )


# ---------- Swap event source (ADR 0015) ----------


# Deterministic chronological order for swaps. Same discipline as
# TRANSFER_ORDER_BY, one clause shorter: uniswap_v3_swap.log_index is NOT NULL,
# because AW_01 decodes from eth_getLogs where the index is always present —
# unlike AW_02's Transfers API rows, which carry None (ADR 0007). So no
# NULLS LAST is needed, and log_index alone separates events within a block.
#
# tx_hash is the final tiebreak, making the order total: (chain, tx_hash,
# log_index) is this table's uniqueness, so no two rows can tie on all four
# clauses and two runs over the same cache produce identical order. Without it a
# block holding two swaps at the same log index — impossible on chain, but the
# schema permits it across pools — would come back in scan order.
SWAP_ORDER_BY = "block_timestamp, block_number, log_index, tx_hash"

_SWAP_COLUMNS = (
    "chain, block_number, block_timestamp, tx_hash, log_index, pool_address, "
    "tx_from, sender, recipient, amount0, amount1, sqrt_price_x96, liquidity, tick"
)


def _fetch_swap_events(
    conn: DuckDBPyConnection,
    chains: list[Chain] | None = None,
    wallet_filter: list[str] | None = None,
) -> Iterator[UniswapV3Swap]:
    """Yield Uniswap V3 swaps in deterministic chronological order.

    The second event source ADR 0015 adds. A swap row is self-sufficient — it
    carries the trader EOA, the executed amounts, the pool and the executed
    price — so pricing a trade needs no join to the transfer legs it emitted.

    Args:
        conn: Open DuckDB connection to the cache.
        chains: Restrict to these chains. None reads every chain; [] reads
            nothing, matching _fetch_transfer_events.
        wallet_filter: Restrict to swaps submitted by these addresses, matched on
            tx_from — the real EOA, not sender/recipient, which for a
            router-mediated trade are the router and the pool (PR #15). None
            reads every wallet; [] reads nothing.

    Yields:
        UniswapV3Swap models, oldest first.
    """
    predicates: list[str] = []
    params: list[object] = []

    if chains is not None:
        if not chains:
            logger.debug("chains=[] — no chains requested, returning no swaps")
            return
        placeholders = ", ".join("?" for _ in chains)
        predicates.append(f"chain IN ({placeholders})")
        params.extend(chains)

    if wallet_filter is not None:
        if not wallet_filter:
            logger.debug("wallet_filter=[] — no wallets requested, returning no swaps")
            return
        wallets = [w.strip().lower() for w in wallet_filter]
        placeholders = ", ".join("?" for _ in wallets)
        predicates.append(f"lower(tx_from) IN ({placeholders})")
        params.extend(wallets)

    where = f"WHERE {' AND '.join(predicates)}" if predicates else ""
    try:
        rows = conn.execute(
            f"SELECT {_SWAP_COLUMNS} FROM uniswap_v3_swap {where} ORDER BY {SWAP_ORDER_BY}",
            params,
        ).fetchall()
    except CatalogException:
        # No swap table: a cache where AW_02 has run and AW_01 has not, which is
        # a legitimate state rather than an error. Yielding nothing degrades the
        # run to transfer-only pricing — the pre-ADR-0015 behaviour — instead of
        # failing. A pipeline stage should not crash because a fetcher has not
        # run, which is the same reasoning route_cache.get_route follows.
        logger.info("uniswap_v3_swap does not exist; no swap events to read")
        return

    per_chain: Counter[str] = Counter()
    for row in rows:
        per_chain[row[0]] += 1
        yield UniswapV3Swap(
            chain=row[0],
            block_number=row[1],
            block_timestamp=row[2],
            tx_hash=row[3],
            log_index=row[4],
            pool_address=row[5],
            tx_from=row[6],
            sender=row[7],
            recipient=row[8],
            # Stored as VARCHAR because uint256 overflows HUGEINT; the model
            # parses them back to int.
            amount0=int(row[9]),
            amount1=int(row[10]),
            sqrt_price_x96=int(row[11]),
            liquidity=int(row[12]),
            tick=row[13],
        )

    logger.info(
        "Read %d swap event(s) in chronological order; per chain %s",
        sum(per_chain.values()),
        dict(sorted(per_chain.items())),
    )


# ---------- Transfer-leg deduplication (ADR 0015) ----------


DedupKey = tuple[str, str]
"""(tx_hash, token_address) — the grain transfer legs can be excluded at.

Not (tx_hash, token_address, log_index), because erc20_transfer.log_index is
NULL for every row: Alchemy's Transfers API omits it for the ERC-20 category
(ADR 0007), which is why that table's primary key is (chain, unique_id). ADR
0015 records the consequence — the key is coarser than a log, so a transaction
that both swaps and plainly transfers the same token loses the plain transfer
too. Known, accepted, and tested.
"""


def _build_swap_dedup_set(swaps: Iterable[UniswapV3Swap]) -> set[DedupKey]:
    """Collect the (tx_hash, token) pairs that swaps already account for.

    A V3 swap emits two ERC-20 Transfer events, one per side, and AW_02 reads
    both. Once swaps are an event source those legs must be excluded from the
    transfer stream, or the same trade is counted twice — once at its executed
    price and once at the hourly grid price it was wrong about (ADR 0015).

    Both sides of the pool are added, not just the tracked token: a swap moves
    both, and the leg we are not pricing is still a transfer AW_02 recorded.

    Args:
        swaps: Swap rows, in any order.

    Returns:
        A materialised set. The transfer stream tests every event against it, so
        a generator would be consumed on the first lookup.

    Raises:
        KeyError: Via get_pool_tokens, if a swap's pool is not in the layout.
            Guessing the pair would deduplicate the wrong transfers — dropping
            real events, or keeping double-counted ones — so this fails loudly.
    """
    dedup: set[DedupKey] = set()
    pools_seen: set[str] = set()

    for swap in swaps:
        token0, token1 = get_pool_tokens(swap.chain, swap.pool_address)
        dedup.add((swap.tx_hash, token0))
        dedup.add((swap.tx_hash, token1))
        pools_seen.add(swap.pool_address)

    logger.info(
        "Dedup set: %d (tx_hash, token) pair(s) from swaps across %d pool(s)",
        len(dedup),
        len(pools_seen),
    )
    return dedup


def _filter_transfers_by_dedup(
    transfers: Iterable[ERC20Transfer],
    dedup_set: set[DedupKey],
) -> Iterator[ERC20Transfer]:
    """Yield transfers that a swap row does not already account for.

    Args:
        transfers: The transfer event stream, in chronological order.
        dedup_set: From _build_swap_dedup_set.

    Yields:
        Transfers whose (tx_hash, token_address) is not in the set, in the order
        received — the engine's chronological contract survives the filter.
    """
    kept = 0
    dropped = 0

    for transfer in transfers:
        if (transfer.tx_hash, transfer.token_address) in dedup_set:
            dropped += 1
            continue
        kept += 1
        yield transfer

    total = kept + dropped
    share = (100.0 * dropped / total) if total else 0.0
    logger.info(
        "Transfer dedup: %d kept, %d dropped as swap legs (%.1f%% of %d)",
        kept,
        dropped,
        share,
        total,
    )


def _apply_swap(
    swap: UniswapV3Swap,
    partitions: dict[PartitionKey, PartitionState],
    price_cache: dict[PriceCacheKey, float],
    allowed: set[str] | None,
) -> None:
    """Apply one swap to the selling and buying partitions.

    ADR 0015: the trade is priced at what it executed at, not at the hour it
    happened in. The sold side consumes from its FIFO stack and keeps the
    Realizations; the bought side opens a lot.

    The wallet is `tx_from` — the transaction *signer*, which for
    contract-mediated trading is not the token holder. V1 computes per-signer
    and flags the partition (ADR 0016) rather than resolving custody, which
    needs the transfer legs dedup drops plus a refetch for contract addresses.
    """
    wallet = swap.tx_from
    if allowed is not None and wallet not in allowed:
        return

    sides = _swap_sides(swap)
    if sides is None:
        logger.warning(
            "Swap %s:%d has amounts that are not one positive and one negative "
            "(amount0=%d, amount1=%d); skipped, because there is no way to tell "
            "which side was sold",
            swap.tx_hash,
            swap.log_index,
            swap.amount0,
            swap.amount1,
        )
        return

    sold, bought = sides
    priced = _price_sides(sold, bought, price_cache, swap.chain, swap.block_timestamp)

    sold_state = _ensure_partition(
        partitions, (swap.chain, wallet, sold["address"]), wallet, sold["decimals"]
    )
    bought_state = _ensure_partition(
        partitions, (swap.chain, wallet, bought["address"]), wallet, bought["decimals"]
    )
    for state in (sold_state, bought_state):
        state.event_count += 1
        state.swap_event_count += 1
        if state.first_event_at is None:
            state.first_event_at = swap.block_timestamp

    sold_qty = Decimal(sold["amount"])
    bought_qty = Decimal(bought["amount"])

    if priced is None:
        # Neither side has an hourly price, so the executed ratio has nothing to
        # anchor to and neither leg can be valued.
        #
        # The sold side is still CONSUMED, deliberately diverging from the
        # instruction to skip the swap entirely. Dedup has already removed both
        # transfer legs from the stream, so nothing else will reduce the stack —
        # skipping would leave the wallet holding tokens it has sold, overstating
        # the balance and letting a later realization consume lots that were
        # already gone. Same resolution as the unpriced pool-destination OUT in
        # PR #40: reduce the stack, realize nothing, flag loudly.
        #
        # The bought side opens no lot, matching ADR 0012 decision 6's treatment
        # of an unpriced acquisition: record the event, flag the partition, never
        # invent a cost.
        for state in (sold_state, bought_state):
            state.has_unpriceable_events = True
            state.unpriced_event_count += 1
        bought_state.in_events.append(
            LotEvent(occurred_at=swap.block_timestamp, unit_cost_usd=None, qty_token=bought_qty)
        )
        logger.warning(
            "Swap %s:%d has no hourly price on either side (%s / %s on %s), so the "
            "executed ratio cannot be anchored. Sold side consumed without a "
            "realization and bought side opens no lot; both partitions flagged. "
            "This wallet's PnL is understated.",
            swap.tx_hash,
            swap.log_index,
            sold["symbol"],
            bought["symbol"],
            swap.chain,
        )
        _consume_or_flag(
            sold_state, swap, wallet, sold["address"], sold_qty, _OUT_SALE_PRICE_UNUSED, keep=False
        )
        return

    sold_unit_usd, bought_unit_usd = priced

    _consume_or_flag(sold_state, swap, wallet, sold["address"], sold_qty, sold_unit_usd, keep=True)

    # Both legs are priced from the same anchor and the same ratio, so the trade
    # values identically from either side: qty_sold * sold_unit_usd equals
    # qty_bought * bought_unit_usd by construction.
    bought_state.engine.add_lot(
        acquired_at=swap.block_timestamp,
        qty_token=bought_qty,
        unit_cost_usd=bought_unit_usd,
        source="trading",
    )
    bought_state.in_events.append(
        LotEvent(
            occurred_at=swap.block_timestamp,
            unit_cost_usd=bought_unit_usd,
            qty_token=bought_qty,
        )
    )


def _ensure_partition(
    partitions: dict[PartitionKey, PartitionState],
    key: PartitionKey,
    wallet: str,
    token_decimals: int,
) -> PartitionState:
    """Return the partition for a key, creating its engine on first use."""
    state = partitions.get(key)
    if state is None:
        state = PartitionState(
            engine=FIFOEngine(wallet=wallet, token_address=key[2], token_decimals=token_decimals)
        )
        partitions[key] = state
    return state


def _consume_or_flag(
    state: PartitionState,
    swap: UniswapV3Swap,
    wallet: str,
    token_address: str,
    qty: Decimal,
    unit_sale_usd: float,
    *,
    keep: bool,
) -> None:
    """Consume from a partition, keeping or discarding the Realizations.

    Shares the insufficient-balance handling with the transfer path rather than
    repeating it: a wallet that acquired its position before the indexed window
    will sell what the stack never received, which is ADR 0012 decision 5's
    has_pre_window_activity signal arriving by a third route.
    """
    try:
        realized = state.engine.consume(
            realized_at=swap.block_timestamp, qty_token=qty, unit_sale_usd=unit_sale_usd
        )
    except InsufficientBalanceError as e:
        state.has_insufficient_balance = True
        state.insufficient_balance_count += 1
        logger.warning(
            "Insufficient balance on swap %s:%d: chain=%s wallet=%s token=%s — %s",
            swap.tx_hash,
            swap.log_index,
            swap.chain,
            wallet,
            token_address,
            e,
        )
        return

    if keep:
        state.realizations.extend(realized)
