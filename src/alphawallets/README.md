# alphawallets (Python package)

The data pipeline: it pulls results from Dune, caches them locally in DuckDB, computes PnL, assigns categories, and writes the leaderboard the frontend reads.

Empty for now apart from `__init__.py`. Modules arrive from Week 1 onward, as the queries they depend on stabilize.

## What belongs here

Planned modules:

| Module | Purpose | Arrives |
|---|---|---|
| `config.py` | Settings and secrets loaded from the environment | Week 1 |
| `dune_client.py` | Dune API access with DuckDB caching of results | Week 1 |
| `airdrops.py` | The V1 airdrop registry and claim attribution | Week 2 |
| `pnl.py` | Realized PnL, FIFO cost basis, airdrop-aware split | Week 2 |
| `categorization.py` | Traders / DeFi / Airdrop Hunters / Yield Farmers | Week 3 |

## Conventions

- `snake_case.py` module names, PEP 8, type hints everywhere (CLAUDE.md Section 6)
- Every public function has a docstring with at least one usage example
- Secrets come from the environment, never from source — `config.py` is the only module that reads them
- Business logic stays out of `dune_client.py`: it fetches and caches, nothing more
- Each module gets a matching test file under [../../tests/](../../tests/)
