"""Pydantic models for historical token prices.

Two models are exposed:
- RawPricePoint: one entry from DefiLlama's /chart response, plus the context
  needed to identify it (chain, token, and the coin-level metadata the API
  reports once per coin rather than per point).
- TokenPrice: the normalized, hour-aligned row written to the token_price
  table and joined by the PnL pipeline.

Design notes:
- Token addresses are lowercase 0x-prefixed hex, 42 chars — same convention as
  every other fetcher.
- DefiLlama reports `confidence` and `symbol` once per coin, not per price
  point. Both are copied onto every RawPricePoint so a single point carries
  everything needed to interpret it.
- `timestamp` on RawPricePoint is the API's raw Unix seconds, deliberately
  unrounded. The API does NOT return hour-aligned timestamps even with
  period=1h (observed spacing ~58 minutes), so alignment is a mapping step,
  not a formality.
- TokenPrice.ts is hour-aligned UTC and validated as such: the PnL join is
  date_trunc('hour', block_timestamp), so a stray unaligned row would silently
  never match.
- `source` distinguishes providers inside the primary key, so a CoinGecko
  fallback can coexist with DefiLlama rows for the same (chain, token, hour).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from alphawallets.config import Chain

ADDRESS_PATTERN = r"^0x[0-9a-f]{40}$"


class RawPricePoint(BaseModel):
    """One price observation as DefiLlama's /chart endpoint reported it.

    Not persisted directly — kept as an explicit intermediate so mapping is
    testable without HTTP, and so the unrounded timestamp is visible for
    debugging alignment.
    """

    model_config = ConfigDict(frozen=True)

    chain: Chain
    token_address: str = Field(pattern=ADDRESS_PATTERN)
    timestamp: int = Field(gt=0, description="Raw Unix seconds from the API, not hour-aligned")
    price: float = Field(ge=0, description="USD price as reported")
    confidence: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="DefiLlama's coin-level confidence score (0-1), copied onto each point",
    )
    symbol: str | None = Field(default=None, description="Token symbol reported by the API")
    decimals: int | None = Field(default=None, ge=0, description="Token decimals reported by API")

    @field_validator("token_address", mode="before")
    @classmethod
    def _lowercase_hex(cls, v: str) -> str:
        return v.lower() if isinstance(v, str) else v


class TokenPrice(BaseModel):
    """An hour-aligned USD price row, ready for the token_price table.

    Consumed by the PnL pipeline, which joins on
    date_trunc('hour', block_timestamp) — hence the alignment invariant.
    """

    model_config = ConfigDict(frozen=True)

    chain: Chain
    token_address: str = Field(pattern=ADDRESS_PATTERN)
    ts: datetime = Field(description="Hour-aligned UTC timestamp")
    price_usd: float = Field(ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)
    source: str = Field(default="defillama", description="Price provider; part of the primary key")
    fetched_at: datetime = Field(description="When this row was retrieved, for staleness checks")

    @field_validator("token_address", mode="before")
    @classmethod
    def _lowercase_hex(cls, v: str) -> str:
        return v.lower() if isinstance(v, str) else v

    @field_validator("ts")
    @classmethod
    def _must_be_hour_aligned_utc(cls, v: datetime) -> datetime:
        """Reject anything the PnL join would silently miss."""
        if v.tzinfo is None:
            raise ValueError("ts must be timezone-aware (UTC)")
        if (v.minute, v.second, v.microsecond) != (0, 0, 0):
            raise ValueError(f"ts must be hour-aligned, got {v.isoformat()}")
        return v

    @field_validator("fetched_at")
    @classmethod
    def _must_be_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware (UTC)")
        return v
