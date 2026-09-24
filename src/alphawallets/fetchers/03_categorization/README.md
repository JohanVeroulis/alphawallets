# 03 — Categorization

Queries that assign wallets to the four V1 categories: Traders, DeFi users, Airdrop Hunters and Yield Farmers. A wallet can hold more than one, with a primary and a secondary designation.

## What belongs here

- Behavioural signals per wallet: swap frequency, protocol interactions, lending/LP positions, airdrop claims
- Category assignment rules and their thresholds
- Airdrop claim detection for the six V1 airdrops

## Conventions

- `AW_XX_description.sql` naming and the standard header comment (CLAUDE.md Section 6)
- Keep signal extraction and threshold application in separate queries, so thresholds can change without rewriting the signals
- Every threshold carries a comment explaining the distribution it came from — no magic numbers
- Output the signals alongside the category, so a wallet's classification can be explained on its detail page

Thresholds are set from real data in Week 3 and recorded in [../../docs/decisions/](../../docs/decisions/). ARB's bridged-only caveat is in [ADR 0001](../../docs/decisions/0001-airdrop-selection.md).
