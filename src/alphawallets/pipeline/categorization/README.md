# Categorization

Category detection for wallets: Traders, DeFi users, Airdrop Hunters, Yield Farmers.

## Inputs

- Per-wallet activity summaries derived from all fetchers
- PnL tables from `pipeline/pnl/`

## Outputs

Per-wallet primary + secondary category labels. A wallet can belong to more than one category (CLAUDE.md Section 9).

## Rules

Concrete numeric thresholds are TBD Week 4, once real data is available to calibrate against.
