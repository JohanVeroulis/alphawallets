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
    query_prices_for_hours,
    query_wallet_swaps,
    query_wallet_transfers,
)

logger = logging.getLogger(__name__)


class TimelineSummary(BaseModel):
    """Counters for one timeline, consumed identically by the CLI and the tests.

    Its own model so the coverage arithmetic lives in exactly one place. The
    denominator for coverage is complete-hour events only — priced plus
    unavailable — because an event in the hour currently in progress has no price
    to be missing yet. Including it would report a permanent structural lag as
    falling coverage, every run, forever.
    """

    model_config = ConfigDict(frozen=True)

    total: int = Field(ge=0)
    swaps: int = Field(ge=0)
    transfers: int = Field(ge=0)
    priced: int = Field(ge=0)
    pending: int = Field(ge=0, description="Events in the current incomplete hour")
    unavailable: int = Field(ge=0, description="Events in a past hour with no price row")
    pending_hour: datetime | None = Field(
        default=None, description="The current hour, when any event falls inside it"
    )
    unavailable_hours: list[datetime] = Field(
        default_factory=list,
        description="Past hours with no price row — a real gap in the backfill",
    )

    @property
    def complete_hour_events(self) -> int:
        """Events whose hour has finished, so a price could legitimately exist."""
        return self.priced + self.unavailable

    @property
    def coverage_pct(self) -> float:
        """Priced share of complete-hour events. 100.0 when there are none."""
        if self.complete_hour_events == 0:
            return 100.0
        return 100.0 * self.priced / self.complete_hour_events

    @property
    def is_fully_priced(self) -> bool:
        """True when every complete-hour event has a price.

        The strict assertion for the live proof: pending is excluded, but a single
        unavailable event makes this False and must fail loudly.
        """
        return self.unavailable == 0


def summarise(events: list[Event]) -> TimelineSummary:
    """Count a timeline into a TimelineSummary.

    Args:
        events: The timeline, in any order.

    Returns:
        A frozen summary, with the pending hour and the sorted list of hours that
        are genuinely missing a price.
    """
    by_status: dict[str, list[Event]] = {"priced": [], "pending": [], "unavailable": []}
    for event in events:
        by_status[event.price_status].append(event)

    pending_hours = {_hour(e.ts) for e in by_status["pending"]}

    return TimelineSummary(
        total=len(events),
        swaps=sum(1 for e in events if e.event_type == "swap"),
        transfers=sum(1 for e in events if e.event_type == "transfer"),
        priced=len(by_status["priced"]),
        pending=len(by_status["pending"]),
        unavailable=len(by_status["unavailable"]),
        # Normally one hour. Taking the max keeps the label honest if a clock skew
        # ever put two hours in the pending bucket.
        pending_hour=max(pending_hours) if pending_hours else None,
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
    now_utc: datetime | None = None,
) -> tuple[list[Event], TimelineSummary]:
    """Build one wallet's priced timeline for one token from the cache.

    Three queries — swaps, transfers, then prices in bulk for exactly the hours
    those events occupy — composed into a chronological Event list.

    Args:
        conn: Open DuckDB connection to the cache.
        wallet_address: Wallet to inspect, any case.
        chain: Chain name. Only rows on this chain are considered.
        token_address: Token contract, any case.
        now_utc: Current time, injectable so tests and the pending/unavailable
            boundary are deterministic.

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

    events = assemble_timeline(swaps, transfers, prices_by_hour, now_utc=now_utc)
    return events, summarise(events)


# ---------- Output ----------


def _short(address: str) -> str:
    """Abbreviate an address for a header line: 0x1234...abcd."""
    return f"{address[:6]}...{address[-4:]}" if len(address) > 12 else address


def _token_symbol(token_address: str) -> str:
    """Look up a token's symbol from the verified pool layout, or fall back.

    Display only — nothing joins on the symbol.
    """
    token = token_address.lower()
    for layout in POOL_TOKEN_LAYOUT.values():
        for slot in ("token0", "token1"):
            if layout[slot]["address"] == token:
                return str(layout[slot]["symbol"])
    return "token"


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
        f"Priced: {summary.priced}/{summary.complete_hour_events} complete-hour events "
        f"({summary.coverage_pct:.0f}%)"
    )
    pending_label = (
        f"current hour {summary.pending_hour.strftime('%Y-%m-%d %H:%MZ')}"
        if summary.pending_hour is not None
        else "none"
    )
    coverage += f"   Pending: {summary.pending} ({pending_label})"
    coverage += f"   Unavailable: {summary.unavailable}"
    if summary.unavailable_hours:
        hours = ", ".join(h.strftime("%Y-%m-%d %H:%MZ") for h in summary.unavailable_hours)
        coverage += f" [{hours}]"
    lines.append(coverage)

    if summary.pending:
        lines += [
            "",
            f"Note: {summary.pending} event(s) fall in {pending_label}, which DefiLlama has "
            "not published a price for yet.",
            "      They are pending, not missing — the next prices backfill will price them. "
            "Coverage above counts complete hours only.",
        ]
    if summary.unavailable_hours:
        lines += [
            "",
            f"Warning: {summary.unavailable} event(s) fall in past hours with no price row. "
            "That is a real gap in the",
            "         prices backfill, not a lag. Re-run AW_03 to cover the hours listed above.",
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
