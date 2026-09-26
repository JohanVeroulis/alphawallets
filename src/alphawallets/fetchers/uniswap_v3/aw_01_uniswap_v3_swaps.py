"""AW_01 — Uniswap V3 Swap events fetcher.

Fetches Swap events from configured Uniswap V3 pools on Ethereum and Base,
decodes them via the pool ABI, and writes both raw logs and decoded rows
to DuckDB.

Chains: ethereum, base (V1 scope, per CLAUDE.md Section 2)
Block-range strategy: fixed window of BLOCK_WINDOW_SIZE blocks per
    eth_getLogs request. Alchemy's free tier caps eth_getLogs at 10
    blocks per call (measured 2026-09-26, both chains). See ADR 0006
    for the discovery, backfill implications, and V1.5 alternatives.
ABI: shipped with the package at ./abis/uniswap_v3_pool.json (sourced
    from @uniswap/v3-core@1.0.1). Shared across all V3 pools.
Output tables: raw_uniswap_v3_swap (audit trail), uniswap_v3_swap
    (decoded, pipeline input). Decoding + writing land in a later step.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from web3 import Web3
from web3.types import FilterParams, LogReceipt

from alphawallets.alchemy_client import make_web3
from alphawallets.config import Chain

logger = logging.getLogger(__name__)

# ---------- Constants ----------

# Alchemy free-tier cap; see ADR 0006. Any value >10 will 400 on both chains.
BLOCK_WINDOW_SIZE = 10

ABI_PATH = Path(__file__).parent / "abis" / "uniswap_v3_pool.json"

# The V1 default target: Uniswap V3 USDC/WETH 0.05% pool on Ethereum.
# See CLAUDE.md Section 2 for the tracked-pairs list.
DEFAULT_ETHEREUM_POOLS: dict[str, str] = {
    "USDC/WETH 0.05%": "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640",
}


# ---------- ABI loading ----------


def _load_pool_abi() -> list[dict[str, Any]]:
    """Load the Uniswap V3 Pool ABI from the shipped JSON."""
    with ABI_PATH.open() as f:
        return json.load(f)


def _get_swap_event_signature(w3: Web3) -> str:
    """Compute the Swap event topic0 (keccak256 of the canonical signature).

    Derives it from the ABI rather than hardcoding, so if the ABI ever
    changes we get a loud failure at load time, not silent wrong-topic
    filtering.
    """
    abi = _load_pool_abi()
    swap_events = [e for e in abi if e.get("type") == "event" and e.get("name") == "Swap"]
    if len(swap_events) != 1:
        raise RuntimeError(f"Expected exactly 1 Swap event in ABI, found {len(swap_events)}")
    swap = swap_events[0]
    input_types = ",".join(i["type"] for i in swap["inputs"])
    signature = f"Swap({input_types})"
    return w3.keccak(text=signature).to_0x_hex()


# ---------- Log fetching ----------


def _fetch_logs_window(
    w3: Web3,
    pool_address: str,
    swap_topic: str,
    from_block: int,
    to_block: int,
) -> list[LogReceipt]:
    """Fetch Swap logs for one block window."""
    filter_params: FilterParams = {
        "address": Web3.to_checksum_address(pool_address),
        "topics": [swap_topic],
        "fromBlock": from_block,
        "toBlock": to_block,
    }
    logs = w3.eth.get_logs(filter_params)
    logger.info(
        "Fetched %d Swap logs from blocks %d-%d for pool %s",
        len(logs),
        from_block,
        to_block,
        pool_address,
    )
    return logs


def fetch_swap_logs(
    chain: Chain,
    pool_address: str,
    from_block: int,
    to_block: int,
) -> list[LogReceipt]:
    """Fetch all Swap logs for a pool in [from_block, to_block] inclusive.

    Splits the range into BLOCK_WINDOW_SIZE-sized windows and calls
    eth_getLogs for each. Returns the concatenated log list in block
    order.

    Args:
        chain: The target chain, ethereum or base.
        pool_address: The Uniswap V3 pool contract address (any case).
        from_block: Start of the range (inclusive).
        to_block: End of the range (inclusive).

    Returns:
        A list of LogReceipt objects in block order.

    Raises:
        ValueError: If from_block > to_block.
        ConnectionError: From make_web3 if Alchemy is unreachable.
    """
    if from_block > to_block:
        raise ValueError(f"from_block ({from_block}) > to_block ({to_block})")

    w3 = make_web3(chain)
    swap_topic = _get_swap_event_signature(w3)

    all_logs: list[LogReceipt] = []
    window_start = from_block
    while window_start <= to_block:
        window_end = min(window_start + BLOCK_WINDOW_SIZE - 1, to_block)
        logs = _fetch_logs_window(w3, pool_address, swap_topic, window_start, window_end)
        all_logs.extend(logs)
        window_start = window_end + 1

    logger.info(
        "Fetched %d total Swap logs from blocks %d-%d on %s (%d windows)",
        len(all_logs),
        from_block,
        to_block,
        chain,
        (to_block - from_block) // BLOCK_WINDOW_SIZE + 1,
    )
    return all_logs
