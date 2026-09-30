"""Pure mapping functions for DefiLlama /chart entries.

No I/O, no HTTP — takes a flattened entry from defillama_client and produces
validated models:

    entry (dict) -> RawPricePoint -> TokenPrice

Hour-alignment lives here. The /chart endpoint does not return hour-aligned
timestamps even with period=1h (observed ~58 minute spacing), and the PnL
pipeline joins on date_trunc('hour', block_timestamp) — so every raw timestamp
is truncated down to its containing hour.

Truncation, not rounding: a point at 10:58 belongs to hour 10, not 11. Rounding
would attribute a price to an hour it was never observed in, and would shift
the first and last points of a range outside it.

Two adjacent raw points can land in the same hour (e.g. 10:02 and 10:58). This
module does not deduplicate — it returns both, with identical `ts`. The writer's
INSERT OR IGNORE resolves the collision, and the orchestrator counts it, so the
overlap stays visible instead of being silently dropped mid-pipeline.

Entries that can't be mapped return None rather than raising, so one malformed
point doesn't abort a chunk. Skips log at WARNING.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from alphawallets.config import Chain
from alphawallets.fetchers.prices.models import RawPricePoint, TokenPrice

logger = logging.getLogger(__name__)

DEFAULT_SOURCE = "defillama"


def align_to_hour(timestamp: int) -> datetime:
    """Truncate a Unix timestamp down to the start of its UTC hour.

    Args:
        timestamp: Unix seconds.

    Returns:
        A tz-aware UTC datetime with minute, second and microsecond zeroed.

    Example:
        >>> align_to_hour(1790747390).isoformat()  # 2026-09-30T10:29:50Z
        '2026-09-30T10:00:00+00:00'
    """
    return datetime.fromtimestamp(timestamp, tz=UTC).replace(minute=0, second=0, microsecond=0)


def to_raw_price_point(
    entry: dict[str, Any],
    chain: Chain,
    token_address: str,
) -> RawPricePoint | None:
    """Map one flattened /chart entry to a validated RawPricePoint.

    Args:
        entry: An element from defillama_client.fetch_chart — carries
            `timestamp` and `price` plus the coin-level `symbol`, `decimals`
            and `confidence`.
        chain: The chain the price was requested for.
        token_address: The token the price belongs to.

    Returns:
        A RawPricePoint, or None when the entry lacks a timestamp or price, or
        fails model validation. Skips are logged at WARNING.
    """
    timestamp = entry.get("timestamp")
    price = entry.get("price")

    if timestamp is None or price is None:
        logger.warning(
            "Skipping price point for %s: missing %s",
            token_address,
            "timestamp" if timestamp is None else "price",
        )
        return None

    try:
        return RawPricePoint(
            chain=chain,
            token_address=token_address,
            timestamp=int(timestamp),
            price=float(price),
            confidence=entry.get("confidence"),
            symbol=entry.get("symbol"),
            decimals=entry.get("decimals"),
        )
    except (TypeError, ValueError) as e:
        logger.warning("Skipping price point for %s at %s: %s", token_address, timestamp, e)
        return None


def to_token_price(
    raw: RawPricePoint,
    fetched_at: datetime | None = None,
    source: str = DEFAULT_SOURCE,
) -> TokenPrice:
    """Normalize a RawPricePoint into an hour-aligned TokenPrice.

    Always succeeds for a valid RawPricePoint: alignment cannot fail, and the
    remaining fields carry over unchanged. Unlike the ERC-20 mapper there is no
    None path, because a price point has no field that can be legitimately
    absent once the raw model has validated.

    Args:
        raw: A validated RawPricePoint.
        fetched_at: Retrieval time, for staleness checks. Defaults to now(UTC).
        source: Price provider. Part of the table's primary key, so a fallback
            provider can hold rows for the same (chain, token, hour).

    Returns:
        A TokenPrice whose `ts` is the start of the hour containing
        `raw.timestamp`.
    """
    return TokenPrice(
        chain=raw.chain,
        token_address=raw.token_address,
        ts=align_to_hour(raw.timestamp),
        price_usd=raw.price,
        confidence=raw.confidence,
        source=source,
        fetched_at=fetched_at if fetched_at is not None else datetime.now(tz=UTC),
    )
