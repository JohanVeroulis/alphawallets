# Ranking

Leaderboard ranking logic.

## Inputs

- PnL tables from `pipeline/pnl/`
- Category labels from `pipeline/categorization/`
- Activity filters (min trades, activity within 30d — see CLAUDE.md Section 9 for the "consistently profitable" definition)

## Outputs

Ranked wallet tables per (timeframe, category) combination, ready for the frontend to consume.

## Timeframes

V1: 30d, 90d.
