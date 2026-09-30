"""Pydantic models for ERC-20 Transfer records from the Alchemy Transfers API.

Two models are exposed:
- RawAssetTransfer: mirrors an `alchemy_getAssetTransfers` result entry, verbatim.
  Used for the raw_erc20_transfer DuckDB table (audit trail; re-map possible
  without re-fetch).
- ERC20Transfer: the normalized transfer, joinable to swaps and prices. Used for
  the erc20_transfer DuckDB table (pipeline input).

Design notes:
- Addresses are lowercase 0x-prefixed hex strings, 42 chars — same convention
  as AW_01.
- `value_raw` is the token amount in base units (uint256 as string) taken from
  the API's `rawContract.value` field. Stored as VARCHAR because it does not
  fit DuckDB's signed HUGEINT for large-supply tokens.
- `value_decimal` is a human-readable float (API's `value` field). Convenient
  for spot-checks; not authoritative — always compute from value_raw + decimals
  in the pipeline.
- `unique_id` is Alchemy's globally unique transfer identifier. For ERC-20
  category it typically looks like `<tx_hash>:log:<log_index>`, but the
  Transfers API contract only guarantees uniqueness, not shape — do not parse
  it. Used as the fallback PK when log_index is absent.
- `log_index` is best-effort: Alchemy returns it inconsistently for the
  Transfers API. When present, it enables joining to raw event logs; when
  absent, unique_id carries dedup responsibility.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from alphawallets.config import Chain

ADDRESS_PATTERN = r"^0x[0-9a-f]{40}$"
TX_HASH_PATTERN = r"^0x[0-9a-f]{64}$"


class RawAssetTransfer(BaseModel):
    """Raw result entry from `alchemy_getAssetTransfers`.

    Written verbatim to the raw_erc20_transfer table. Preserves the API's
    original shape so re-mapping to ERC20Transfer is possible without
    re-hitting Alchemy.
    """

    model_config = ConfigDict(frozen=True)

    chain: Chain
    unique_id: str = Field(description="Alchemy's globally unique transfer identifier")
    block_num: int = Field(ge=0, description="Block number as int (API returns hex)")
    tx_hash: str = Field(pattern=TX_HASH_PATTERN)
    from_addr: str = Field(pattern=ADDRESS_PATTERN)
    to_addr: str = Field(pattern=ADDRESS_PATTERN)
    value_raw: str = Field(
        description="uint256 token amount in base units, as string (from rawContract.value)"
    )
    value_decimal: float | None = Field(
        default=None,
        description=(
            "Human-readable amount (API's 'value' field). None for very large or malformed values."
        ),
    )
    asset: str | None = Field(default=None, description="Symbol as reported by Alchemy, e.g. 'UNI'")
    category: str = Field(description="Transfer category: 'erc20' for this fetcher")
    contract_address: str = Field(pattern=ADDRESS_PATTERN, description="Token contract address")
    contract_decimal: int | None = Field(
        default=None, ge=0, description="Token decimals as reported by rawContract.decimal"
    )
    log_index: int | None = Field(
        default=None,
        ge=0,
        description="Log index within the block. Absent for some Transfers API results.",
    )
    block_timestamp: datetime | None = Field(
        default=None, description="UTC timestamp from metadata.blockTimestamp"
    )

    @field_validator("tx_hash", "from_addr", "to_addr", "contract_address", mode="before")
    @classmethod
    def _lowercase_hex(cls, v: str) -> str:
        return v.lower() if isinstance(v, str) else v


class ERC20Transfer(BaseModel):
    """Normalized ERC-20 transfer for pipeline consumption.

    Written to the erc20_transfer table. Consumed by wallet-activity summaries,
    airdrop attribution, and PnL cost-basis logic.
    """

    model_config = ConfigDict(frozen=True)

    chain: Chain
    block_number: int = Field(ge=0)
    block_timestamp: datetime = Field(description="UTC timestamp of the block")
    tx_hash: str = Field(pattern=TX_HASH_PATTERN)
    log_index: int | None = Field(
        default=None,
        ge=0,
        description="Log index within the block. None when Alchemy omits it.",
    )
    unique_id: str = Field(description="Alchemy dedup key; PK when log_index is None")
    token_address: str = Field(pattern=ADDRESS_PATTERN)
    from_addr: str = Field(pattern=ADDRESS_PATTERN)
    to_addr: str = Field(pattern=ADDRESS_PATTERN)
    value_raw: str = Field(description="uint256 amount in base units, stored as VARCHAR")
    token_decimals: int | None = Field(default=None, ge=0)

    @field_validator("tx_hash", "token_address", "from_addr", "to_addr", mode="before")
    @classmethod
    def _lowercase_hex(cls, v: str) -> str:
        return v.lower() if isinstance(v, str) else v
