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
import math
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from duckdb import DuckDBPyConnection
from pydantic import BaseModel, ConfigDict, Field

from alphawallets.config import Chain
from alphawallets.db import connect
from alphawallets.fetchers.prices.defillama_client import chunk_plan, fetch_chart
from alphawallets.fetchers.prices.mapper import to_raw_price_point, to_token_price
from alphawallets.fetchers.prices.models import TokenPrice
from alphawallets.fetchers.prices.writer import create_tables, write_token_prices
from alphawallets.tokens import KNOWN_SYMBOLS, get_token_address

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


def get_resume_point(
    conn: DuckDBPyConnection,
    chain: str,
    token_address: str,
) -> datetime | None:
    """Return the newest priced hour already stored for this (chain, token), or None.

    Scoped to source='defillama' so a future CoinGecko backfill cannot make this
    fetcher believe it has already covered a span. The four-column primary key
    keeps both sources' rows side by side, which is exactly why the filter is
    needed here.

    Args:
        conn: Open DuckDB connection.
        chain: Chain name.
        token_address: Token contract, any case.

    Returns:
        The newest stored hour as a UTC datetime, or None when this fetcher has
        stored nothing for this (chain, token).
    """
    row = conn.execute(
        "SELECT MAX(ts) FROM token_price "
        "WHERE chain = ? AND lower(token_address) = ? AND source = 'defillama'",
        [chain, token_address.lower()],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return row[0].astimezone(UTC)


def span_days_since(resume_point: datetime, now_utc: datetime | None = None) -> int:
    """Days of history needed to reach back to a resume point, rounded up.

    Rounded up, and never below 1, so a partial day is covered rather than left
    as a hole. Overlap is free: the writer's INSERT OR IGNORE drops hours already
    stored, so erring long costs a few redundant points and erring short would
    leave a gap.

    Args:
        resume_point: Newest stored hour, from get_resume_point.
        now_utc: Current time, injectable for deterministic tests.

    Returns:
        A span in whole days, at least 1.
    """
    reference = now_utc if now_utc is not None else datetime.now(tz=UTC)
    elapsed = reference.astimezone(UTC) - resume_point.astimezone(UTC)
    return max(1, math.ceil(elapsed.total_seconds() / 86400))


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
        "--token-symbol",
        default=None,
        choices=sorted(KNOWN_SYMBOLS),
        help=(
            "Token symbol from the V1 registry, resolved against --chain. "
            "Preferred over --token. Cannot be combined with it."
        ),
    )
    parser.add_argument(
        "--token",
        # No argparse default: main() applies the UNI default itself, so the mutex
        # with --token-symbol can tell "not passed" from "passed the default".
        default=None,
        help=f"Token contract address. Default: UNI ({DEFAULT_TOKENS['UNI']}). "
        "Cannot be combined with --token-symbol.",
    )
    parser.add_argument(
        "--span-days",
        type=int,
        # No argparse default: main() applies DEFAULT_SPAN_DAYS itself, so the
        # mutex with --resume can tell "not passed" from "passed the default".
        default=None,
        help=f"Days of history to fetch. Default: {DEFAULT_SPAN_DAYS}. "
        "Cannot be combined with --resume.",
    )
    parser.add_argument(
        "--period",
        default=DEFAULT_PERIOD,
        help=f"Sampling interval. Default: {DEFAULT_PERIOD}.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Continue from the newest hour already stored for this chain and token. "
            "Falls back to the default span when nothing is stored yet. Cannot be "
            "combined with --span-days."
        ),
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


