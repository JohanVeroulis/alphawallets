"""AW_03 — DefiLlama historical prices fetcher.

Fetches an hourly USD price timeseries for one token from DefiLlama's /chart
endpoint, hour-aligns each point, and writes to the token_price table.

Source: DefiLlama, free and keyless. Coverage for every V1 tracked token was
    verified in ADR 0008 (52/52 probes across a full year).
Route: /chart for bulk backfill; /prices/historical/{timestamp} is reserved
    for fill-gap cases and is not used here.
Granularity: hourly. The PnL pipeline joins on
    date_trunc('hour', block_timestamp), so prices land on hour boundaries.
Chunking: /chart caps a request at 500 points, so 30 days hourly (720) is
    split into balanced chunks by the client. See defillama_client.
Strategy: pre-warm backfill (this fetcher) plus a daily top-up (later), so
    pipeline queries can assume cache hits rather than fetching inline.
Output table: token_price, primary key (chain, token_address, ts, source).
    Idempotent via INSERT OR IGNORE — re-running a span writes nothing new.

Not RPC: this is a plain REST API, so it uses httpx directly rather than the
Web3 provider the on-chain fetchers share.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from alphawallets.config import Chain
from alphawallets.db import connect
from alphawallets.fetchers.prices.defillama_client import chunk_plan, fetch_chart
from alphawallets.fetchers.prices.mapper import to_raw_price_point, to_token_price
from alphawallets.fetchers.prices.models import TokenPrice
from alphawallets.fetchers.prices.writer import create_tables, write_token_prices

logger = logging.getLogger(__name__)

# ---------- Constants ----------

# The V1 default target: UNI on Ethereum, both a tracked DeFi token and a
# tracked airdrop (CLAUDE.md Section 2).
DEFAULT_TOKENS: dict[str, str] = {
    "UNI": "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984",
}

DEFAULT_SPAN_DAYS = 30
DEFAULT_PERIOD = "1h"
HTTP_TIMEOUT_SECONDS = 60.0


class FetchResult(BaseModel):
    """Summary of a fetch_and_persist_prices run.

    Same shape family as AW_01's and AW_02's FetchResult so callers (CLI,
    scheduled jobs, tests) can treat every fetcher alike.
    """

    model_config = ConfigDict(frozen=True)

    chain: str
    token_address: str
    span_days: int = Field(gt=0)
    period: str
    raw_points_seen: int = Field(ge=0, description="Points returned by the API across all chunks")
    chunks_fetched: int = Field(ge=0)
    mapped: int = Field(ge=0, description="Points that survived mapping into TokenPrice rows")
    duplicate_after_alignment: int = Field(
        ge=0,
        description=(
            "Mapped rows the writer skipped because their (chain, token, ts, source) "
            "already existed — either two observations inside one hour, or a re-run "
            "over an already-cached span."
        ),
    )
    written: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)


def fetch_and_persist_prices(
    chain: Chain,
    token_address: str,
    span_days: int = DEFAULT_SPAN_DAYS,
    period: str = DEFAULT_PERIOD,
    db_path: Path | str | None = None,
    client: httpx.Client | None = None,
) -> FetchResult:
    """Fetch, align, and persist a token's hourly price history.

    Steps:
    1. Ask the client for the full span — it handles chunking internally.
    2. Map every entry to a RawPricePoint, then to an hour-aligned TokenPrice.
    3. Write once, in a single batch. Unlike the on-chain fetchers there's no
       per-page write: the whole span is at most a few thousand rows and the
       fetch is seconds, so partial-progress protection buys nothing.
    4. Return a FetchResult with counts.

    Args:
        chain: ethereum or base.
        token_address: Token contract address.
        span_days: Days of history to cover.
        period: Sampling interval, e.g. '1h'.
        db_path: Optional DuckDB path override. Defaults to config.
        client: Optional httpx.Client, mainly for tests. When omitted, one is
            created and closed inside this call.

    Returns:
        FetchResult with per-run counts and wall-clock time.

    Raises:
        ValueError: On a non-positive span_days, unsupported period, or an API
            rejection.
        httpx.HTTPError: On a transport failure that outlived the client's
            retries.
    """
    start_time = time.time()
    _total_points, n_chunks, _chunk_span = chunk_plan(span_days, period)

    owns_client = client is None
    http_client = client if client is not None else httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)

    try:
        entries = fetch_chart(http_client, chain, token_address, span_days, period)
    finally:
        if owns_client:
            http_client.close()

    fetched_at = datetime.now(tz=UTC)
    prices: list[TokenPrice] = []
    for entry in entries:
        raw = to_raw_price_point(entry, chain=chain, token_address=token_address)
        if raw is None:
            continue
        prices.append(to_token_price(raw, fetched_at=fetched_at))

    with connect(db_path) as conn:
        create_tables(conn)
        written = write_token_prices(conn, prices)

    elapsed = time.time() - start_time

    result = FetchResult(
        chain=chain,
        token_address=token_address.lower(),
        span_days=span_days,
        period=period,
        raw_points_seen=len(entries),
        chunks_fetched=n_chunks,
        mapped=len(prices),
        duplicate_after_alignment=len(prices) - written,
        written=written,
        elapsed_seconds=round(elapsed, 3),
    )

    logger.info(
        "fetch_and_persist_prices: chain=%s token=%s span=%dd period=%s chunks=%d "
        "seen=%d mapped=%d written=%d duplicate_after_alignment=%d elapsed=%.2fs",
        result.chain,
        result.token_address,
        result.span_days,
        result.period,
        result.chunks_fetched,
        result.raw_points_seen,
        result.mapped,
        result.written,
        result.duplicate_after_alignment,
        result.elapsed_seconds,
    )

    return result


# ---------- CLI ----------


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI arg parser. Extracted so tests can call it in isolation."""
    parser = argparse.ArgumentParser(
        description="Fetch historical token prices from DefiLlama and persist to DuckDB.",
    )
    parser.add_argument(
        "--chain",
        required=True,
        choices=["ethereum", "base"],
        help="Target chain (V1 scope per CLAUDE.md Section 2).",
    )
    parser.add_argument(
        "--token",
        default=DEFAULT_TOKENS["UNI"],
        help=f"Token contract address. Default: UNI ({DEFAULT_TOKENS['UNI']}).",
    )
    parser.add_argument(
        "--span-days",
        type=int,
        default=DEFAULT_SPAN_DAYS,
        help=f"Days of history to fetch. Default: {DEFAULT_SPAN_DAYS}.",
    )
    parser.add_argument(
        "--period",
        default=DEFAULT_PERIOD,
        help=f"Sampling interval. Default: {DEFAULT_PERIOD}.",
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
        help="Log per-chunk fetch and write progress at INFO level.",
    )
    return parser


