"""Pydantic models for Uniswap V3 Swap events.

Two models are exposed:
- RawSwapLog: mirrors an Ethereum log entry as returned by eth_getLogs. Used
  for the raw_uniswap_v3_swap DuckDB table (audit trail; re-decode possible
  without re-fetch).
- UniswapV3Swap: the decoded Swap event, enriched with chain, pool_address
  and block_timestamp. Used for the uniswap_v3_swap DuckDB table (pipeline
  input).

Design notes:
- Addresses are stored as lowercase hex strings (0x-prefixed, 42 chars) for
  DuckDB VARCHAR compatibility and readability.
- Amounts and price fields are Python int (arbitrary precision). sqrt_price_x96
  is uint160 and does not fit any DuckDB integer type; liquidity is uint128 and
  exceeds DuckDB's signed HUGEINT (2^127) range for values in its upper half.
  Both are written to DuckDB as VARCHAR to avoid overflow. amount0/amount1
  (int256) do not fit HUGEINT either and are also stored as VARCHAR.
- Timestamps are UTC datetime; block_number is int.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from alphawallets.config import Chain

ADDRESS_PATTERN = r"^0x[0-9a-f]{40}$"
TX_HASH_PATTERN = r"^0x[0-9a-f]{64}$"


class RawSwapLog(BaseModel):
    """Raw Ethereum log for a Uniswap V3 Swap event.

    Written verbatim to the raw_uniswap_v3_swap table. Enough data to re-decode
    later without hitting Alchemy again.
    """

    model_config = ConfigDict(frozen=True)

    chain: Chain
    block_number: int = Field(ge=0)
    block_hash: str = Field(pattern=TX_HASH_PATTERN)
    tx_hash: str = Field(pattern=TX_HASH_PATTERN)
    log_index: int = Field(ge=0)
    address: str = Field(pattern=ADDRESS_PATTERN, description="Emitting pool contract")
    topics: list[str] = Field(description="Event topics; topics[0] is the Swap signature")
    data: str = Field(description="Hex-encoded non-indexed event data")

    @field_validator("address", "block_hash", "tx_hash", mode="before")
    @classmethod
    def _lowercase_hex(cls, v: str) -> str:
        return v.lower() if isinstance(v, str) else v

    @field_validator("topics", mode="before")
    @classmethod
    def _lowercase_topics(cls, v: list) -> list:
        if isinstance(v, list):
            return [t.lower() if isinstance(t, str) else t for t in v]
        return v


class UniswapV3Swap(BaseModel):
    """Decoded Uniswap V3 Swap event.

    Written to the uniswap_v3_swap table. Consumed by pipeline stages.

    Sign convention on amount0/amount1: positive means tokens flowed INTO the
    pool, negative means tokens flowed OUT of the pool. One is positive and the
    other negative for every swap. Pool metadata (which token is token0 vs
    token1) lives elsewhere; this model is unopinionated about direction.
    """

    model_config = ConfigDict(frozen=True)

    chain: Chain
    block_number: int = Field(ge=0)
    block_timestamp: datetime = Field(description="UTC timestamp of the block")
    tx_hash: str = Field(pattern=TX_HASH_PATTERN)
    log_index: int = Field(ge=0, description="Log index within the block")
    pool_address: str = Field(pattern=ADDRESS_PATTERN)
    sender: str = Field(pattern=ADDRESS_PATTERN, description="Router or msg.sender")
    recipient: str = Field(pattern=ADDRESS_PATTERN, description="Recipient of the output tokens")
    amount0: int = Field(description="int256; signed. Positive = into pool.")
    amount1: int = Field(description="int256; signed. Positive = into pool.")
    sqrt_price_x96: int = Field(ge=0, description="uint160; post-swap sqrt(price) * 2^96")
    liquidity: int = Field(ge=0, description="uint128; pool liquidity at time of swap")
    tick: int = Field(description="int24; current pool tick after swap")

    @field_validator("pool_address", "sender", "recipient", "tx_hash", mode="before")
    @classmethod
    def _lowercase_hex(cls, v: str) -> str:
        return v.lower() if isinstance(v, str) else v
