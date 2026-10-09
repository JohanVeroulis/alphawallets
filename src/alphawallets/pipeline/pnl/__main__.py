"""CLI for the PnL calculator: `python -m alphawallets.pipeline.pnl`.

Reads the local DuckDB cache, computes per-(wallet, token, window) realized PnL,
and writes it to `wallet_pnl`. Reads the cache only — no provider, no credential,
no rate limit (CLAUDE.md §6).

The summary it prints is deliberately more than "rows written". ADR 0012's three
caveat flags exist so a consumer can decide what to trust, and a flag nobody
looks at is a flag that does not work — so the run reports how much of its own
output carries each caveat. A leaderboard built on rows that are 80% pre-window
is a different object from one built on clean rows, and the only moment that is
cheap to notice is here.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from alphawallets.config import Chain
from alphawallets.db import connect
from alphawallets.pipeline.pnl.calculator import WINDOW_DAYS, compute_wallet_pnl
from alphawallets.pipeline.pnl.models import WalletPnL
from alphawallets.pipeline.pnl.writer import create_tables, write_wallet_pnl

logger = logging.getLogger(__name__)

SAMPLE_ROWS = 10
TOP_WALLETS = 5


def _parse_as_of(raw: str) -> datetime:
    """Parse an ISO 8601 timestamp into an aware UTC datetime.

    A naive input is rejected rather than assumed to be UTC. Every window bound
    in this pipeline is UTC by construction (ADR 0009), and silently adopting a
    naive value would let a local-time argument shift both window edges without
    anyone noticing.
    """
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"--as-of must be an ISO 8601 timestamp, got {raw!r} ({e})"
        ) from e
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"--as-of must carry a timezone, got {raw!r}. Use a trailing Z for UTC."
        )
    return parsed.astimezone(UTC)


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI arg parser. Extracted so tests can call it in isolation."""
    parser = argparse.ArgumentParser(
        prog="python -m alphawallets.pipeline.pnl",
        description=(
            "Compute realized PnL per (wallet, token, window) from the local "
            "DuckDB cache and write it to wallet_pnl. Reads the cache only."
        ),
    )
    parser.add_argument(
        "--as-of",
        type=_parse_as_of,
        default=None,
        help=(
            "Window end, ISO 8601 with a timezone, e.g. 2026-10-06T23:59:59Z. "
            "Defaults to the newest indexed block timestamp for the selected "
            "chains — the edge of what we know, rather than wall-clock now."
        ),
    )
    parser.add_argument(
        "--chains",
        nargs="+",
        choices=["ethereum", "base"],
        default=None,
        help="Chains to compute. Default: every chain in the cache.",
    )
    parser.add_argument(
        "--wallet",
        action="append",
        dest="wallets",
        default=None,
        metavar="ADDRESS",
        help="Wallet to compute, repeatable. Default: every wallet in the cache.",
    )
    parser.add_argument(
        "--cache-db",
        type=Path,
        default=None,
        help="DuckDB cache path override. Defaults to the configured cache.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and report without writing. Prints counts and a sample.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Log per-stage progress at INFO level.",
    )
    return parser


