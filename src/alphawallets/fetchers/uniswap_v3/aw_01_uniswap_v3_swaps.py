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
Output tables:
- raw_uniswap_v3_swap — verbatim audit trail from Alchemy, primary key
  (chain, block_hash, log_index) so reorged variants can coexist.
- uniswap_v3_swap — decoded, pipeline-ready rows, primary key
  (chain, tx_hash, log_index). Reorged logs (removed=True) are
  excluded here but kept in the raw table.

Idempotency: INSERT OR IGNORE on both tables, so overlapping re-runs
are safe (see ADR 0006 for why this matters during backfill).
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from web3 import Web3
from web3.types import FilterParams, LogReceipt

from alphawallets.alchemy_client import make_web3
from alphawallets.config import Chain
from alphawallets.db import connect
from alphawallets.fetchers.uniswap_v3.decoder import (
    decode_swap_log,
    make_swap_event_decoder,
    to_raw_swap_log,
)
from alphawallets.fetchers.uniswap_v3.writer import (
    create_tables,
    write_decoded_swaps,
    write_raw_logs,
)

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


class FetchResult(BaseModel):
    """Summary of a fetch_and_persist_swaps run.

    Returned to callers (CLI, scheduled jobs, tests) so they can log,
    assert, or report on what a run did.
    """

    model_config = ConfigDict(frozen=True)

    chain: str
    pool_address: str
    from_block: int = Field(ge=0)
    to_block: int = Field(ge=0)
    windows_fetched: int = Field(ge=0)
    raw_logs_written: int = Field(ge=0)
    raw_logs_skipped: int = Field(ge=0, description="Duplicates ignored on insert")
    decoded_swaps_written: int = Field(ge=0)
    decoded_swaps_skipped: int = Field(ge=0)
    reorged_logs_excluded: int = Field(
        ge=0, description="Logs with removed=True; kept in raw, excluded from decoded"
    )
    elapsed_seconds: float = Field(ge=0)


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


