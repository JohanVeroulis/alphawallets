"""Pydantic models for the PnL pipeline.

Three domain objects, all frozen:

- CostBasisLot — one acquisition sitting in a wallet's FIFO stack. Carries
  the quantity still available for consumption, the per-token USD cost basis
  at the time of acquisition, and the source (trading or airdrop). FIFO
  consumption removes from the oldest lot first, oldest-first across both
  sub-stacks regardless of source. The source tag on each Realization carries
  the trading/airdrop attribution for the PnL split (ADR 0012 decision 4).

- Realization — one consumption event (a sell swap, or an outflow that the
  engine chose to realize). Records the matched lot's cost basis against the
  realization-time price so trading and airdrop PnL can be summed per window
  by filtering on realized_at.

- WalletPnL — the output row written to the wallet_pnl table (ADR 0012
  schema), with the three caveat flags surfaced rather than silently applied
  (decisions 5, 6, 7).

Big integers (token quantities in base units) are stored as Decimal, not float.
Realized PnL in USD is float because the DuckDB column is DOUBLE and the error
from a Decimal-to-float conversion at the boundary is immaterial next to the
price-source error; keeping quantities exact is what matters because they feed
the next lot's unit_cost_usd division.

Timestamps are UTC datetime, enforced by validator. The ADR 0012 window
semantics (decision 9) depend on this: a realization enters the 30-day window
by comparing its realized_at against the window's UTC boundaries.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from alphawallets.config import Chain

ADDRESS_PATTERN = r"^0x[0-9a-f]{40}$"
TX_HASH_PATTERN = r"^0x[0-9a-f]{64}$"

LotSource = Literal["trading", "airdrop"]
"""Source of a lot in the FIFO stack.

- "trading": acquired via swap-in, or transfer-in from an address that is NOT
  a known airdrop distribution contract. Cost basis is the USD price at the
  acquisition timestamp (ADR 0012 decision 2).
- "airdrop": acquired via transfer-in FROM a known distribution contract.
  Cost basis is zero; selling realizes the full proceeds as airdrop PnL
  (decision 4).
