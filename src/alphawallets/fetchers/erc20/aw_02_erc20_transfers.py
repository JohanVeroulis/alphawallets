"""AW_02 — ERC-20 Transfer events fetcher.

Fetches ERC-20 transfers for a token contract via Alchemy's Transfers API
(`alchemy_getAssetTransfers`), maps them to validated models, and writes both
raw and normalized rows to DuckDB.

Chains: ethereum, base (V1 scope, per CLAUDE.md Section 2)
Route: Transfers API rather than eth_getLogs — no 10-block window cap
    (ADR 0006), entries arrive pre-decoded, and pagination is by page key
    rather than block range. eth_getLogs stays the route for
    protocol-specific event decoding (AW_01's Uniswap Swap).
Pagination: one HTTP call per page, up to 1000 entries each. Writes happen
    per page, so a crash mid-run loses at most one page of progress.
Output tables:
- raw_erc20_transfer — audit trail, primary key (chain, unique_id).
- erc20_transfer — normalized pipeline input, same primary key. Entries
  without a block timestamp are written to raw only; they cannot be decoded
  because every downstream consumer keys on time.

Idempotency: INSERT OR IGNORE on both tables, so overlapping re-runs are safe.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from duckdb import DuckDBPyConnection
from pydantic import BaseModel, ConfigDict, Field

from alphawallets.alchemy_client import make_web3
from alphawallets.config import Chain
from alphawallets.db import connect
from alphawallets.fetchers.erc20.client import fetch_asset_transfers
from alphawallets.fetchers.erc20.mapper import to_erc20_transfer, to_raw_asset_transfer
from alphawallets.fetchers.erc20.models import ERC20Transfer, RawAssetTransfer
from alphawallets.fetchers.erc20.writer import (
    create_tables,
    write_decoded_transfers,
    write_raw_transfers,
)

logger = logging.getLogger(__name__)

# ---------- Constants ----------

# The V1 default target: UNI on Ethereum. Both a tracked DeFi token and a
# tracked airdrop (CLAUDE.md Section 2), so it exercises both use cases.
DEFAULT_TOKENS: dict[str, str] = {
    "UNI": "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984",
}


class FetchResult(BaseModel):
    """Summary of a fetch_and_persist_erc20_transfers run.

    Mirrors AW_01's FetchResult shape so callers (CLI, scheduled jobs, tests)
    can treat both fetchers the same way.
    """

    model_config = ConfigDict(frozen=True)

    chain: str
    contract_address: str
    from_block: int = Field(ge=0)
    to_block: int = Field(ge=0)
    pages_fetched: int = Field(ge=0)
    transfers_seen: int = Field(ge=0, description="Entries returned by the API across all pages")
    raw_transfers_written: int = Field(ge=0)
    raw_transfers_skipped: int = Field(ge=0, description="Duplicates ignored on insert")
    decoded_transfers_written: int = Field(ge=0)
    decoded_transfers_skipped: int = Field(ge=0, description="Duplicates ignored on insert")
    mapping_skipped: int = Field(
        ge=0, description="Entries that could not be mapped at all (malformed; see WARNING logs)"
    )
    decoded_skipped_missing_timestamp: int = Field(
        ge=0,
        description=(
            "Raw rows written but not decodable because block_timestamp was absent. "
            "Re-mappable from the raw table if Alchemy backfills the metadata."
        ),
    )
    elapsed_seconds: float = Field(ge=0)


def fetch_and_persist_erc20_transfers(
    chain: Chain,
    contract_address: str,
    from_block: int,
    to_block: int,
    db_path: Path | str | None = None,
) -> FetchResult:
    """Fetch, map, and persist ERC-20 transfers for one token over a block range.

    Orchestrates the full pipeline:
    1. Open DuckDB (creating tables if missing)
    2. For each Transfers API page in [from_block, to_block]:
       a. alchemy_getAssetTransfers for the contract
       b. Map entries to RawAssetTransfer and write to raw_erc20_transfer
       c. Normalize to ERC20Transfer and write to erc20_transfer
    3. Return a FetchResult with counts.

    Writes happen per page so a crash loses at most one page. Idempotent: safe
    to re-run overlapping ranges (INSERT OR IGNORE on both primary keys).

    Entries that fail mapping are counted in `mapping_skipped`; entries that
    map but lack a block timestamp land in raw only and are counted in
    `decoded_skipped_missing_timestamp`.

    Args:
        chain: ethereum or base.
        contract_address: ERC-20 token contract address.
        from_block: Start block, inclusive.
        to_block: End block, inclusive.
        db_path: Optional DuckDB path override. Defaults to
            get_cache_db_path() from config.

    Returns:
        FetchResult with per-run counts and wall-clock time.

    Raises:
        ValueError: If from_block > to_block, or the RPC returns an error.
        ConnectionError: From make_web3 if Alchemy is unreachable.
    """
    if from_block > to_block:
        raise ValueError(f"from_block ({from_block}) > to_block ({to_block})")

    start_time = time.time()
    w3 = make_web3(chain)

    pages_fetched = 0
    transfers_seen = 0
    raw_written = 0
    raw_skipped = 0
    decoded_written = 0
    decoded_skipped = 0
    mapping_skipped = 0
    missing_timestamp = 0

    with connect(db_path) as conn:
        create_tables(conn)

        page_key: str | None = None
        while True:
            entries, page_key = fetch_asset_transfers(
                w3,
                contract_address,
                from_block,
                to_block,
                page_key=page_key,
            )
            pages_fetched += 1
            transfers_seen += len(entries)

            if entries:
                raws: list[RawAssetTransfer] = []
                for entry in entries:
                    raw = to_raw_asset_transfer(entry, chain=chain)
                    if raw is None:
                        mapping_skipped += 1
                        continue
                    raws.append(raw)

                n_raw_inserted = write_raw_transfers(conn, raws)
                raw_written += n_raw_inserted
                raw_skipped += len(raws) - n_raw_inserted

                decoded: list[ERC20Transfer] = []
                for raw in raws:
                    transfer = to_erc20_transfer(raw)
                    if transfer is None:
                        missing_timestamp += 1
                        continue
                    decoded.append(transfer)

                n_decoded_inserted = write_decoded_transfers(conn, decoded)
                decoded_written += n_decoded_inserted
                decoded_skipped += len(decoded) - n_decoded_inserted

            if page_key is None:
                break

    elapsed = time.time() - start_time

    result = FetchResult(
        chain=chain,
        contract_address=contract_address.lower(),
        from_block=from_block,
        to_block=to_block,
        pages_fetched=pages_fetched,
        transfers_seen=transfers_seen,
        raw_transfers_written=raw_written,
        raw_transfers_skipped=raw_skipped,
        decoded_transfers_written=decoded_written,
        decoded_transfers_skipped=decoded_skipped,
        mapping_skipped=mapping_skipped,
        decoded_skipped_missing_timestamp=missing_timestamp,
        elapsed_seconds=round(elapsed, 3),
    )

    logger.info(
        "fetch_and_persist_erc20_transfers: chain=%s contract=%s blocks=%d-%d pages=%d "
        "seen=%d raw_written=%d raw_skipped=%d decoded_written=%d decoded_skipped=%d "
        "mapping_skipped=%d missing_timestamp=%d elapsed=%.2fs",
        result.chain,
        result.contract_address,
        result.from_block,
        result.to_block,
        result.pages_fetched,
        result.transfers_seen,
        result.raw_transfers_written,
        result.raw_transfers_skipped,
        result.decoded_transfers_written,
        result.decoded_transfers_skipped,
        result.mapping_skipped,
        result.decoded_skipped_missing_timestamp,
        result.elapsed_seconds,
    )

    return result


# ---------- CLI ----------


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI arg parser. Extracted so tests can call it in isolation."""
    parser = argparse.ArgumentParser(
        description="Fetch ERC-20 transfers via the Alchemy Transfers API and persist to DuckDB.",
    )
    parser.add_argument(
        "--chain",
        required=True,
        choices=["ethereum", "base"],
        help="Target chain (V1 scope per CLAUDE.md Section 2).",
    )
    parser.add_argument(
        "--contract",
        default=DEFAULT_TOKENS["UNI"],
        help=f"ERC-20 token contract address. Default: UNI ({DEFAULT_TOKENS['UNI']}).",
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
            "contract, up to the current head. Falls back to the default "
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
        help="Log per-page fetch and write progress at INFO level.",
    )
    return parser


