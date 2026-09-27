"""Decoder for Uniswap V3 Swap logs.

Converts raw eth_getLogs entries into RawSwapLog (verbatim audit trail) and
UniswapV3Swap (decoded, ready for pipeline consumption).

Design notes:
- The web3 contract's event decoder is built once per fetcher run and reused
  for every log. Constructing it is cheap but not free, and doing it per-log
  would show up in profiles for large backfills.
- Reorg-removed logs (removed=True) are still written to the raw table for
  audit purposes but MUST be excluded from the decoded table by the caller
  — that's the pipeline's source of truth.
- block_timestamp comes from the log itself (Alchemy includes it), so no
  extra RPC call is needed to enrich decoded rows.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from web3 import Web3
from web3.contract.contract import ContractEvent

from alphawallets.config import Chain
from alphawallets.fetchers.uniswap_v3.models import RawSwapLog, UniswapV3Swap

ABI_PATH = Path(__file__).parent / "abis" / "uniswap_v3_pool.json"


def _load_pool_abi() -> list[dict[str, Any]]:
    """Load the Uniswap V3 Pool ABI from the shipped JSON."""
    with ABI_PATH.open() as f:
        return json.load(f)


def make_swap_event_decoder(w3: Web3) -> ContractEvent:
    """Build a reusable Swap event decoder.

    Call once per fetcher run and pass the returned object into
    decode_swap_log for every log.
    """
    abi = _load_pool_abi()
    # Contract address doesn't matter for event decoding — the Swap event
    # signature is shared across all V3 pools.
    contract = w3.eth.contract(abi=abi)
    return contract.events.Swap()


def _hex_str(value: Any) -> str:
    """Coerce HexBytes / bytes / str to a lowercase 0x-prefixed hex string."""
    if hasattr(value, "to_0x_hex"):
        return value.to_0x_hex().lower()
    if isinstance(value, bytes):
        return "0x" + value.hex().lower()
    if isinstance(value, str):
        return value.lower() if value.startswith("0x") else "0x" + value.lower()
    raise TypeError(f"Cannot convert {type(value).__name__} to hex string")


def _parse_block_timestamp(raw_ts: Any) -> datetime:
    """Alchemy returns blockTimestamp as a hex string like '0x66f5...'."""
    if isinstance(raw_ts, str) and raw_ts.startswith("0x"):
        ts = int(raw_ts, 16)
    elif isinstance(raw_ts, int):
        ts = raw_ts
    else:
        raise TypeError(f"Unexpected blockTimestamp type: {type(raw_ts).__name__}")
    return datetime.fromtimestamp(ts, tz=UTC)


def to_raw_swap_log(log: dict[str, Any], chain: Chain) -> RawSwapLog:
    """Convert an eth_getLogs entry into a validated RawSwapLog.

    Faithful copy — every field the node returned is preserved. Not opinionated
    about whether the log is reorged; that's carried in the `removed` flag.
    """
    return RawSwapLog(
        chain=chain,
        block_number=log["blockNumber"],
        block_hash=_hex_str(log["blockHash"]),
        tx_hash=_hex_str(log["transactionHash"]),
        log_index=log["logIndex"],
        transaction_index=log["transactionIndex"],
        address=_hex_str(log["address"]),
        topics=[_hex_str(t) for t in log["topics"]],
        data=_hex_str(log["data"]),
        removed=bool(log.get("removed", False)),
    )


def decode_swap_log(
    log: dict[str, Any],
    swap_event: ContractEvent,
    chain: Chain,
) -> UniswapV3Swap:
    """Decode one raw Swap log into a UniswapV3Swap.

    Args:
        log: An eth_getLogs entry (dict with topics, data, block metadata).
        swap_event: The decoder built once via make_swap_event_decoder(w3).
        chain: The chain this log came from (ethereum or base).

    Returns:
        A validated UniswapV3Swap.

    Raises:
        ValueError: If the log's blockTimestamp is missing (Alchemy includes
            it by default, but a stripped node could omit it).
    """
    if "blockTimestamp" not in log:
        raise ValueError(
            "Log is missing blockTimestamp. Alchemy includes it by default; "
            "if this fires, the RPC provider is stripping it and the fetcher "
            "needs an eth_getBlockByNumber fallback."
        )

    processed = swap_event.process_log(log)
    args = processed["args"]

    return UniswapV3Swap(
        chain=chain,
        block_number=log["blockNumber"],
        block_timestamp=_parse_block_timestamp(log["blockTimestamp"]),
        tx_hash=_hex_str(log["transactionHash"]),
        log_index=log["logIndex"],
        pool_address=_hex_str(log["address"]),
        sender=_hex_str(args["sender"]),
        recipient=_hex_str(args["recipient"]),
        amount0=int(args["amount0"]),
        amount1=int(args["amount1"]),
        sqrt_price_x96=int(args["sqrtPriceX96"]),
        liquidity=int(args["liquidity"]),
        tick=int(args["tick"]),
    )
