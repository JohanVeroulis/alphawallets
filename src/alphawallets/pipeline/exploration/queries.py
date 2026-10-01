"""SQL for the wallet activity proof: swaps, transfers, and the price grid.

Three small queries plus a composition step. SQL is written literally — no ORM,
no builder — so each statement can be read in one glance and pasted into a DuckDB
shell when a result looks wrong.

Every query filters on chain and on token explicitly. That is not defensive
boilerplate: Stage A found that the chain column and the address casing are
exactly where a three-schema join goes silently wrong, so the invariants live in
the WHERE clause rather than in a caller's discipline.

Prices are fetched in bulk for the set of hours the events occupy, not joined per
event. One query instead of N, and it makes "no price row for this hour" an
explicit absent key rather than a NULL that a LEFT JOIN quietly produces.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from duckdb import DuckDBPyConnection

from alphawallets.pipeline.exploration.wallet_activity_proof import (
    Event,
    classify_price_status,
)

# Which token sits in which slot of each pool we fetch swaps for.
#
# The swap table stores amount0/amount1 but not token0/token1, so the sign of the
# right amount cannot be interpreted without knowing the layout. Three ways to
# learn it: call the pool contract (an Alchemy call from a pipeline stage, which
# CLAUDE.md Section 6 rules out), store the columns on the swap row (a schema
# change and another backfill), or declare the layout for the pools we track.
# The third is chosen: V1 tracks a handful of pools, the layout is immutable once
# a pool is deployed, and extending the dict is a one-line change.
#
# Every entry below was verified live against the pool contracts on 2026-10-01 —
# token0(), token1(), and each token's symbol() and decimals().
POOL_TOKEN_LAYOUT: dict[str, dict[str, Any]] = {
    "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640": {  # USDC/WETH 0.05%
        "token0": {
            "address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "symbol": "USDC",
            "decimals": 6,
        },
        "token1": {
            "address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "symbol": "WETH",
            "decimals": 18,
        },
    },
    "0x1d42064fc4beb5f8aaf85f4617ae8b3b5b8bd801": {  # UNI/WETH 0.3%
        "token0": {
            "address": "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984",
            "symbol": "UNI",
            "decimals": 18,
        },
        "token1": {
            "address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "symbol": "WETH",
            "decimals": 18,
        },
    },
}


# ---------- Layout helpers ----------


def pools_holding_token(token_address: str) -> list[str]:
    """Return the tracked pools that hold a token, lowercase.

    Used to scope the swap query: a wallet's UNI swaps are the swaps it made in
    pools that contain UNI, and the layout dict is what knows which those are.
    """
    token = token_address.lower()
    return [
        pool
        for pool, layout in POOL_TOKEN_LAYOUT.items()
        if token in {layout["token0"]["address"], layout["token1"]["address"]}
    ]


def token_slot(pool_address: str, token_address: str) -> str:
    """Return 'token0' or 'token1' — which side of the pool a token sits on.

    Raises:
        KeyError: If the pool is not in POOL_TOKEN_LAYOUT, or the token is not in
            that pool. Both are programmer errors, and guessing a slot would
            invert every direction for that pool.
    """
    pool = pool_address.lower()
    token = token_address.lower()
    if pool not in POOL_TOKEN_LAYOUT:
        raise KeyError(
            f"Pool {pool} is not in POOL_TOKEN_LAYOUT. Add its verified token0/token1 "
            "before reading swaps from it — the amount signs cannot be interpreted without it."
        )
    layout = POOL_TOKEN_LAYOUT[pool]
    for slot in ("token0", "token1"):
        if layout[slot]["address"] == token:
            return slot
    raise KeyError(f"Token {token} is not in pool {pool}")


def _swap_direction(amount: int) -> str:
    """Classify a swap by what happened to the wallet's token.

    Uniswap V3 signs amounts from the pool's perspective: positive means the
    token came INTO the pool, negative means it left. So a positive amount for
    our token is the wallet selling it, and a negative amount is the wallet
    buying it. Getting this backwards would flip every trade in the timeline.

    Args:
        amount: The signed pool-side amount of the token being tracked.

    Returns:
        'sell' when the wallet sent the token into the pool, 'buy' otherwise.
    """
    return "sell" if amount > 0 else "buy"


def _to_whole_units(raw: str | int, decimals: int) -> Decimal:
    """Convert a raw integer amount to whole token units, exactly.

    Decimal, not float: raw amounts are uint256-scale and a float would lose the
    low-order digits silently. Stored as VARCHAR for the same reason.
    """
    return Decimal(abs(int(raw))) / (Decimal(10) ** decimals)


# ---------- Queries ----------


SWAPS_SQL = """
SELECT block_timestamp, tx_hash, log_index, pool_address, amount0, amount1
FROM uniswap_v3_swap
WHERE chain = ?
  AND lower(tx_from) = ?
  AND lower(pool_address) IN ({pool_placeholders})
