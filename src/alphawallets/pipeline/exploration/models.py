"""Models for the wallet activity proof: one wallet event, priced or not.

The bottom of this package's layering, imported by both queries.py and the
wallet_activity_proof orchestrator and importing neither — the same shape the
fetchers use (models -> mapper/writer -> aw_XX).

Price coverage is three-state, not two. The price grid is hour-aligned and only
as current as DefiLlama's most recent published hour, so an event in the current
incomplete hour is legitimately unpriced and will be priced by the next
backfill. That is "pending", and it is not the same failure as "unavailable" —
an hour in the past with no price row, which means the backfill has a real gap.
Measured on the first live run: 43/50 UNI/WETH swaps priced, the other 7 all in
the current hour. Collapsing those two states would have read as 86% coverage
with no way to tell a lag from a defect.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# UNI on Ethereum: a tracked DeFi token and a tracked airdrop (CLAUDE.md
# Section 2), and the only token with swap, transfer and price data in the cache.
UNI_ETHEREUM = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"

EventType = Literal["swap", "transfer"]
Direction = Literal["in", "out", "sell", "buy"]
PriceStatus = Literal["priced", "pending", "unavailable"]

# Which directions each event type is allowed to carry. A transfer moves tokens
# in or out of the wallet; a swap exchanges them, so it buys or sells.
_DIRECTIONS_BY_TYPE: dict[str, set[str]] = {
    "transfer": {"in", "out"},
    "swap": {"buy", "sell"},
}


class Event(BaseModel):
    """One thing a wallet did with one token, priced where a price exists.

    Frozen, like every model in the project: an event is an observation of
    something that already happened, and a pipeline stage that rewrites one is a
    bug rather than a feature.

    Two invariants are enforced here rather than trusted to the query layer,
    because both would otherwise produce a plausible-looking timeline:

    1. direction must match event_type — a transfer is in/out, a swap is
       buy/sell. A "swap in" would mean the classification logic crossed wires.
    2. price_status, price_usd and value_usd must agree. 'priced' requires a
       price; 'pending' and 'unavailable' require its absence. A row claiming
       to be priced with no price would inflate coverage silently.
    """

    model_config = ConfigDict(frozen=True)

    ts: datetime = Field(description="Block timestamp, timezone-aware UTC")
    event_type: EventType
    direction: Direction
    amount_token: Decimal = Field(description="Token amount in whole units, decimals applied")
    price_usd: float | None = Field(
        default=None, description="USD price for the event's hour, None unless price_status=priced"
    )
    value_usd: Decimal | None = Field(
        default=None, description="amount_token * price_usd, None when unpriced"
    )
    price_status: PriceStatus
    tx_hash: str
    counterparty: str | None = Field(
        default=None, description="The other address in a transfer. None for swaps"
    )
    other_amount: Decimal | None = Field(
        default=None, description="The paired leg of a swap. None for transfers"
    )
    other_token: str | None = Field(
        default=None, description="Symbol or address of the paired leg. None for transfers"
    )
    amount_approximate: bool = Field(
        default=False,
        description=(
            "True when the token's decimals were assumed rather than recorded, so "
            "amount_token may be misscaled. Rendered as (~) in the timeline."
        ),
    )

    @field_validator("ts")
    @classmethod
    def _must_be_utc_aware(cls, v: datetime) -> datetime:
        """Reject naive timestamps, which compare wrongly against the price grid."""
        if v.tzinfo is None:
            raise ValueError("ts must be timezone-aware (UTC)")
        return v

    @field_validator("tx_hash", "counterparty")
    @classmethod
    def _lowercase_addresses(cls, v: str | None) -> str | None:
        """Normalise hex to lowercase so comparisons and grouping behave.

        The cache stores lowercase throughout, but an Event can also be built in
        a test or a notebook, and a mixed-case tx_hash would quietly fail to
        match one read from the DB.
        """
        return v.lower() if v is not None else None

    @model_validator(mode="before")
    @classmethod
    def _check_coherence(cls, data: Any) -> Any:
        """Enforce the direction/type and price_status/price agreements.

        Runs in 'before' mode because the model is frozen: this is the last point
        at which the incoming fields can be inspected together, and value_usd is
        derived here rather than recomputed by every caller.
        """
        if not isinstance(data, dict):
            return data

        event_type = data.get("event_type")
        direction = data.get("direction")
        allowed = _DIRECTIONS_BY_TYPE.get(event_type) if event_type else None
        if allowed is not None and direction is not None and direction not in allowed:
            raise ValueError(
                f"direction {direction!r} is not valid for event_type {event_type!r}; "
                f"expected one of {sorted(allowed)}"
            )

        status = data.get("price_status")
        price = data.get("price_usd")
        if status == "priced" and price is None:
            raise ValueError("price_status='priced' requires a price_usd")
        if status in {"pending", "unavailable"} and price is not None:
            raise ValueError(
                f"price_status={status!r} must not carry a price_usd, got {price!r}. "
                "A priced event is 'priced'; the other two mean no price exists."
            )

        # Derive value_usd once, here, so no caller has to remember that a
        # Decimal amount cannot be multiplied by a float price directly.
        if price is not None and data.get("value_usd") is None:
            amount = data.get("amount_token")
            if amount is not None:
                data["value_usd"] = Decimal(str(amount)) * Decimal(str(price))

        return data


def classify_price_status(
    event_hour: datetime,
    has_price_row: bool,
    now_utc: datetime | None = None,
) -> PriceStatus:
    """Decide whether an unpriced event is a lag or a gap.

    The price grid is hour-aligned and trails the chain head: DefiLlama publishes
    an hour's point after that hour, so events in the hour currently in progress
    have no price row yet and will get one on the next backfill. Any earlier hour
    without a price row is a genuine hole in the backfill.

    Args:
        event_hour: The event's timestamp truncated to the hour, UTC.
        has_price_row: Whether token_price had a row for that hour.
        now_utc: Current time, injectable so tests are deterministic. Defaults to
            datetime.now(UTC).

    Returns:
        'priced' when a price exists, 'pending' for the current incomplete hour,
        'unavailable' for a past hour with no price.
    """
    if has_price_row:
        return "priced"

    reference = now_utc if now_utc is not None else datetime.now(tz=UTC)
    current_hour = reference.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    if event_hour.astimezone(UTC) >= current_hour:
        return "pending"
    return "unavailable"
