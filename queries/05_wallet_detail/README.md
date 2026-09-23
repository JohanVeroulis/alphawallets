# 05 — Wallet Detail

Queries behind the per-wallet page: what this wallet did, how it made its money, and why it sits in its category.

## What belongs here

- Recent activity feed per wallet: swaps, deposits, withdrawals, claims
- PnL breakdown by token and by source (trading / airdrop / yield)
- Current positions and protocol exposure
- The signals that produced the wallet's category

## Conventions

- `AW_XX_description.sql` naming and the standard header comment (CLAUDE.md Section 6)
- Always parameterized by wallet address — never scan all wallets from this folder
- Cap the activity feed (for example the 100 most recent events) so the page stays fast
- Show the reasoning, not just the label: the detail page has to justify the leaderboard

How this data reaches the frontend is decided in Week 4 (see [ROADMAP.md](../../ROADMAP.md)).
