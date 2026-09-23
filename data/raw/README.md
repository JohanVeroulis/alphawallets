# data/raw

Raw Dune query results, exactly as fetched, before any cleaning. Keeping the untouched response makes it possible to re-run transformations without spending Dune credits again.

**Contents are git-ignored.** `.gitkeep` exists only so the folder survives a clone.

## What belongs here

- Query result files named after the query that produced them, e.g. `AW_01_wallet_universe_2026-09-23.parquet`
- One file per query run; runs are never overwritten in place

## Conventions

- Include the fetch date in the file name — it is the only record of when the data is from
- Parquet is preferred over CSV: smaller and it keeps types
- Treat these files as read-only; cleaning writes to [../processed/](../processed/)
