# Pipeline

Derived analysis over the data written by `fetchers/`. Everything here reads from DuckDB and writes back derived tables — no Alchemy calls.

## Stages

- `exploration/` — Jupyter-style notebooks and ad-hoc scripts for understanding raw data before formalizing it. Output is throwaway; nothing here writes to production tables.
- `pnl/` — FIFO cost-basis PnL calculation, airdrop-aware. Reads decoded fetcher tables; writes per-wallet PnL tables.
- `categorization/` — Category detection (Traders, DeFi users, Airdrop Hunters, Yield Farmers). Reads PnL and activity tables; writes wallet categorizations.
- `ranking/` — Leaderboard ranking logic. Reads PnL + categorization; writes ranked wallet tables.

The frontend (Week 5+) reads from the ranking output. Wallet detail pages read from a join across all pipeline tables.
