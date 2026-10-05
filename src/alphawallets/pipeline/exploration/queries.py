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

from alphawallets.pipeline.exploration.models import Event, classify_price_status

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
# Every entry below was verified live against the pool contracts — token0(),
# token1(), fee(), and each token's decimals() — and re-verified slot by slot
# after transcription into this dict.
#
# Methodology caveat for anyone adding future pools. Fee-tier selection used
# the pool's liquidity() value, which is a uint128 in the pool's own internal
# units — roughly sqrt(token0 * token1) scaled by decimals. It is validly
# comparable ACROSS FEE TIERS OF THE SAME PAIR, because those pools hold
# identical tokens with identical decimals, and that is the only way it is used
# here. It is NOT comparable across different pairs or different quote assets:
# claiming one token's pool is 'deeper' than another's from these numbers is
# meaningless, and comparing a TOKEN/WETH pool against a TOKEN/USDC pool
# crosses both the pair and the decimals boundary. When liquidity cannot settle
# a choice, a short smoke-test backfill is the reliable signal: whether swaps
# actually emit is a fact, where a liquidity comparison would be an inference.
POOL_TOKEN_LAYOUT: dict[str, dict[str, Any]] = {
    "0x1d42064fc4beb5f8aaf85f4617ae8b3b5b8bd801": {  # UNI/WETH 0.3% on ethereum
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
    "0x5ab53ee1d50eef2c1dd3d5402789cd27bb52c1bb": {  # AAVE/WETH 0.3% on ethereum
        "token0": {
            "address": "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9",
            "symbol": "AAVE",
            "decimals": 18,
        },
        "token1": {
            "address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "symbol": "WETH",
            "decimals": 18,
        },
    },
    "0xa3f558aebaecaf0e11ca4b2199cc5ed341edfd74": {  # LDO/WETH 0.3% on ethereum
        "token0": {
            "address": "0x5a98fcbea516cf06857215779fd812ca3bef1b32",
            "symbol": "LDO",
            "decimals": 18,
        },
        "token1": {
            "address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "symbol": "WETH",
            "decimals": 18,
        },
    },
    "0x57af956d3e2cca3b86f3d8c6772c03ddca3eaacb": {  # PENDLE/WETH 0.3% on ethereum
        "token0": {
            "address": "0x808507121b80c02388fad14726482e061b8da827",
            "symbol": "PENDLE",
            "decimals": 18,
        },
        "token1": {
            "address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "symbol": "WETH",
            "decimals": 18,
        },
    },
    "0x919fa96e88d67499339577fa202345436bcdaf79": {  # CRV/WETH 0.3% on ethereum
        "token0": {
            "address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "symbol": "WETH",
            "decimals": 18,
        },
        "token1": {
            "address": "0xd533a949740bb3306d119cc777fa900ba034cd52",
            "symbol": "CRV",
            "decimals": 18,
        },
    },
    "0xc3db44adc1fcdfd5671f555236eae49f4a8eea18": {  # ENA/WETH 0.3% on ethereum
        "token0": {
            "address": "0x57e114b691db790c35207b2e685d4a43181e6061",
            "symbol": "ENA",
            "decimals": 18,
        },
        "token1": {
            "address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "symbol": "WETH",
            "decimals": 18,
        },
    },
    # MKR trades here, but DefiLlama's /chart does not carry MKR, so its
    # swaps classify as 'unpriceable' until ADR 0010 ships. MKR's symbol()
    # also returns bytes32 rather than string; that affects token metadata
    # calls, not pool metadata, so nothing here depends on it.
    "0xe8c6c9227491c0a8156a0106a0204d881bb7e531": {  # MKR/WETH 0.3% on ethereum
        "token0": {
            "address": "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2",
            "symbol": "MKR",
            "decimals": 18,
        },
        "token1": {
            "address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "symbol": "WETH",
            "decimals": 18,
        },
    },
    # 1% rather than the 0.3% default: measured ~2x the liquidity of the
    # 0.3% pool. MORPHO here is the TRANSFERABLE deployment (ADR 0008
    # amendment) — the legacy address has no pool and no price.
    "0x25b96761e765b9ac20db18fa57fa91e3b617ec6f": {  # MORPHO/WETH 1% on ethereum
        "token0": {
            "address": "0x58d97b57bb95320f9a05dc918aef65434969c2b2",
            "symbol": "MORPHO",
            "decimals": 18,
        },
        "token1": {
            "address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "symbol": "WETH",
            "decimals": 18,
        },
    },
    "0xa6cc3c2531fdaa6ae1a3ca84c2855806728693e8": {  # LINK/WETH 0.3% on ethereum
        "token0": {
            "address": "0x514910771af9ca656af840dff83e8264ecf986ca",
            "symbol": "LINK",
            "decimals": 18,
        },
        "token1": {
            "address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "symbol": "WETH",
            "decimals": 18,
        },
    },
    "0x59354356ec5d56306791873f567d61ebf11dfbd5": {  # ARB/WETH 0.3% on ethereum
        "token0": {
            "address": "0xb50721bcf8d664c30412cfbc6cf7a15145234ad1",
            "symbol": "ARB",
            "decimals": 18,
        },
        "token1": {
            "address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "symbol": "WETH",
            "decimals": 18,
        },
    },
    "0xc2c390c6cd3c4e6c2b70727d35a45e8a072f18ca": {  # EIGEN/WETH 0.3% on ethereum
        "token0": {
            "address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "symbol": "WETH",
            "decimals": 18,
        },
        "token1": {
            "address": "0xec53bf9167f50cdeb3ae105f56099aaab9061f83",
            "symbol": "EIGEN",
            "decimals": 18,
        },
    },
    "0x06f00544c0bc62e6db10f46d370dfccdc23d8189": {  # ETHFI/WETH 0.3% on ethereum
        "token0": {
            "address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "symbol": "WETH",
            "decimals": 18,
        },
        "token1": {
            "address": "0xfe0c30065b384f05761f15d0cc899d4f9f9cc0eb",
            "symbol": "ETHFI",
            "decimals": 18,
        },
    },
    "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640": {  # USDC/WETH 0.05% on ethereum
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
    # 1% rather than 0.3%: the 0.3% pool exists but is ~25,000x shallower.
    "0xab365f161dd501473a1ff0d2ef0dce94e7398839": {  # UNI/WETH 1% on base
        "token0": {
            "address": "0x4200000000000000000000000000000000000006",
            "symbol": "WETH",
            "decimals": 18,
        },
        "token1": {
            "address": "0xc3de830ea07524a0761646a6a4e4be0e114a3c83",
            "symbol": "UNI",
            "decimals": 18,
        },
    },
    "0x2e86514cfd61fb19c5cf2b879d536d273d6e693d": {  # AAVE/WETH 0.3% on base
        "token0": {
            "address": "0x4200000000000000000000000000000000000006",
            "symbol": "WETH",
            "decimals": 18,
        },
        "token1": {
            "address": "0x63706e401c06ac8513145b7687a14804d17f814b",
            "symbol": "AAVE",
            "decimals": 18,
        },
    },
    # 1% rather than 0.3%: measured ~60x the liquidity of the 0.3% pool.
    "0x330e535c40eb49cc186496f061052fcf814d68cb": {  # CRV/WETH 1% on base
        "token0": {
            "address": "0x4200000000000000000000000000000000000006",
            "symbol": "WETH",
            "decimals": 18,
        },
        "token1": {
            "address": "0x8ee73c484a26e0a5df2ee2a4960b789967dd0415",
            "symbol": "CRV",
            "decimals": 18,
        },
    },
    # Shallowest pool in the set (~5 orders of magnitude below its peers)
    # and the only V3 option for PENDLE on Base; the USDC pair is thinner
    # still. Low confidence — expect few or no swaps in a short window.
    "0xd7042869277c75ca56f1f6cc7e18ff0d83410dee": {  # PENDLE/WETH 0.3% on base
        "token0": {
            "address": "0x4200000000000000000000000000000000000006",
            "symbol": "WETH",
            "decimals": 18,
        },
        "token1": {
            "address": "0xa99f6e6785da0f5d6fb42495fe424bce029eeb3e",
            "symbol": "PENDLE",
            "decimals": 18,
        },
    },
    "0x2f42df4af5312b492e9d7f7b2110d9c7bf2d9e4f": {  # MORPHO/WETH 0.3% on base
        "token0": {
            "address": "0x4200000000000000000000000000000000000006",
            "symbol": "WETH",
            "decimals": 18,
        },
        "token1": {
            "address": "0xbaa5cc21fd487b8fcc2f632f3f4e8d37262a0842",
            "symbol": "MORPHO",
            "decimals": 18,
        },
    },
    "0x224a5d3f2155f2f85af70b6d72aea61a15273ff4": {  # LINK/WETH 0.3% on base
        "token0": {
            "address": "0x4200000000000000000000000000000000000006",
            "symbol": "WETH",
            "decimals": 18,
        },
        "token1": {
            "address": "0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196",
            "symbol": "LINK",
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
# Which wallet to show when no --wallet is given.
#
# Not ORDER BY COUNT(*). Swaps and transfers live in different address spaces:
# a swap is keyed on tx_from, the EOA that submitted the transaction, while a
# transfer is keyed on from_addr/to_addr, the token-level participants — which for
# a router-mediated swap are the pool and the router, not the EOA. Measured on the
# live cache on 2026-10-01: 33 distinct UNI/WETH swappers, 469 distinct transfer
# participants, only 8 addresses in both. Ranking by raw row count therefore picks
# a high-traffic contract (the first live run chose one with 238 transfers and zero
# swaps), and such an address almost never has swaps — so the three-way join the
# module exists to demonstrate never appeared in the output.
#
# Wallets present in both tables are ranked first, even with far less activity.
TOP_WALLET_SQL = """
WITH swaps AS (
    SELECT lower(tx_from) AS wallet, COUNT(*) AS n
    FROM uniswap_v3_swap
    WHERE chain = ? AND lower(pool_address) IN ({pool_placeholders})
    GROUP BY 1
),
transfers AS (
    SELECT wallet, COUNT(*) AS n FROM (
        SELECT lower(from_addr) AS wallet
        FROM erc20_transfer
        WHERE chain = ? AND lower(token_address) = ?
        UNION ALL
        SELECT lower(to_addr) AS wallet
        FROM erc20_transfer
        WHERE chain = ? AND lower(token_address) = ?
    )
    GROUP BY 1
),
combined AS (
    SELECT
        COALESCE(s.wallet, t.wallet) AS wallet,
        COALESCE(s.n, 0) AS swap_count,
        COALESCE(t.n, 0) AS transfer_count
    FROM swaps s
    FULL OUTER JOIN transfers t ON s.wallet = t.wallet
)
SELECT wallet, swap_count, transfer_count
FROM combined
ORDER BY
    (swap_count > 0 AND transfer_count > 0) DESC,
    swap_count + transfer_count DESC,
    wallet
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


GRID_HEAD_SQL = """
SELECT MAX(ts)
FROM token_price
WHERE chain = ? AND lower(token_address) = ?
"""


def query_price_grid_head(
    conn: DuckDBPyConnection,
    chain: str,
    token_address: str,
) -> datetime | None:
    """Return the newest priced hour for a (chain, token), or None if unpriced.

    The reference point for classify_price_status: above it the backfill simply
    has not arrived, at or below it a missing hour is a real hole.
    """
    row = conn.execute(GRID_HEAD_SQL, [chain, token_address.lower()]).fetchone()
    if row is None or row[0] is None:
        return None
    return row[0].astimezone(UTC)


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
    price_grid_head: datetime | None = None,
    is_unpriceable: bool = False,
) -> list[Event]:
    """Turn raw query results into one chronological, priced Event list.

    Args:
        swaps: Rows from query_wallet_swaps.
        transfers: Rows from query_wallet_transfers.
        prices_by_hour: Mapping from query_prices_for_hours.
        price_grid_head: Newest priced hour for this (chain, token), from
            query_price_grid_head. None means the token has no prices at all.
        is_unpriceable: Whether no configured price route can serve this token.
            Applies to the whole timeline, since it is a property of the token.

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
                price_grid_head=price_grid_head,
                is_unpriceable=is_unpriceable,
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
                price_grid_head=price_grid_head,
                is_unpriceable=is_unpriceable,
                counterparty=transfer["counterparty"],
                amount_approximate=transfer["decimals_assumed"],
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
    price_grid_head: datetime | None,
    is_unpriceable: bool,
    **extra: Any,
) -> Event:
    """Build one Event, resolving its price and status from the hour grid."""
    hour = hour_of(ts)
    price = None if is_unpriceable else prices_by_hour.get(hour)
    status = classify_price_status(
        hour,
        has_price_row=price is not None,
        price_grid_head=price_grid_head,
        is_unpriceable=is_unpriceable,
    )
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
