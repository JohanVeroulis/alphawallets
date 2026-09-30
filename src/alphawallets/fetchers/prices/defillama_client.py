"""DefiLlama /chart client for bulk historical prices.

Route: `/chart/{coins}?start=&span=&period=` returns a price timeseries per
coin. Chosen over `/prices/historical/{timestamp}/{coins}` because one request
covers hundreds of points; the per-timestamp endpoint is reserved for
fill-gap cases. Coverage for V1 tokens was verified in ADR 0008.

Three API quirks this module absorbs, all found by live probe on 2026-09-30:

1. `span` is a POINT COUNT, not a duration. `span=30d` returns 30 points, not
   30 days of them — the "d" is silently ignored.
2. A single request is capped at 500 points. Asking for more returns HTTP 400:
   {"message":"Requested 720 data points exceeds the maximum of 500."}
   So 30 days hourly (720 points) must be split across requests.
3. Timestamps are NOT hour-aligned even with `period=1h` (observed spacing
   ~58 minutes). Alignment happens in the mapper, not here.

Two layers:
- fetch_chart_chunk: one HTTP request, span must be <= MAX_POINTS_PER_REQUEST.
- fetch_chart: takes a span in days, splits into chunks, walks `start`
  forward, concatenates and de-duplicates. Callers never see the chunking.

Coin-level metadata (symbol, decimals, confidence) is reported once per coin by
the API, not per point. This module copies it onto every returned entry so the
mapper can build a self-contained RawPricePoint from one dict.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFILLAMA_BASE = "https://coins.llama.fi"

# A single /chart request cannot exceed this many points. Verified 2026-09-30:
# span=720 returns HTTP 400 {"message":"Requested 720 data points exceeds the
# maximum of 500."} — mirrors AW_01's BLOCK_WINDOW_SIZE = 10 (ADR 0006).
MAX_POINTS_PER_REQUEST = 500

HOURS_PER_DAY = 24

# Retry policy for transient failures. Manual backoff rather than a new
# dependency (tenacity): three attempts is enough for a rate limit or a blip,
# and keeping it inline makes the behaviour obvious at the call site.
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

_PERIOD_SECONDS: dict[str, int] = {
    "1h": 3600,
    "4h": 4 * 3600,
    "1d": 86400,
}


def coin_id(chain: str, token_address: str) -> str:
    """Build DefiLlama's coin identifier: '{chain}:{address}'."""
    return f"{chain}:{token_address.lower()}"


def period_seconds(period: str) -> int:
    """Return the number of seconds one `period` spans.

    Raises:
        ValueError: For a period this client doesn't know how to advance by.
            Chunking needs the duration to walk `start` forward, so an unknown
            period is a hard error rather than a guess.
    """
    if period not in _PERIOD_SECONDS:
        raise ValueError(f"Unsupported period {period!r}. Supported: {sorted(_PERIOD_SECONDS)}")
    return _PERIOD_SECONDS[period]