"""


class CostBasisLot(BaseModel):
    """One acquisition in a wallet's FIFO stack for a single token.

    The source literal tags the lot's origin and is carried onto any Realization
    it produces, so trading and airdrop PnL can be summed per window by
    filtering Realization.source. Consumption order is unified FIFO
    (oldest-first) across sources — see cost_basis.py.
    """

    model_config = ConfigDict(frozen=True)

    acquired_at: datetime = Field(description="UTC timestamp of the acquisition event")
    qty_token: Decimal = Field(
        gt=0,
        description=(
            "Quantity remaining in this lot, in base units (not decimal-adjusted). "
            "Decreases as the lot is consumed by realizations. Decimal to preserve "
            "uint256 precision — a swap of 123456789012345678 base units of UNI "
            "would lose a digit under float."
        ),
    )
    unit_cost_usd: float = Field(
        ge=0,
        description=(
            "USD cost basis per ONE token (decimal-adjusted). For a trading lot, "
            "this is the price-at-receipt. For an airdrop lot, this is 0.0."
        ),
    )
    source: LotSource = Field(
        description="Trading or airdrop; carried onto each Realization for the PnL split"
    )

    @field_validator("acquired_at")
    @classmethod
    def _must_be_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("acquired_at must be timezone-aware (UTC)")
        return v


class Realization(BaseModel):
    """One consumption event against a FIFO lot.

    A single sell swap can produce multiple Realizations if it consumes across
    several lots (e.g. sold 100 UNI, consumed from a 60-UNI lot and a 40-UNI
    lot — two Realization rows). This granularity is what makes ADR 0012
    decision 9 (window-sliced PnL) work: the window filter is on realized_at,
    and partial-lot realizations attribute each matched quantity to the time
    it was actually realized rather than when the originating lot was opened.
    """

    model_config = ConfigDict(frozen=True)

    realized_at: datetime = Field(description="UTC timestamp of the realizing event")
    qty_token: Decimal = Field(
        gt=0,
        description="Quantity of this realization in base units (not decimal-adjusted)",
    )
    unit_cost_usd: float = Field(
        ge=0,
        description="Cost basis per token, carried from the consumed lot",
    )
    unit_sale_usd: float = Field(
        ge=0,
        description="USD price per token at realized_at (from the price join)",
    )
    source: LotSource = Field(description="Carried from the consumed lot; selects PnL bucket")
    pnl_usd: float = Field(
        description=(
            "(unit_sale_usd - unit_cost_usd) * qty_token (decimal-adjusted). "
            "Pre-computed so the writer and reports don't recompute per row."
        )
    )

    @field_validator("realized_at")
    @classmethod
    def _must_be_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("realized_at must be timezone-aware (UTC)")
        return v


class WalletPnL(BaseModel):
    """Output row for the wallet_pnl table (ADR 0012 schema).

    Granularity: per (chain, wallet, token_address, window). The leaderboard
    aggregates across tokens at report time; this row is the atomic unit
    (decision 8).

    `realized_pnl_usd` is the sum of trading + airdrop, stored rather than
    derived so DuckDB can ORDER BY without recomputing on every leaderboard
    query. The caveat flags are decisions 5, 6, 7 — surfaced on the row, not
    silently applied, so a reader can decide what to trust.
    """

    model_config = ConfigDict(frozen=True)

    chain: Chain
    wallet: str = Field(pattern=ADDRESS_PATTERN)
    token_address: str = Field(pattern=ADDRESS_PATTERN)
    window_start: datetime = Field(description="Window lower bound, UTC, inclusive")
    window_end: datetime = Field(description="Window upper bound, UTC, exclusive")

    # Split PnL: trading is the skill signal, airdrop is the luck signal
    realized_pnl_usd: float = Field(
        description="Sum of trading + airdrop realized PnL. Stored for ORDER BY."
    )
    realized_pnl_trading_usd: float = Field(description="Realized PnL from trading lots only")
    realized_pnl_airdrop_usd: float = Field(description="Realized PnL from airdrop lots only")

    # V1.5 reserved; always None in V1 (decision: realized only)
    unrealized_pnl_usd: float | None = Field(
        default=None,
        description="Mark-to-market unrealized PnL. V1.5+. Writer stores NULL.",
    )

    # Volume context — reading PnL without volume is meaningless (a $100 profit
    # on $100,000 turnover is a very different signal from $100 on $200)
    bought_usd: float = Field(ge=0, description="Sum of USD acquired via trading in-events")
    sold_usd: float = Field(ge=0, description="Sum of USD realized via sell-events")
    realization_count: int = Field(ge=0, description="Count of Realization rows in the window")

    # Position state at window_end
    balance_token: Decimal = Field(
        ge=0,
        description=(
            "Remaining balance in base units at window_end, summed across both "
            "sub-stacks. Decimal preserves uint256; writer serializes to VARCHAR."
        ),
    )
    avg_cost_basis_usd: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Weighted-average cost basis across remaining lots. None when "
            "balance_token is zero (division would be undefined)."
        ),
    )

    # Caveat flags — ADR 0012 decisions 5, 6, 7. Surfaced, never silently applied.
    has_pre_window_activity: bool = Field(
        default=False,
        description=(
            "True when the wallet already held a balance at window_start. The "
            "wallet is excluded from leaderboard rankings but PnL is still "
            "computed for visible events (decision 5)."
        ),
    )
    has_unpriceable_events: bool = Field(
        default=False,
        description=(
            "True when at least one acquisition or realization in-window could "
            "not be priced (unpriceable token, or outside the price grid). PnL "
            "covers the priced events only (decision 6)."
        ),
    )
    has_smart_wallet_signal: bool = Field(
        default=False,
        description=(
            "True when activity patterns suggest the EOA is signing for a Safe "
            "or ERC-4337 account rather than acting as its own position holder "
            "(decision 7). Detection logic lives in a later PR."
        ),
    )

    computed_at: datetime = Field(
        description=(
            "UTC timestamp of when this row was materialized. Lets a changed "
            "number be explained — re-running after a deeper backfill will "
            "legitimately alter earlier windows per ADR 0012 consequence 6."
        )
    )

    @field_validator("wallet", "token_address", mode="before")
    @classmethod
    def _lowercase_hex(cls, v: str) -> str:
        return v.lower() if isinstance(v, str) else v

    @field_validator("window_start", "window_end", "computed_at")
    @classmethod
    def _must_be_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("Timestamps must be timezone-aware (UTC)")
        return v