def _resolve_token(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[str, str]:
    """Resolve the target token to a contract address and a display label.

    --token-symbol is resolved against --chain through the V1 registry; --token
    is taken as an address. Neither given falls back to the UNI default, so every
    earlier invocation behaves identically.

    Returns:
        (token_address, label) — the label names the symbol when one was used, so
        the summary says which token this run was about rather than only its hex.
    """
    if args.token_symbol is not None:
        try:
            return get_token_address(args.token_symbol, args.chain), args.token_symbol.upper()
        except KeyError as e:
            # UnknownTokenError and TokenNotOnChainError both carry an actionable
            # message; argparse's exit is the right surface for a bad argument.
            parser.error(str(e).strip("\"'"))
            raise  # unreachable: parser.error exits
    if args.token is not None:
        return args.token, "(from --token)"
    return DEFAULT_TOKENS["UNI"], "UNI (default)"


def _validate_range_args(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    span_days: int | None = None,
) -> tuple[int, int, int]:
    """Validate the span/period arguments and return the resulting chunk plan.

    Named to mirror AW_01's and AW_02's validators, though this fetcher's range
    is a span in days rather than a block range.

    Args:
        args: Parsed CLI arguments.
        parser: The parser, used to exit with a usage message.
        span_days: Effective span, when main() has already resolved one from a
            resume point. Falls back to --span-days, then to DEFAULT_SPAN_DAYS.

    Returns:
        (total_points, n_chunks, chunk_span) — the same plan fetch_chart will
        follow, so the CLI summary can state it before any request is made.
    """
    if args.resume and args.span_days is not None:
        parser.error("--resume cannot be combined with --span-days")
    if args.token_symbol is not None and args.token is not None:
        parser.error("--token-symbol cannot be combined with --token")

    effective = span_days if span_days is not None else args.span_days
    if effective is None:
        effective = DEFAULT_SPAN_DAYS
    if effective <= 0:
        parser.error(f"--span-days must be positive, got {effective}")
    try:
        return chunk_plan(effective, args.period)
    except ValueError as e:
        parser.error(str(e))
        raise  # unreachable: parser.error exits, but keeps the type checker happy


def main() -> None:
    """CLI entry point for the DefiLlama historical prices fetcher."""
    parser = _build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Validate before any DB or network access, so a bad flag combination fails
    # immediately rather than after opening the cache.
    _validate_range_args(args, parser)

    token, token_label = _resolve_token(args, parser)

    span_days = args.span_days if args.span_days is not None else DEFAULT_SPAN_DAYS
    mode = (
        f"explicit span ({span_days} days)"
        if args.span_days is not None
        else (f"default span ({span_days} days)")
    )

    if args.resume:
        with connect(args.db_path) as conn:
            create_tables(conn)
            resume_point = get_resume_point(conn, args.chain, token)
        if resume_point is None:
            logger.info(
                "Resume: nothing stored for chain=%s token=%s; defaulting to %d days",
                args.chain,
                token,
                DEFAULT_SPAN_DAYS,
            )
            span_days = DEFAULT_SPAN_DAYS
            mode = f"resume — no previous data, defaulting to {DEFAULT_SPAN_DAYS} days"
        else:
            span_days = span_days_since(resume_point)
            logger.info(
                "Resume: newest stored hour %s; fetching %d day(s)",
                resume_point.isoformat(),
                span_days,
            )
            mode = f"resume from {resume_point.strftime('%Y-%m-%d %H:%MZ')}"

    total_points, n_chunks, _chunk_span = _validate_range_args(args, parser, span_days)

    print(
        f"AlphaWallets — DefiLlama Historical Prices\n"
        f"  Chain:  {args.chain}\n"
        f"  Token:  {token_label}\n"
        f"  Addr:   {token}\n"
        f"  Mode:   {mode}\n"
        # RUF001: the multiplication sign is intentional in user-facing output
        f"  Span:   {span_days} days × {args.period} = {total_points} points "  # noqa: RUF001
        f"→ {n_chunks} chunk{'s' if n_chunks != 1 else ''} (500-cap per request)\n"
    )

    result = fetch_and_persist_prices(
        chain=args.chain,
        token_address=token,
        span_days=span_days,
        period=args.period,
        db_path=args.db_path,
    )

    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
