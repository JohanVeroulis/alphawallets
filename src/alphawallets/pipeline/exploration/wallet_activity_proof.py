"""Three-way integration proof: swaps x transfers x prices for one wallet.

The first pipeline-stage module in the repo, and the first place the three Week 1
schemas are joined rather than written in isolation. It answers one question —
"what did this wallet do with this token, and what was it worth?" — by reading
uniswap_v3_swap, erc20_transfer and token_price out of the local cache.

Reads DuckDB only. No Alchemy, no HTTP (CLAUDE.md Section 6): pipeline stages
work from the cache the fetchers fill, so a stage can be re-run freely and its
cost is never a provider's rate limit.

Layering, mirroring the fetchers: models.py holds Event and the price
classification, queries.py holds the SQL, and this module composes them and
renders the result.
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from duckdb import DuckDBPyConnection
from pydantic import BaseModel, ConfigDict, Field

from alphawallets.db import connect
from alphawallets.pipeline.exploration.models import UNI_ETHEREUM, Event
from alphawallets.pipeline.exploration.queries import (
    POOL_TOKEN_LAYOUT,
    assemble_timeline,
    hour_of,
    query_most_active_wallet,
    query_price_grid_head,
    query_prices_for_hours,
    query_wallet_swaps,
    query_wallet_transfers,
)
from alphawallets.tokens import V1_TOKENS
from alphawallets.unpriceable import is_unpriceable

logger = logging.getLogger(__name__)


class TimelineSummary(BaseModel):
    """Counters for one timeline, consumed identically by the CLI and the tests.

    Its own model so the coverage arithmetic lives in exactly one place. The
    denominator for coverage is covered-range events only — priced plus
    unavailable. Both of the other states are legitimate skips rather than
    failures: an event above the price grid's head has no price to be missing
    yet, and an event on a route-less token never will have one. Counting either
    would report a provider characteristic as falling coverage, every run,
    forever. Only unavailable is a real gap.
    """

    model_config = ConfigDict(frozen=True)

    total: int = Field(ge=0)
    swaps: int = Field(ge=0)
    transfers: int = Field(ge=0)
    priced: int = Field(ge=0)
    pending: int = Field(ge=0, description="Events in the current incomplete hour")
    unavailable: int = Field(
        ge=0, description="Events at or below the grid head with no price row — a real hole"
    )
    unpriceable: int = Field(
        ge=0, description="Events on a token no configured price route can serve"
    )
    price_grid_head: datetime | None = Field(
        default=None,
        description="Newest priced hour for this (chain, token). None when unpriced",
    )
    pending_hours: list[datetime] = Field(
        default_factory=list,
        description="Hours above the grid head — the backfill has not reached them",
    )
    unavailable_hours: list[datetime] = Field(
        default_factory=list,
        description="Past hours with no price row — a real gap in the backfill",
    )

    @property
    def covered_range_events(self) -> int:
        """Events where a price could legitimately exist.

        Excludes pending (the backfill has not reached their hour) and
        unpriceable (no route can serve their token) — neither is a miss.
        """
        return self.priced + self.unavailable

    @property
    def coverage_pct(self) -> float:
        """Priced share of covered-range events. 100.0 when there are none."""
        if self.covered_range_events == 0:
            return 100.0
        return 100.0 * self.priced / self.covered_range_events

    @property
    def is_fully_priced(self) -> bool:
        """True when every event inside the covered range has a price.

        The strict assertion for the live proof: pending is excluded, but a single
        unavailable event makes this False and must fail loudly.
        """
        return self.unavailable == 0


def summarise(
    events: list[Event],
    price_grid_head: datetime | None = None,
) -> TimelineSummary:
    """Count a timeline into a TimelineSummary.

    Args:
        events: The timeline, in any order.
        price_grid_head: Newest priced hour for this (chain, token), carried into
            the summary so the output can explain the pending bucket.

    Returns:
        A frozen summary, with the sorted lists of hours that are pending and of
        hours that are genuinely missing a price.
    """
    by_status: dict[str, list[Event]] = {
        "priced": [],
        "pending": [],
        "unavailable": [],
        "unpriceable": [],
    }
    for event in events:
        by_status[event.price_status].append(event)

    return TimelineSummary(
        total=len(events),
        swaps=sum(1 for e in events if e.event_type == "swap"),
        transfers=sum(1 for e in events if e.event_type == "transfer"),
        priced=len(by_status["priced"]),
        pending=len(by_status["pending"]),
        unavailable=len(by_status["unavailable"]),
        unpriceable=len(by_status["unpriceable"]),
        price_grid_head=price_grid_head,
        pending_hours=sorted({_hour(e.ts) for e in by_status["pending"]}),
        unavailable_hours=sorted({_hour(e.ts) for e in by_status["unavailable"]}),
    )


def _hour(ts: datetime) -> datetime:
    """Truncate to the UTC hour. Mirrors queries.hour_of, kept local to avoid a cycle."""
    return ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def build_wallet_timeline(
    conn: DuckDBPyConnection,
    wallet_address: str,
    chain: str = "ethereum",
    token_address: str = UNI_ETHEREUM,
) -> tuple[list[Event], TimelineSummary]:
    """Build one wallet's priced timeline for one token from the cache.

    Three queries — swaps, transfers, then prices in bulk for exactly the hours
    those events occupy — composed into a chronological Event list.

    Args:
        conn: Open DuckDB connection to the cache.
        wallet_address: Wallet to inspect, any case.
        chain: Chain name. Only rows on this chain are considered.
        token_address: Token contract, any case.

    Returns:
        (events, summary) — the full timeline including unpriced events, and its
        counters.
    """
    swaps = query_wallet_swaps(conn, wallet_address, chain, token_address)
    transfers = query_wallet_transfers(conn, wallet_address, chain, token_address)

    # Only the hours the events actually occupy, so the price query scales with
    # activity rather than with the span of the cache.
    hours = {hour_of(row["ts"]) for row in [*swaps, *transfers]}
    prices_by_hour = query_prices_for_hours(conn, chain, token_address, hours)

    # The grid's own head is the reference for pending vs unavailable, so the
    # classification depends on cached data rather than on the wall clock.
    grid_head = query_price_grid_head(conn, chain, token_address)

    # A property of the token, not of any one event, so resolved once. Read from
    # the route cache AW_03's discovery probe writes (ADR 0010), so a token
    # becomes priceable here the moment a route is found for it — no second list
    # to keep in sync.
    unpriceable = is_unpriceable(conn, chain, token_address)
    if unpriceable:
        logger.info(
            "No configured price route serves chain=%s token=%s; "
            "classifying its events as unpriceable (see ADR 0010)",
            chain,
            token_address,
        )

    events = assemble_timeline(
        swaps,
        transfers,
        prices_by_hour,
        price_grid_head=grid_head,
        is_unpriceable=unpriceable,
    )
    return events, summarise(events, price_grid_head=grid_head)


# ---------- Output ----------


def _short(address: str) -> str:
    """Abbreviate an address for a header line: 0x1234...abcd."""
    return f"{address[:6]}...{address[-4:]}" if len(address) > 12 else address


def _token_symbol(token_address: str) -> str:
    """Look up a token's symbol for display, or fall back to a neutral label.

    Checks the V1 registry first, then the verified pool layout — the registry
    covers every tracked token while the layout only knows the ones sitting in a
    tracked pool. Without the registry a timeline for MKR rendered its amounts as
    "0.81 token", which reads as a defect to anyone opening the output.

    Display only — nothing joins on the symbol.
    """
    token = token_address.lower()
    for symbol, addresses in V1_TOKENS.items():
        if token in addresses.values():
            return symbol
    for layout in POOL_TOKEN_LAYOUT.values():
        for slot in ("token0", "token1"):
            if layout[slot]["address"] == token:
                return str(layout[slot]["symbol"])
    return "token"


def _hour_label(ts: datetime) -> str:
    """Render an hour for the summary lines, always in UTC."""
    return ts.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")


def _format_amount(amount: Decimal) -> str:
    """Thousands-separated, two decimals — readable in a column."""
    return f"{amount:,.2f}"


def format_event_line(event: Event, symbol: str) -> str:
    """Render one event as a timeline row.

    Unpriced events show why they are unpriced in the price column rather than a
    blank, so a reader can tell a structural lag from a missing backfill without
    consulting the summary.
    """
    hour_label = event.ts.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")
    amount = _format_amount(event.amount_token)
    approx = " (~)" if event.amount_approximate else ""

    if event.price_status == "priced":
        assert event.price_usd is not None and event.value_usd is not None
        price_cell = f"@ ${event.price_usd:,.2f}"
        value_cell = f"= ${event.value_usd:>12,.2f}"
    else:
        price_cell = f"@ {event.price_status}"
        value_cell = f"= {'--':>13}"

    paired = ""
    if event.other_amount is not None and event.other_token is not None:
        paired = f"  (-> {_format_amount(event.other_amount)} {event.other_token})"

    return (
        f"[{hour_label}]  {event.event_type.upper():<8}  {event.direction.upper():<4}  "
        f"{amount:>14} {symbol}{approx}  {price_cell:<16} {value_cell}{paired}"
    )


def format_timeline(
    events: list[Event],
    summary: TimelineSummary,
    wallet_address: str,
    chain: str,
    token_address: str,
) -> str:
    """Render the whole proof: header, counters, then one line per event.

    Written to read honestly to someone who opens the terminal without having
    read the design discussion — every unpriced event says why, and the summary
    names the pending hour and any genuinely missing hours.
    """
    symbol = _token_symbol(token_address)
    lines = [
        "AlphaWallets — Wallet Activity Proof",
        f"  Wallet: {_short(wallet_address)}",
        f"  Chain:  {chain}   Token: {symbol} ({_short(token_address)})",
        "",
        f"Events: {summary.total} ({summary.swaps} swaps, {summary.transfers} transfers)",
    ]

    coverage = (
        f"Priced: {summary.priced}/{summary.covered_range_events} covered-range events "
        f"({summary.coverage_pct:.0f}%)"
    )
    coverage += f"   Pending: {summary.pending}"
    if summary.pending_hours:
        hours = ", ".join(_hour_label(h) for h in summary.pending_hours)
        coverage += f" [{hours}]"
    coverage += f"   Unavailable: {summary.unavailable}"
    if summary.unavailable_hours:
        hours = ", ".join(_hour_label(h) for h in summary.unavailable_hours)
        coverage += f" [{hours}]"
    coverage += f"   Unpriceable: {summary.unpriceable}"
    lines.append(coverage)

    head_label = (
        _hour_label(summary.price_grid_head)
        if summary.price_grid_head is not None
        else "none — this token has no prices in the cache"
    )
    lines.append(f"Price grid head: {head_label}")

    if summary.unpriceable:
        lines += [
            "",
            f"Note: all {summary.unpriceable} event(s) are on a token no configured price "
            "route can serve, so no",
            "      price exists for them on any hour. This is not a backfill gap and "
            "re-running AW_03 will not",
            "      help — the fallback route is designed in ADR 0010 and not yet "
            "implemented. Coverage above",
            "      counts only events a price could exist for.",
        ]
    elif summary.pending:
        if summary.price_grid_head is None:
            lines += [
                "",
                f"Note: all {summary.pending} event(s) are unpriced because no prices exist "
                "for this chain and token.",
                "      Run AW_03 for this token, then re-run this proof.",
            ]
        else:
            lines += [
                "",
                f"Note: {summary.pending} event(s) fall in hours above the price grid head "
                f"({head_label}), so no price",
                "      exists for them yet. That is the provider's publishing lag, not a "
                "missing backfill — DefiLlama",
                "      runs roughly two hours behind the chain head. Coverage above counts "
                "the covered range only.",
            ]
    if summary.unavailable_hours:
        lines += [
            "",
            f"Warning: {summary.unavailable} event(s) fall in hours at or below the grid "
            "head with no price row.",
            "         That is a real hole inside the range we cover, not lag. Re-run AW_03 "
            "for the hours listed above.",
        ]

    lines.append("")
    if not events:
        lines.append("(no events)")
    for event in events:
        lines.append(format_event_line(event, symbol))

    return "\n".join(lines)


# ---------- CLI ----------


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI arg parser. Extracted so tests can call it in isolation."""
    parser = argparse.ArgumentParser(
        description=(
            "Prove the three Week 1 schemas join: show one wallet's swaps and "
            "transfers for one token, priced from the hourly grid. Reads the "
            "local DuckDB cache only."
        ),
    )
    parser.add_argument(
        "--wallet",
        default=None,
        help="Wallet address. Omit to auto-pick the most active wallet in the cache.",
    )
    parser.add_argument(
        "--chain",
        default="ethereum",
        choices=["ethereum", "base"],
        help="Target chain (V1 scope per CLAUDE.md Section 2). Default: ethereum.",
    )
    parser.add_argument(
        "--token",
        default=UNI_ETHEREUM,
        help=f"Token contract address. Default: UNI ({UNI_ETHEREUM}).",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Optional DuckDB path override.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Log query progress at INFO level.",
    )
    return parser


def main() -> int:
    """CLI entry point for the wallet activity proof.

    Returns:
        0 on success, 1 when the cache holds no activity to prove anything with.
        A non-zero exit is reserved for "nothing to show" rather than used for an
        unpriced event: an unavailable price is reported loudly in the output but
        is a data finding, not a failure of this stage.
    """
    parser = _build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    with connect(args.db_path) as conn:
        wallet = args.wallet
        if wallet is None:
            wallet = query_most_active_wallet(conn, args.chain, args.token)
            if wallet is None:
                print(
                    f"No activity in the cache for chain={args.chain} "
                    f"token={args.token}.\n"
                    "Backfill first: AW_01 for swaps, AW_02 for transfers, AW_03 for prices.",
                )
                return 1
            logger.info("Auto-picked most active wallet: %s", wallet)

        events, summary = build_wallet_timeline(
            conn,
            wallet_address=wallet,
            chain=args.chain,
            token_address=args.token,
        )

    if not events:
        print(
            f"No events for wallet={wallet} chain={args.chain} token={args.token}.\n"
            "The wallet has no swaps or transfers of this token in the cache.",
        )
        return 1

    print(format_timeline(events, summary, wallet, args.chain, args.token))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
