# alphawallets (Python package)

The Python side of AlphaWallets. Fetches on-chain data from Alchemy, indexes it in DuckDB, and computes per-wallet PnL, categories, and rankings.

The V1 scope, conventions, and open questions are in [`CLAUDE.md`](../../CLAUDE.md). Week-by-week plan in [`ROADMAP.md`](../../ROADMAP.md).

## Subpackages

- [`fetchers/`](fetchers/README.md) — Raw data collection from Alchemy, organized by protocol or generic data type (`uniswap_v3/`, `erc20/`). Every fetcher writes to DuckDB and returns a summary of what it wrote.
- [`pipeline/`](pipeline/README.md) — Derived analysis over the data written by fetchers. Reads from DuckDB, never calls Alchemy directly. Stages: `exploration/`, `pnl/`, `categorization/`, `ranking/`.

## Top-level modules

To be added with the first fetcher (Week 1). Expected:

- Configuration and environment loading (Alchemy API key, DuckDB path, chain list)
- A shared Alchemy client wrapper with retry, rate-limit awareness, and CU accounting
- A known-airdrops registry (`data/known_airdrops.json`)

Naming and structure are finalized as part of the first fetcher PR.

## Conventions

See [`CLAUDE.md`](../../CLAUDE.md) Section 6 for the full set. Highlights:

- Type hints on everything, Pydantic models for data crossing module boundaries
- Fetcher modules follow the `AW_XX_description.py` naming pattern
- Fetchers never write outside their DuckDB tables; pipeline stages never call Alchemy

## Tests

`pytest` runs from the repo root:

```bash
uv run pytest
```

Tests live in `../../tests/`. Coverage targets grow as modules land — no strict floor while the package is only skeletons.