def fetch_chart_chunk(
    client: httpx.Client,
    chain: str,
    token_address: str,
    span: int,
    period: str = "1h",
    start_ts: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch one page of the price chart — a single HTTP request.

    Args:
        client: An httpx.Client (caller owns its lifetime and timeout).
        chain: DefiLlama chain name, e.g. 'ethereum'.
        token_address: Token contract address.
        span: Number of points to request. Must be <= MAX_POINTS_PER_REQUEST.
        period: Sampling interval, e.g. '1h'.
        start_ts: Unix seconds to anchor the window. None lets the API choose
            (it returns the most recent `span` points).

    Returns:
        A list of entries, each carrying the point's `timestamp` and `price`
        plus the coin-level `symbol`, `decimals` and `confidence`.

    Raises:
        ValueError: If span exceeds the per-request cap, or the API rejects the
            request, or the response omits the requested coin.
        httpx.HTTPError: On a transport failure that outlived the retries.
    """
    if span > MAX_POINTS_PER_REQUEST:
        raise ValueError(
            f"Requested {span} data points exceeds the maximum of "
            f"{MAX_POINTS_PER_REQUEST} per request; split the range into chunks"
        )
    if span <= 0:
        raise ValueError(f"span must be positive, got {span}")

    cid = coin_id(chain, token_address)
    params: dict[str, Any] = {"span": span, "period": period}
    if start_ts is not None:
        params["start"] = start_ts

    response = _get_with_retry(client, f"{DEFILLAMA_BASE}/chart/{cid}", params)

    if response.status_code == 400:
        # The cap message is the useful one; surface it verbatim.
        raise ValueError(f"DefiLlama rejected the request for {cid}: {_error_message(response)}")
    response.raise_for_status()

    payload = response.json()
    coins = payload.get("coins") or {}
    entry = coins.get(cid)
    if entry is None:
        raise ValueError(
            f"DefiLlama returned no data for {cid}. "
            f"Coins present: {sorted(coins) or 'none'} — check chain prefix and address."
        )

    symbol = entry.get("symbol")
    decimals = entry.get("decimals")
    confidence = entry.get("confidence")

    # Flatten coin-level metadata onto every point so the mapper works from a
    # single dict per observation.
    return [
        {
            "timestamp": point["timestamp"],
            "price": point["price"],
            "symbol": symbol,
            "decimals": decimals,
            "confidence": confidence,
        }
        for point in entry.get("prices", [])
    ]


def fetch_chart(
    client: httpx.Client,
    chain: str,
    token_address: str,
    span_days: int,
    period: str = "1h",
) -> list[dict[str, Any]]:
    """Fetch a full price history, splitting across requests as needed.

    Converts `span_days` into a point count, divides it into chunks no larger
    than MAX_POINTS_PER_REQUEST, and walks `start` forward by
    chunk_size * period_seconds. Results are concatenated, de-duplicated on raw
    timestamp (chunk boundaries can overlap by a point), and returned in
    chronological order.

    Args:
        client: An httpx.Client.
        chain: DefiLlama chain name.
        token_address: Token contract address.
        span_days: How many days of history to cover.
        period: Sampling interval, e.g. '1h'.

    Returns:
        Chronologically sorted entries, one per distinct raw timestamp.

    Raises:
        ValueError: On a non-positive span_days, an unsupported period, or an
            API rejection.
    """
    if span_days <= 0:
        raise ValueError(f"span_days must be positive, got {span_days}")

    step = period_seconds(period)
    total_points = math.ceil(span_days * 86400 / step)
    n_chunks = math.ceil(total_points / MAX_POINTS_PER_REQUEST)
    # Balance the chunks rather than filling the first ones to the cap: an even
    # split keeps every request the same shape, which makes the logs readable.
    chunk_span = math.ceil(total_points / n_chunks)

    start_ts = int((datetime.now(tz=UTC) - timedelta(days=span_days)).timestamp())

    logger.info(
        "Fetching %s: %d days at %s = %d points -> %d chunk(s) of %d (cap %d per request)",
        coin_id(chain, token_address),
        span_days,
        period,
        total_points,
        n_chunks,
        chunk_span,
        MAX_POINTS_PER_REQUEST,
    )

    seen_timestamps: set[int] = set()
    collected: list[dict[str, Any]] = []

    for index in range(n_chunks):
        chunk = fetch_chart_chunk(
            client,
            chain,
            token_address,
            span=chunk_span,
            period=period,
            start_ts=start_ts,
        )
        new_points = [p for p in chunk if p["timestamp"] not in seen_timestamps]
        seen_timestamps.update(p["timestamp"] for p in new_points)
        collected.extend(new_points)

        logger.info(
            "chunk %d/%d: start=%s span=%d -> %d points (%d new, %d duplicate)",
            index + 1,
            n_chunks,
            datetime.fromtimestamp(start_ts, tz=UTC).isoformat(),
            chunk_span,
            len(chunk),
            len(new_points),
            len(chunk) - len(new_points),
        )

        start_ts += chunk_span * step

    collected.sort(key=lambda p: p["timestamp"])
    logger.info(
        "Fetched %d distinct points for %s across %d chunk(s)",
        len(collected),
        coin_id(chain, token_address),
        n_chunks,
    )
    return collected


# ---------- Internals ----------


def _error_message(response: httpx.Response) -> str:
    """Pull DefiLlama's error text out of a response, falling back to the body."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("message") or body)
    return str(body)[:200]


def _get_with_retry(
    client: httpx.Client,
    url: str,
    params: dict[str, Any],
) -> httpx.Response:
    """GET with exponential backoff on rate limits and server errors.

    A 400 is returned to the caller rather than retried — it means the request
    itself is wrong, and repeating it wastes time.
    """
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.get(url, params=params)
        except httpx.HTTPError as e:
            last_error = e
            if attempt == MAX_ATTEMPTS:
                raise
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "Transport error on attempt %d/%d (%s); retrying in %.1fs",
                attempt,
                MAX_ATTEMPTS,
                type(e).__name__,
                delay,
            )
            time.sleep(delay)
            continue

        if response.status_code not in RETRY_STATUS_CODES:
            return response

        if attempt == MAX_ATTEMPTS:
            logger.warning(
                "Giving up after %d attempts; last status %d", MAX_ATTEMPTS, response.status_code
            )
            return response

        delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        logger.warning(
            "HTTP %d on attempt %d/%d; retrying in %.1fs",
            response.status_code,
            attempt,
            MAX_ATTEMPTS,
            delay,
        )
        time.sleep(delay)

    # Unreachable: the loop either returns or raises.
    raise RuntimeError("retry loop exited without a response") from last_error
