# 02 — PnL Calculation

Queries that turn wallet activity into a profit number. V1 computes realized PnL with a FIFO cost basis; unrealized PnL is Post-V1.

## What belongs here

- Buy/sell event extraction per wallet and token
- FIFO cost-basis matching and realized PnL
- Airdrop-aware split: airdrop income separated from trading PnL
- Price joins against Dune price tables

## Conventions

- `AW_XX_description.sql` naming and the standard header comment (CLAUDE.md Section 6)
- Every query states its price source and time resolution in the header
- Tokens received as airdrops must never be treated as zero-cost purchases — join against `data/known_airdrops.json` / the airdrop queries first
- Prefer explicit `NUMERIC` casts over implicit ones; rounding differences compound across thousands of trades

Relevant decisions: [ADR 0001 — V1 Airdrop Selection](../../docs/decisions/0001-airdrop-selection.md). Price source and the treatment of transfers in/out are still open (CLAUDE.md Section 9).
