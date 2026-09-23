# data/processed

Cleaned and transformed data built from [../raw/](../raw/): the DuckDB cache and the tables the pipeline and the frontend read.

**Contents are git-ignored.** `.gitkeep` exists only so the folder survives a clone.

## What belongs here

- The DuckDB database file holding the local analytics cache
- Derived tables: wallet universe, PnL, categories, leaderboard

## Conventions

- Everything here is reproducible from `raw/` plus the pipeline code — delete it and re-run rather than patching it by hand
- Schema changes to the leaderboard table are breaking changes for the frontend; note them in [../../STATUS.md](../../STATUS.md)
- No wallet-level data leaves this folder except through the published leaderboard
