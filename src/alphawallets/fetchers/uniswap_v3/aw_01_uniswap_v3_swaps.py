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

from duckdb import DuckDBPyConnection
from pydantic import BaseModel, ConfigDict, Field
from web3 import Web3
from web3.types import FilterParams, LogReceipt

from alphawallets.alchemy_client import make_web3
from alphawallets.config import Chain
from alphawallets.db import connect
from alphawallets.fetchers.uniswap_v3.decoder import (
    _hex_str,
    decode_swap_log,
    fetch_tx_from_map,
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
# One primary pool per V1 tracked token, plus the original USDC/WETH reference
# pool. TOKEN/WETH throughout: the USDC fallback was never needed, since every
# V1 token has a WETH pair. Fee tier is 0.3% by default, overridden per pool
# only where a different tier measured deeper — see the note on each exception.
#
# Every address was verified on-chain via getPool(tokenA, tokenB, fee) and then
# token0(), token1(), fee(), and re-verified after transcription. The matching
# token-slot layout lives in pipeline/exploration/queries.py POOL_TOKEN_LAYOUT,
# which carries the fee-tier methodology caveat; both are hand-written so a
# reviewer sees either side change.
DEFAULT_ETHEREUM_POOLS: dict[str, str] = {
    "UNI/WETH 0.3%": "0x1d42064fc4beb5f8aaf85f4617ae8b3b5b8bd801",
    "AAVE/WETH 0.3%": "0x5ab53ee1d50eef2c1dd3d5402789cd27bb52c1bb",
    "LDO/WETH 0.3%": "0xa3f558aebaecaf0e11ca4b2199cc5ed341edfd74",
    "PENDLE/WETH 0.3%": "0x57af956d3e2cca3b86f3d8c6772c03ddca3eaacb",
    "CRV/WETH 0.3%": "0x919fa96e88d67499339577fa202345436bcdaf79",
    "ENA/WETH 0.3%": "0xc3db44adc1fcdfd5671f555236eae49f4a8eea18",
    # MKR trades here, but DefiLlama's /chart does not carry MKR, so its
    # swaps classify as 'unpriceable' until ADR 0010 ships. MKR's symbol()
    # also returns bytes32 rather than string; that affects token metadata
    # calls, not pool metadata, so nothing here depends on it.
    "MKR/WETH 0.3%": "0xe8c6c9227491c0a8156a0106a0204d881bb7e531",
    # 1% rather than the 0.3% default: measured ~2x the liquidity of the
    # 0.3% pool. MORPHO here is the TRANSFERABLE deployment (ADR 0008
    # amendment) — the legacy address has no pool and no price.
    "MORPHO/WETH 1%": "0x25b96761e765b9ac20db18fa57fa91e3b617ec6f",
    "LINK/WETH 0.3%": "0xa6cc3c2531fdaa6ae1a3ca84c2855806728693e8",
    "ARB/WETH 0.3%": "0x59354356ec5d56306791873f567d61ebf11dfbd5",
    "EIGEN/WETH 0.3%": "0xc2c390c6cd3c4e6c2b70727d35a45e8a072f18ca",
    "ETHFI/WETH 0.3%": "0x06f00544c0bc62e6db10f46d370dfccdc23d8189",
    "USDC/WETH 0.05%": "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
}

# Base coverage follows the token registry: only the six V1 tokens with a
# verified Base address have pools here (src/alphawallets/tokens.py).
DEFAULT_BASE_POOLS: dict[str, str] = {
    # 1% rather than 0.3%: the 0.3% pool exists but is ~25,000x shallower.
    "UNI/WETH 1%": "0xab365f161dd501473a1ff0d2ef0dce94e7398839",
    "AAVE/WETH 0.3%": "0x2e86514cfd61fb19c5cf2b879d536d273d6e693d",
    # 1% rather than 0.3%: measured ~60x the liquidity of the 0.3% pool.
    "CRV/WETH 1%": "0x330e535c40eb49cc186496f061052fcf814d68cb",
    # Shallowest pool in the set (~5 orders of magnitude below its peers)
    # and the only V3 option for PENDLE on Base; the USDC pair is thinner
    # still. Low confidence — expect few or no swaps in a short window.
    "PENDLE/WETH 0.3%": "0xd7042869277c75ca56f1f6cc7e18ff0d83410dee",
    "MORPHO/WETH 0.3%": "0x2f42df4af5312b492e9d7f7b2110d9c7bf2d9e4f",
    "LINK/WETH 0.3%": "0x224a5d3f2155f2f85af70b6d72aea61a15273ff4",
}

DEFAULT_POOLS_BY_CHAIN: dict[str, dict[str, str]] = {
    "ethereum": DEFAULT_ETHEREUM_POOLS,
    "base": DEFAULT_BASE_POOLS,
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

            # Enrich decoded rows with tx_from (real EOA, not router)
            tx_from_map = fetch_tx_from_map(w3, live_logs)

            decoded_records = [
                decode_swap_log(
                    log,
                    swap_event,
                    chain=chain,
                    tx_from=tx_from_map[_hex_str(log["transactionHash"])],
                )
                for log in live_logs
            ]
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
        "--resume",
        action="store_true",
        help=(
            "Continue from the highest block already stored for this chain and "
            "pool, up to the current head. Falls back to the default "
            "window when nothing is stored yet. Cannot be combined with "
            "--blocks / --from-block / --to-block."
        ),
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


def get_resume_point(conn: DuckDBPyConnection, chain: str, pool_address: str) -> int | None:
    """Return the highest block already stored for this (chain, pool), or None.

    Reads the decoded table, not the raw one: raw rows can include logs the
    pipeline later excluded as reorged, so the decoded table is the pipeline's
    source of truth (CLAUDE.md Section 6).

    Args:
        conn: Open DuckDB connection.
        chain: Chain name.
        pool_address: Uniswap V3 pool contract, any case.

    Returns:
        The highest stored block number, or None when nothing is stored for this
        (chain, pool_address) — including when the table does not exist yet.
    """
    row = conn.execute(
        "SELECT MAX(block_number) FROM uniswap_v3_swap WHERE chain = ? AND lower(pool_address) = ?",
        [chain, pool_address.lower()],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _validate_range_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Validate range args and return the effective block count for head-relative mode.

    Returns:
        Number of blocks to fetch from head, or 0 if explicit range mode.
    """
    if (args.from_block is None) != (args.to_block is None):
        parser.error("--from-block and --to-block must be provided together")
    if args.blocks is not None and args.from_block is not None:
        parser.error("--blocks cannot be combined with --from-block / --to-block")
    if args.resume and args.blocks is not None:
        parser.error("--resume cannot be combined with --blocks")
    if args.resume and args.from_block is not None:
        parser.error("--resume cannot be combined with --from-block / --to-block")
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

    mode = f"head-relative ({blocks_default:,} blocks)"
    if args.from_block is not None:
        mode = "explicit range"
        from_block = args.from_block
        to_block = args.to_block
    else:
        w3 = make_web3(args.chain)
        current = w3.eth.block_number

        resume_from: int | None = None
        if args.resume:
            with connect(args.db_path) as conn:
                create_tables(conn)
                resume_from = get_resume_point(conn, args.chain, args.pool)
            if resume_from is None:
                logger.info(
                    "Resume: nothing stored for chain=%s pool=%s; defaulting to %d blocks",
                    args.chain,
                    args.pool,
                    blocks_default,
                )
                mode = f"resume — no previous data, defaulting to {blocks_default:,} blocks"
            else:
                logger.info(
                    "Resume: highest stored block %d; fetching from %d",
                    resume_from,
                    resume_from + 1,
                )
                mode = f"resume from stored block {resume_from:,}"

        if resume_from is not None:
            # +1 so the stored block is not re-fetched. An off-by-one here wastes
            # at most one block of work: INSERT OR IGNORE makes overlap harmless,
            # so this arithmetic is not a correctness concern.
            from_block = resume_from + 1
            to_block = current
        else:
            from_block = current - blocks_default + 1
            to_block = current

    print(
        f"AlphaWallets — Uniswap V3 Swap Fetcher\n"
        f"  Chain:      {args.chain}\n"
        f"  Pool:       {args.pool}\n"
        f"  Mode:       {mode}\n"
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
