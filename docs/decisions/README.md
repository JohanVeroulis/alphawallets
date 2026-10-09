# Architecture Decision Records

This folder records significant decisions: what we chose, why, and what we gave up. The goal is that a future reader (or a future Claude Code session) doesn't have to re-argue a settled question.

## Conventions

- **File name:** `NNNN-kebab-case-title.md`, where `NNNN` is a zero-padded sequential number (`0001`, `0002`, …). Numbers are never reused.
- **Sections:** Title, Status, Date, Context, Decision, Alternatives Considered, Consequences.
- **Status:** `Proposed` → `Accepted`. Later it can become `Superseded by NNNN` or `Deprecated`.
- **ADRs don't change.** To change a decision, write a new ADR and mark the old one `Superseded by NNNN`. Fixing typos and adding links is fine.
- **Keep them short.** One page is enough. Link to queries or data that back the decision.

## When to write one

- Choosing between tools, data sources, or storage (e.g. DuckDB vs. SQLite)
- Methodology decisions: PnL calculation, category rules, wallet filters
- Scope changes to V1

## Index

| # | Title | Status |
|---|---|---|
| [0001](0001-airdrop-selection.md) | V1 Airdrop Selection | Accepted |
| [0002](0002-pandas-2x-pin.md) | Pin pandas to 2.x for V1 | Accepted |
| [0003](0003-alchemy-over-dune.md) | Alchemy as V1 data source (replaces Dune) | Accepted |
| [0004](0004-duckdb-cache.md) | DuckDB as V1 local cache and analytical store | Accepted |
| [0005](0005-web3-7x-pin.md) | Pin web3.py to 7.x and eth-abi to 5.x for V1 | Accepted |
| [0006](0006-alchemy-eth-getlogs-block-window.md) | Alchemy free-tier eth_getLogs is capped at 10 blocks per request | Accepted |
| [0007](0007-alchemy-cups-constraint.md) | Alchemy free-tier CUPS constrains request concentration, not total volume | Accepted |
| [0008](0008-defillama-historical-prices.md) | DefiLlama as V1 historical price source | Accepted |
| [0009](0009-duckdb-connection-and-schema-conventions.md) | DuckDB connection and schema conventions | Accepted |
| [0010](0010-defillama-historical-fallback.md) | DefiLlama historical price fallback for /chart gaps | Accepted |
| [0011](0011-chain-aware-backfill-parallelization.md) | Chain-aware backfill parallelization | Accepted |
| [0012](0012-pnl-methodology.md) | PnL calculation methodology | Accepted |
| 0013 | *Reserved* — provider grid granularity (proposed in PR #31, not yet written) | Proposed |
| [0014](0014-pool-destination-realization.md) | Transfer-OUT to a Uniswap V3 pool is a realization | Accepted |
| [0015](0015-per-swap-executed-price.md) | Per-swap executed price, from swaps as an event source | Accepted |
| [0016](0016-contract-mediated-attribution.md) | Contract-mediated trading is flagged, not ranked | Accepted |
