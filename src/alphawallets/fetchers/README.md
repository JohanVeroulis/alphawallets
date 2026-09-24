# Dune Queries

Every Dune SQL query used by AlphaWallets lives here, one file per query, mirroring what is saved in the Dune workspace. The folder is the source of truth for review and history; Dune is where the queries actually run.

## What belongs here

- Dune SQL (DuneSQL / Trino dialect) for the wallet universe, PnL, categorization, leaderboard and wallet detail pages
- Ad-hoc exploration that taught us something worth keeping (in `00_exploration/`)

## Organization

| Folder | Purpose |
|---|---|
| `00_exploration/` | Ad-hoc queries for learning the data. Not part of the pipeline. |
| `01_wallet_universe/` | Candidate wallet identification and filtering |
| `02_pnl_calculation/` | Realized, airdrop-aware PnL |
| `03_categorization/` | Traders / DeFi users / Airdrop Hunters / Yield Farmers |
| `04_leaderboard/` | Final ranking and leaderboard output |
| `05_wallet_detail/` | Per-wallet detail and activity |

## Conventions

- File name: `AW_XX_description.sql` — `XX` is a zero-padded sequential number, never reused (CLAUDE.md Section 6)
- Numbering is global across folders, so `AW_07` appears exactly once in this tree
- UPPERCASE keywords, `snake_case` identifiers, one clause per line, CTEs over nested subqueries
- Filter on `block_time` / `block_date` early — query cost is a real constraint
- Every file starts with the header comment below

```sql
-- AW_XX_description
-- Purpose: <what question this answers>
-- Grain:   <one row per ...>
-- Inputs:  <parameters, if any>
-- Dune ID: <id once saved in the workspace>
```

Decisions that shape these queries are recorded in [../docs/decisions/](../docs/decisions/).