def fetch_and_persist_swaps(
    chain: Chain,
    pool_address: str,
    from_block: int,
    to_block: int,
    db_path: Path | str | None = None,
) -> FetchResult:
    """Fetch, decode, and persist Uniswap V3 Swap events for a pool.

    Orchestrates the full pipeline for one pool over a block range:
    1. Open DuckDB (creating tables if missing)
    2. For each BLOCK_WINDOW_SIZE window in [from_block, to_block]:
       a. eth_getLogs for the Swap event
       b. Convert each log to RawSwapLog and write to raw_uniswap_v3_swap
       c. Decode non-reorged logs and write to uniswap_v3_swap
    3. Return a FetchResult with counts.

    Writes happen per-window so a crash mid-run loses at most one window's
    progress. Idempotent: safe to re-run with overlapping ranges (INSERT
    OR IGNORE handles duplicates via primary keys).

    Reorged logs (removed=True) are still written to raw_uniswap_v3_swap
    for audit purposes, but excluded from uniswap_v3_swap since the pipeline
    layer treats the decoded table as canonical.

    Args:
        chain: ethereum or base.
        pool_address: Uniswap V3 pool address.
        from_block: Start block, inclusive.
        to_block: End block, inclusive.
        db_path: Optional DuckDB path override. Defaults to
            get_cache_db_path() from config.

    Returns:
        FetchResult with per-run counts and wall-clock time.

    Raises:
        ValueError: If from_block > to_block.
        ConnectionError: From make_web3 if Alchemy is unreachable.
    """
    if from_block > to_block:
        raise ValueError(f"from_block ({from_block}) > to_block ({to_block})")

    start_time = time.time()
    w3 = make_web3(chain)
    swap_topic = _get_swap_event_signature(w3)
    swap_event = make_swap_event_decoder(w3)

    raw_written = 0
    raw_skipped = 0
    decoded_written = 0
    decoded_skipped = 0
    reorged_excluded = 0
    windows_fetched = 0

    with connect(db_path) as conn:
        create_tables(conn)

        window_start = from_block
        while window_start <= to_block:
            window_end = min(window_start + BLOCK_WINDOW_SIZE - 1, to_block)

            logs = _fetch_logs_window(w3, pool_address, swap_topic, window_start, window_end)
            windows_fetched += 1

            if not logs:
                window_start = window_end + 1
                continue

            # Raw: everything, including reorged
            raw_records = [to_raw_swap_log(log, chain=chain) for log in logs]
            n_raw_inserted = write_raw_logs(conn, raw_records)
            raw_written += n_raw_inserted
            raw_skipped += len(raw_records) - n_raw_inserted

            # Decoded: exclude reorged
            live_logs = [log for log in logs if not log.get("removed", False)]
            reorged_excluded += len(logs) - len(live_logs)

            decoded_records = [decode_swap_log(log, swap_event, chain=chain) for log in live_logs]
            n_decoded_inserted = write_decoded_swaps(conn, decoded_records)
            decoded_written += n_decoded_inserted
            decoded_skipped += len(decoded_records) - n_decoded_inserted

            window_start = window_end + 1

    elapsed = time.time() - start_time

    result = FetchResult(
        chain=chain,
        pool_address=pool_address.lower(),
        from_block=from_block,
        to_block=to_block,
        windows_fetched=windows_fetched,
        raw_logs_written=raw_written,
        raw_logs_skipped=raw_skipped,
        decoded_swaps_written=decoded_written,
        decoded_swaps_skipped=decoded_skipped,
        reorged_logs_excluded=reorged_excluded,
        elapsed_seconds=round(elapsed, 3),
    )

    logger.info(
        "fetch_and_persist_swaps: chain=%s pool=%s blocks=%d-%d windows=%d "
        "raw_written=%d raw_skipped=%d decoded_written=%d decoded_skipped=%d "
        "reorged=%d elapsed=%.2fs",
        result.chain,
        result.pool_address,
        result.from_block,
        result.to_block,
        result.windows_fetched,
        result.raw_logs_written,
        result.raw_logs_skipped,
        result.decoded_swaps_written,
        result.decoded_swaps_skipped,
        result.reorged_logs_excluded,
        result.elapsed_seconds,
    )

    return result


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI arg parser. Extracted so tests can call it in isolation."""
    parser = argparse.ArgumentParser(
        description="Fetch Uniswap V3 Swap events and persist to DuckDB.",
    )
    parser.add_argument(
        "--chain",
        required=True,
        choices=["ethereum", "base"],
        help="Target chain (V1 scope per CLAUDE.md Section 2).",
    )
    parser.add_argument(
        "--pool",
        required=True,
        help="Uniswap V3 pool contract address (0x-prefixed, 42 chars).",
    )
    parser.add_argument(
        "--blocks",
        type=int,
        default=None,
        help="Number of blocks to fetch, ending at current head. "
        "Cannot be combined with --from-block / --to-block. "
        "If neither is given, defaults to 1000.",
    )
    parser.add_argument(
        "--from-block",
        type=int,
        default=None,
        help="Explicit start block, inclusive. Requires --to-block. "
        "Cannot be combined with --blocks.",
    )
    parser.add_argument(
        "--to-block",
        type=int,
        default=None,
        help="Explicit end block, inclusive. Requires --from-block. "
        "Cannot be combined with --blocks.",
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
        help="Log per-window fetch and write progress at INFO level.",
    )
    return parser


def _validate_range_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Validate range args and return the effective block count for head-relative mode.

    Returns:
        Number of blocks to fetch from head, or 0 if explicit range mode.
    """
    if (args.from_block is None) != (args.to_block is None):
        parser.error("--from-block and --to-block must be provided together")
    if args.blocks is not None and args.from_block is not None:
        parser.error("--blocks cannot be combined with --from-block / --to-block")
    if args.from_block is not None:
        return 0
    return args.blocks if args.blocks is not None else 1000


def main() -> None:
    """CLI entry point for the Uniswap V3 swap fetcher."""
    parser = _build_arg_parser()
    args = parser.parse_args()
    blocks_default = _validate_range_args(args, parser)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.from_block is not None:
        from_block = args.from_block
        to_block = args.to_block
    else:
        w3 = make_web3(args.chain)
        current = w3.eth.block_number
        from_block = current - blocks_default + 1
        to_block = current

    print(
        f"AlphaWallets — Uniswap V3 Swap Fetcher\n"
        f"  Chain:      {args.chain}\n"
        f"  Pool:       {args.pool}\n"
        f"  From block: {from_block:,}\n"
        f"  To block:   {to_block:,}\n"
        f"  Windows:    {(to_block - from_block) // BLOCK_WINDOW_SIZE + 1}\n"
    )

    result = fetch_and_persist_swaps(
        chain=args.chain,
        pool_address=args.pool,
        from_block=from_block,
        to_block=to_block,
        db_path=args.db_path,
    )

    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