def get_resume_point(conn: DuckDBPyConnection, chain: str, contract_address: str) -> int | None:
    """Return the highest block already stored for this (chain, token), or None.

    Reads the decoded table, not the raw one: raw rows can include logs the
    pipeline later excluded as reorged, so the decoded table is the pipeline's
    source of truth (CLAUDE.md Section 6).

    Args:
        conn: Open DuckDB connection.
        chain: Chain name.
        contract_address: ERC-20 token contract, any case.

    Returns:
        The highest stored block number, or None when nothing is stored for this
        (chain, contract_address) — including when the table does not exist yet.
    """
    row = conn.execute(
        "SELECT MAX(block_number) FROM erc20_transfer WHERE chain = ? AND lower(token_address) = ?",
        [chain, contract_address.lower()],
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
    """CLI entry point for the ERC-20 transfers fetcher."""
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
                resume_from = get_resume_point(conn, args.chain, args.contract)
            if resume_from is None:
                logger.info(
                    "Resume: nothing stored for chain=%s contract=%s; defaulting to %d blocks",
                    args.chain,
                    args.contract,
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
        f"AlphaWallets — ERC-20 Transfers Fetcher\n"
        f"  Chain:      {args.chain}\n"
        f"  Contract:   {args.contract}\n"
        f"  Mode:       {mode}\n"
        f"  From block: {from_block:,}\n"
        f"  To block:   {to_block:,}\n"
    )

    result = fetch_and_persist_erc20_transfers(
        chain=args.chain,
        contract_address=args.contract,
        from_block=from_block,
        to_block=to_block,
        db_path=args.db_path,
    )

    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