def _validate_range_args(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> tuple[int, int, int]:
    """Validate the span/period arguments and return the resulting chunk plan.

    Named to mirror AW_01's and AW_02's validators, though this fetcher's range
    is a span in days rather than a block range.

    Returns:
        (total_points, n_chunks, chunk_span) — the same plan fetch_chart will
        follow, so the CLI summary can state it before any request is made.
    """
    if args.span_days <= 0:
        parser.error(f"--span-days must be positive, got {args.span_days}")
    try:
        return chunk_plan(args.span_days, args.period)
    except ValueError as e:
        parser.error(str(e))
        raise  # unreachable: parser.error exits, but keeps the type checker happy


def main() -> None:
    """CLI entry point for the DefiLlama historical prices fetcher."""
    parser = _build_arg_parser()
    args = parser.parse_args()
    total_points, n_chunks, _chunk_span = _validate_range_args(args, parser)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    print(
        f"AlphaWallets — DefiLlama Historical Prices\n"
        f"  Chain:  {args.chain}\n"
        f"  Token:  {args.token}\n"
        # RUF001: the multiplication sign is intentional in user-facing output
        f"  Span:   {args.span_days} days × {args.period} = {total_points} points "  # noqa: RUF001
        f"→ {n_chunks} chunk{'s' if n_chunks != 1 else ''} (500-cap per request)\n"
    )

    result = fetch_and_persist_prices(
        chain=args.chain,
        token_address=args.token,
        span_days=args.span_days,
        period=args.period,
        db_path=args.db_path,
    )

    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
