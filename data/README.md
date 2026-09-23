# Data

Local working data for the pipeline: raw Dune results, the processed tables built from them, and the static reference data the code needs.

**Contents of `raw/` and `processed/` are git-ignored.** They are a cache — reproducible by re-running the pipeline — and they can contain large files. Only the folder structure is tracked, via `.gitkeep` files, so a fresh clone has somewhere to write.

## What belongs here

| Path | Tracked? | Contents |
|---|---|---|
| `raw/` | structure only | Raw Dune query results, as fetched |
| `processed/` | structure only | Cleaned and transformed tables, the DuckDB cache |
| `known_airdrops.json` | yes | Static reference: the six V1 airdrops |

## Conventions

- Never commit data files. If something must be checked in, it is small, static reference data and it lives at this level, not in `raw/` or `processed/`
- Never commit `.env` or anything containing an API key (CLAUDE.md Section 7)
- Anything in `raw/` and `processed/` must be reproducible from a pipeline run; nothing here is a source of truth
- `known_airdrops.json` follows [ADR 0001](../docs/decisions/0001-airdrop-selection.md) — changing the airdrop list means writing a new ADR
