# PnL

FIFO cost-basis PnL calculation, airdrop-aware.

## Inputs

- Decoded swap tables from `fetchers/uniswap_v3/` (and other DEX fetchers as they land)
- ERC-20 Transfer tables from `fetchers/erc20/`
- Historical USD prices (source under investigation in Week 1 — DefiLlama primary candidate per ADR 0003)

## Outputs

Per-wallet PnL tables with airdrop-sourced balances tagged and separated from purchased balances.

## Methodology

- Realized PnL only for V1 (unrealized in V1.5)
- FIFO cost basis
- Airdrop cost basis = 0; distribution transactions are traced from `fetchers/erc20/` output

## Running it

```bash
# Compute PnL for all wallets across both chains, using the cache's own as-of
uv run python -m alphawallets.pipeline.pnl

# Dry-run (shows what would be written without touching the DB)
uv run python -m alphawallets.pipeline.pnl --dry-run

# Single wallet, Ethereum only
uv run python -m alphawallets.pipeline.pnl --wallet 0xABC... --chains ethereum
```

Reads the local DuckDB cache and writes `wallet_pnl`. No provider, no credential, no rate limit — a pipeline stage works from what the fetchers filled (CLAUDE.md §6), so it is safe to re-run.

| Flag | Default |
|---|---|
| `--as-of` | Just past the newest indexed `block_timestamp` for the selected chains |
| `--chains` | Every chain in the cache |
| `--wallet` | Every wallet. Repeatable |
| `--cache-db` | The configured cache path |
| `--dry-run` | Off — compute and report without writing |

### `--as-of` semantics

`window_end` is **exclusive**. An explicitly passed `--as-of` is therefore "up to but not including" that instant.

The default is the newest `block_timestamp` **plus one microsecond**, not the timestamp itself: with a strict exclusive bound, defaulting to the bare maximum would exclude every event in the newest block, and in a cache whose events share one timestamp it would exclude all of them and report no activity at all. "As of the data edge" has to mean *including* the edge.

Wall-clock `now()` is deliberately not the default. It would open a trailing gap between the last indexed block and the window end, and make the same cache produce different answers depending on when the command was run.

### Re-runs update rather than duplicate

The writer uses `INSERT OR REPLACE`, unlike every other writer in this project. ADR 0012's final consequence is that a running cost basis means a window's PnL legitimately changes when earlier events are re-fetched — a deeper backfill, or a reorg repair. Ignoring the second write would leave a stale figure in place and make `computed_at` a lie about which data produced the row.

### The summary reports its own trustworthiness

Beyond row counts, each run prints how much of its output carries each ADR 0012 caveat flag:

```
  Caveat flags — how much of this output to trust:
    has_pre_window_activity       12 /    40 rows ( 30.0%)
    has_unpriceable_events         4 /    40 rows ( 10.0%)
    has_smart_wallet_signal        0 /    40 rows (  0.0%)
```

A flag nobody looks at is a flag that does not work. A leaderboard built on rows that are 80% pre-window is a different object from one built on clean rows, and this is the cheapest moment to notice.