def _print_summary(rows: list[WalletPnL], *, written: int, dry_run: bool) -> None:
    """Report what the run produced, including how much of it carries caveats."""
    print("\nAlphaWallets — PnL Calculator")
    if not rows:
        print("  No PnL rows produced.")
        print(
            "  Either the cache holds no transfers, or no wallet had activity "
            "inside a window.\n  Backfill first: AW_01 swaps, AW_02 transfers, "
            "AW_03 prices."
        )
        return

    partitions = {(r.chain, r.wallet, r.token_address) for r in rows}
    verdict = "computed, not written" if dry_run else f"{written} written"
    print(f"  Rows:       {len(rows)} ({verdict})")
    print(f"  Partitions: {len(partitions)} (chain, wallet, token)")
    print(f"  Windows:    {', '.join(f'{d}d' for d in WINDOW_DAYS)}")
    print(f"  As of:      {rows[0].window_end.isoformat()}")

    print("\n  Caveat flags — how much of this output to trust:")
    for label, predicate in (
        ("has_pre_window_activity", lambda r: r.has_pre_window_activity),
        ("has_unpriceable_events", lambda r: r.has_unpriceable_events),
        ("has_smart_wallet_signal", lambda r: r.has_smart_wallet_signal),
    ):
        count = sum(1 for r in rows if predicate(r))
        share = 100.0 * count / len(rows)
        print(f"    {label:<26} {count:>5} / {len(rows)} rows ({share:5.1f}%)")
    print(
        "    (pre-window rows are excluded from leaderboard rankings per ADR 0012 "
        "decision 5;\n     contract-mediated rows likewise per ADR 0016)"
    )

    shortest = min(WINDOW_DAYS)
    in_window = [r for r in rows if (r.window_end - r.window_start).days == shortest]
    # Both exclusions applied here, not just reported above. This top-N is the
    # only leaderboard the project has today, so printing a flagged row inside
    # it would contradict the two ADRs the lines above cite — and ADR 0016 was
    # written because a flagged row sat at the top of exactly this list.
    eligible = [
        r for r in in_window if not r.has_pre_window_activity and not r.has_smart_wallet_signal
    ]
    ranked = sorted(eligible, key=lambda r: r.realized_pnl_trading_usd, reverse=True)[:TOP_WALLETS]
    excluded = len(in_window) - len(eligible)
    if ranked:
        print(f"\n  Top {len(ranked)} eligible by trading PnL ({shortest}d window):")
        for row in ranked:
            flags = "U" if row.has_unpriceable_events else "-"
            print(
                f"    {row.wallet[:10]}… {row.token_address[:10]}… "
                f"trading ${row.realized_pnl_trading_usd:>14,.2f}  "
                f"airdrop ${row.realized_pnl_airdrop_usd:>14,.2f}  [{flags}]"
            )
        print("    flags: U = unpriceable events")
    else:
        print(f"\n  No eligible rows to rank in the {shortest}d window.")
    print(
        f"    {excluded} of {len(in_window)} {shortest}d rows excluded from ranking "
        "(pre-window or contract-mediated)"
    )

    if dry_run:
        print(f"\n  Dry run — nothing written. First {SAMPLE_ROWS} rows:")
        for row in rows[:SAMPLE_ROWS]:
            days = (row.window_end - row.window_start).days
            print(
                f"    {row.chain:<9} {row.wallet[:10]}… {row.token_address[:10]}… "
                f"{days:>3}d  pnl ${row.realized_pnl_usd:>12,.2f}  "
                f"bought ${row.bought_usd:>12,.2f}  sold ${row.sold_usd:>12,.2f}  "
                f"n={row.realization_count}"
            )


def main() -> int:
    """CLI entry point.

    Returns:
        0 on success, 1 on a failure that is the operator's to fix — a missing
        cache, or a schema drift. The message says which.
    """
    parser = _build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.cache_db is not None and not args.cache_db.exists():
        # Checked before connecting: DuckDB would otherwise create an empty file
        # at the path and report "no transfers", which reads as a data problem
        # rather than as a wrong path.
        print(
            f"Cache not found: {args.cache_db}\n"
            "Pass an existing --cache-db, or omit it to use the configured cache.",
            file=sys.stderr,
        )
        return 1

    chains: list[Chain] | None = args.chains
    try:
        with connect(args.cache_db) as conn:
            create_tables(conn)
            rows = list(
                compute_wallet_pnl(
                    conn,
                    as_of=args.as_of,
                    wallet_filter=args.wallets,
                    chains=chains,
                )
            )
            # Materialised rather than streamed into the writer so the summary
            # can report flag distribution over the whole run. At V1 scale this
            # is two rows per (wallet, token) pair; if that stops being true,
            # stream to the writer and accumulate counters instead.
            written = 0 if args.dry_run else write_wallet_pnl(conn, rows)
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    _print_summary(rows, written=written, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