ORDER BY block_timestamp, log_index
"""

TRANSFERS_SQL = """
SELECT block_timestamp, tx_hash, log_index, from_addr, to_addr, value_raw, token_decimals
FROM erc20_transfer
WHERE chain = ?
  AND lower(token_address) = ?
  AND (lower(from_addr) = ? OR lower(to_addr) = ?)
ORDER BY block_timestamp, log_index
"""

PRICES_SQL = """
SELECT ts, price_usd
FROM token_price
WHERE chain = ?
  AND lower(token_address) = ?
  AND ts IN ({hour_placeholders})
"""

# Which wallet had the most rows across both tables, used when no --wallet is
# given. UNION ALL then aggregate, so a wallet that both swapped and transferred
# ranks above one that only did one of them.
TOP_WALLET_SQL = """
WITH activity AS (
    SELECT lower(tx_from) AS wallet
    FROM uniswap_v3_swap
    WHERE chain = ? AND lower(pool_address) IN ({pool_placeholders})
    UNION ALL
    SELECT lower(from_addr) AS wallet
    FROM erc20_transfer
    WHERE chain = ? AND lower(token_address) = ?
    UNION ALL
    SELECT lower(to_addr) AS wallet
    FROM erc20_transfer
    WHERE chain = ? AND lower(token_address) = ?
)
SELECT wallet, COUNT(*) AS n
FROM activity
GROUP BY wallet
ORDER BY n DESC, wallet ASC
LIMIT 1
"""


def query_wallet_swaps(
    conn: DuckDBPyConnection,
    wallet: str,
    chain: str,
    token_address: str,
) -> list[dict[str, Any]]:
    """Fetch the wallet's swaps in pools holding the token.

    Matches on tx_from, the real EOA that submitted the transaction, not on
    sender/recipient — those are usually a router contract (PR #15).

    Args:
        conn: Open DuckDB connection.
        wallet: Wallet address, any case.
        chain: Chain name, e.g. 'ethereum'.
        token_address: The token being tracked, any case.

    Returns:
        One dict per swap, each with the token's signed amount and the paired
        leg already resolved from the pool layout.
    """
    wallet = wallet.lower()
    token_address = token_address.lower()
    pools = pools_holding_token(token_address)
    if not pools:
        return []

    placeholders = ", ".join("?" for _ in pools)
    rows = conn.execute(
        SWAPS_SQL.format(pool_placeholders=placeholders),
        [chain, wallet, *pools],
    ).fetchall()

    swaps: list[dict[str, Any]] = []
    for ts, tx_hash, log_index, pool_address, amount0, amount1 in rows:
        layout = POOL_TOKEN_LAYOUT[pool_address.lower()]
        slot = token_slot(pool_address, token_address)
        other_slot = "token1" if slot == "token0" else "token0"
        amounts = {"token0": int(amount0), "token1": int(amount1)}

        swaps.append(
            {
                "ts": ts,
                "tx_hash": tx_hash,
                "log_index": log_index,
                "pool_address": pool_address.lower(),
                "signed_amount": amounts[slot],
                "amount_token": _to_whole_units(amounts[slot], layout[slot]["decimals"]),
                "other_amount": _to_whole_units(
                    amounts[other_slot], layout[other_slot]["decimals"]
                ),
                "other_token": layout[other_slot]["symbol"],
            }
        )
    return swaps


def query_wallet_transfers(
    conn: DuckDBPyConnection,
    wallet: str,
    chain: str,
    token_address: str,
) -> list[dict[str, Any]]:
    """Fetch the wallet's transfers of the token, in either direction.

    Args:
        conn: Open DuckDB connection.
        wallet: Wallet address, any case.
        chain: Chain name.
        token_address: Token contract, any case.

    Returns:
        One dict per transfer, with direction and counterparty resolved.
    """
    wallet = wallet.lower()
    token_address = token_address.lower()
    rows = conn.execute(TRANSFERS_SQL, [chain, token_address, wallet, wallet]).fetchall()

    transfers: list[dict[str, Any]] = []
    for ts, tx_hash, log_index, from_addr, to_addr, value_raw, token_decimals in rows:
        outgoing = from_addr.lower() == wallet
        # token_decimals is nullable in the schema (the Transfers API does not
        # always report it). 18 is the ERC-20 default and the right fallback for
        # every V1 tracked token; a wrong guess would misscale the amount, so it
        # is recorded in the dict for the caller to see.
        decimals = token_decimals if token_decimals is not None else 18
        transfers.append(
            {
                "ts": ts,
                "tx_hash": tx_hash,
                "log_index": log_index,
                "direction": "out" if outgoing else "in",
                "amount_token": _to_whole_units(value_raw, decimals),
                "counterparty": (to_addr if outgoing else from_addr).lower(),
                "decimals_assumed": token_decimals is None,
            }
        )
    return transfers


def query_prices_for_hours(
    conn: DuckDBPyConnection,
    chain: str,
    token_address: str,
    hours: set[datetime],
) -> dict[datetime, float]:
    """Fetch USD prices for a set of hours in one query.

    Bulk rather than per-event: the price-row-exists check is then a dict lookup,
    and a missing hour is an absent key instead of a NULL from a LEFT JOIN.

    Args:
        conn: Open DuckDB connection.
        chain: Chain name.
        token_address: Token contract, any case.
        hours: Hour-aligned UTC datetimes to look up.

    Returns:
        A mapping of hour to price. Hours without a row are simply absent.
    """
    if not hours:
        return {}

    ordered = sorted(hours)
    placeholders = ", ".join("?" for _ in ordered)
    rows = conn.execute(
        PRICES_SQL.format(hour_placeholders=placeholders),
        [chain, token_address.lower(), *ordered],
    ).fetchall()
    return {ts.astimezone(UTC): float(price) for ts, price in rows}


def query_most_active_wallet(
    conn: DuckDBPyConnection,
    chain: str,
    token_address: str,
) -> str | None:
    """Return the wallet with the most swap+transfer rows for the token.

    Used when the CLI is run without --wallet, so the proof has something to show
    without the operator first hunting for an address.

    Returns:
        The wallet address, or None when the cache holds no activity at all.
    """
    token_address = token_address.lower()
    pools = pools_holding_token(token_address)
    if not pools:
        return None

    placeholders = ", ".join("?" for _ in pools)
    row = conn.execute(
        TOP_WALLET_SQL.format(pool_placeholders=placeholders),
        [chain, *pools, chain, token_address, chain, token_address],
    ).fetchone()
    return row[0] if row else None


# ---------- Composition ----------


def hour_of(ts: datetime) -> datetime:
    """Truncate a timestamp to its UTC hour — the price grid's join key.

    Done in Python rather than SQL so the value is identical to the one the price
    lookup was keyed on, with no dependence on the session timezone. The session
    is pinned to UTC anyway (db.connect), but this stage should not rely on it.
    """
    return ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def assemble_timeline(
    swaps: list[dict[str, Any]],
    transfers: list[dict[str, Any]],
    prices_by_hour: dict[datetime, float],
    now_utc: datetime | None = None,
) -> list[Event]:
    """Turn raw query results into one chronological, priced Event list.

    Args:
        swaps: Rows from query_wallet_swaps.
        transfers: Rows from query_wallet_transfers.
        prices_by_hour: Mapping from query_prices_for_hours.
        now_utc: Current time, injectable for deterministic tests.

    Returns:
        Events sorted by timestamp, each classified priced / pending /
        unavailable.
    """
    events: list[Event] = []

    for swap in swaps:
        events.append(
            _build_event(
                ts=swap["ts"],
                event_type="swap",
                direction=_swap_direction(swap["signed_amount"]),
                amount_token=swap["amount_token"],
                tx_hash=swap["tx_hash"],
                prices_by_hour=prices_by_hour,
                now_utc=now_utc,
                other_amount=swap["other_amount"],
                other_token=swap["other_token"],
            )
        )

    for transfer in transfers:
        events.append(
            _build_event(
                ts=transfer["ts"],
                event_type="transfer",
                direction=transfer["direction"],
                amount_token=transfer["amount_token"],
                tx_hash=transfer["tx_hash"],
                prices_by_hour=prices_by_hour,
                now_utc=now_utc,
                counterparty=transfer["counterparty"],
            )
        )

    # Sort on the timestamp alone. tx_hash breaks ties so the order is stable
    # across runs rather than dependent on which query returned first.
    events.sort(key=lambda e: (e.ts, e.tx_hash))
    return events


def _build_event(
    ts: datetime,
    event_type: str,
    direction: str,
    amount_token: Decimal,
    tx_hash: str,
    prices_by_hour: dict[datetime, float],
    now_utc: datetime | None,
    **extra: Any,
) -> Event:
    """Build one Event, resolving its price and status from the hour grid."""
    hour = hour_of(ts)
    price = prices_by_hour.get(hour)
    status = classify_price_status(hour, has_price_row=price is not None, now_utc=now_utc)
    return Event(
        ts=ts,
        event_type=event_type,
        direction=direction,
        amount_token=amount_token,
        price_usd=price,
        price_status=status,
        tx_hash=tx_hash,
        **extra,
    )
