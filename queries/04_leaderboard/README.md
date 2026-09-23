# 04 — Leaderboard

The queries that produce the ranked leaderboard the frontend reads. This is the project's headline output.

## What belongs here

- Final ranking over both timeframes (30d, 90d)
- The "consistently profitable" filter applied to the candidate set
- Joined output: PnL, category, activity summary, per wallet
- Filter-support columns: trade size buckets, activity counts

## Conventions

- `AW_XX_description.sql` naming and the standard header comment (CLAUDE.md Section 6)
- The output schema is a contract with the frontend — changing a column name or type is a breaking change, so note it in the header and in [STATUS.md](../../STATUS.md)
- One row per `(blockchain, wallet, timeframe)`; keep the timeframe as a column rather than splitting into separate queries
- Ties broken deterministically, so the leaderboard does not reshuffle between runs

The "consistently profitable" definition is provisional until Week 2 (CLAUDE.md Section 9).
