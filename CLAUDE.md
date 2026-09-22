# CLAUDE.md — AlphaWallets

Project memory for Claude Code. Read this file in full at the start of every session, before doing anything else.

## 1. Project Overview

**AlphaWallets** is an on-chain analytics tool that identifies consistently profitable wallets on Ethereum and Base. It sorts them into four categories (Traders, DeFi users, Airdrop Hunters, Yield Farmers) and shows what they are doing right now, so retail traders can learn from real profitable actors rather than from influencers.

- **Owner:** Ioannis Veroulis ("Gioxan"), GitHub `JohanVeroulis`, data engineer in Thessaloniki, Greece
- **Repo:** `github.com/JohanVeroulis/alphawallets` (private during development)
- **Language:** English for everything: code, docs, commits, comments, PR descriptions

## 2. V1 Scope (LOCKED)

Anything not listed here is out of scope. If a task drifts outside it, stop and say so.

| Area | In scope |
|---|---|
| Chains | Ethereum mainnet, Base |
| Major pairs | ETH/USDC, ETH/USDT, WBTC/USDC, WBTC/ETH |
| DeFi tokens | UNI, AAVE, LDO, PENDLE, CRV, ENA, MKR, MORPHO, LINK, ARB |
| Stablecoin activity | Aave, Compound, Morpho, Pendle |
| Airdrops (tracked separately) | UNI, ARB, ENA, EIGEN, MORPHO, ETHFI |
| Categories | Traders, DeFi users, Airdrop Hunters, Yield Farmers |
| PnL | Airdrop-aware: airdrop income is separated from trading/yield PnL |
| Features | Leaderboard, wallet detail page, filters (timeframe, category, trade size, min activity) |
| Timeframes | 30d, 90d |

**Notes on scope**
- ARB is Arbitrum-native. We don't track the Arbitrum claim itself, only ARB activity on Ethereum after it was bridged.
- MORPHO and UNI show up both as DeFi tokens and as airdrops. PnL logic has to separate airdrop-sourced balances from purchased balances.

### On hold (V1.5+). Do not build.
- Solana support (planned for V1.5)
- Real-time alerts
- Cross-chain wallet linking
- Upcoming-airdrop feed
- Native BTC and XRP (data does not fit our use case)

## 3. Tech Stack

| Layer | Choice |
|---|---|
| Data source | Dune Analytics (DuneSQL / Trino dialect) as the primary V1 source |
| Pipeline / backend | Python 3.11+, managed with `uv` |
| Local cache | DuckDB (decision recorded in Section 9) |
| Frontend (Week 4+) | Next.js 14 + TailwindCSS |
| Deployment | Vercel (frontend); scheduled jobs on GitHub Actions |
| Tooling | Git + GitHub, VS Code, Claude Code |

**Architecture constraint:** the Dune API key must never reach the browser. The frontend reads data through server-side code (Next.js route handlers / server components) or from pre-computed results written by the pipeline, never by calling Dune directly from the client.

## 4. Timeline (5–6 weeks to V1)

| Week | Focus | Deliverables |
|---|---|---|
| 1 | Data foundation | Dune workspace, wallet-universe queries, PnL exploration |
| 2 | Leaderboard logic | PnL calculation, ranking |
| 3 | Categorization | Detection rules for the 4 categories |
| 4 | Frontend V1 | Next.js scaffold, leaderboard + wallet detail |
| 5 | Polish | Testing, performance |
| 6 | Launch prep | README, screenshots, Twitter thread, Product Hunt |

When a task belongs to a later week, point that out before starting it.

## 5. Commands

Fill these in as tooling is added. Do not invent commands that do not exist yet.

```bash
uv sync                 # install dependencies
uv run pytest           # run tests
uv run ruff check .     # lint
uv run ruff format .    # format
```

## 6. Conventions

### Git
- **Main branch:** `main`. Never commit directly to it for non-trivial work.
- **Branches:** `feat/`, `fix/`, `query/`, `docs/` + kebab-case slug (e.g. `query/aw-03-wallet-universe`)
- **Commits:** Conventional Commits with the types `feat:`, `fix:`, `docs:`, `chore:`, `query:`, `refactor:`, `test:`, `style:`
  - `query:` is for adding or changing Dune SQL, e.g. `query: add AW_03 wallet universe for Base`
- Small, focused commits: one logical change each.
- Never commit `.env`, secrets, tokens, or the owner's personal wallet addresses.

### Python
- PEP 8, **type hints everywhere**, docstrings on all public functions/classes
- Lint with `ruff`, format with `ruff format`
- Modules: `snake_case.py`
- Comments explain **why**, not what

### SQL (Dune)
- UPPERCASE keywords, `snake_case` identifiers, one clause per line
- Use CTEs rather than nested subqueries; give each CTE a descriptive name
- Every query file starts with a header comment: purpose, inputs/parameters, output grain, Dune query ID
- Filter on partition/time columns (`block_time`, `block_date`) early to keep query cost down

```sql
-- AW_01_wallet_universe
-- Purpose: candidate wallets with >= N swaps on in-scope pairs, last 90d
-- Grain: one row per (blockchain, wallet)
-- Dune ID: <id>
WITH swaps AS (
    SELECT
        blockchain,
        tx_from AS wallet,
        amount_usd
    FROM dex.trades
    WHERE block_time >= NOW() - INTERVAL '90' DAY
        AND blockchain IN ('ethereum', 'base')
)
SELECT
    blockchain,
    wallet,
    COUNT(*) AS swap_count
FROM swaps
GROUP BY 1, 2
```

### File naming
- Dune queries: `AW_XX_description.sql` (`XX` = zero-padded sequential number, never reused)
- Python modules: `snake_case.py`
- Docs: `kebab-case.md`

## 7. Operating Principles for Claude Code

1. **Read this file first** in every session.
2. **Never guess** credentials, API keys, or env vars. If one is missing, stop and ask.
3. **Never commit** `.env`, secrets, tokens, or the owner's personal wallet addresses. Public wallet addresses that are analysis *output* are fine in query results but not hard-coded as personal data.
4. **Confirm before anything destructive**: `rm`, force push, `git reset --hard`, dropping or overwriting DB tables, deleting Dune queries.
5. **New code** comes with type hints, docstrings, and at least one usage example (docstring example or test).
6. **Edited code:** run the relevant tests before calling the change done. Report failures as they are; do not hide them.
7. **Small commits.** Commit or push only when asked.
8. **If scope or intent is unclear, ask before writing code.**
9. **Docs are a deliverable.** Every feature ships with docs (`docs/` or its README section).
10. **English only** in everything produced.

## 8. Working Together

- Gioxan reviews **step by step**: deliver in small, reviewable increments and pause at natural checkpoints. No big batches.
- Decisions are sometimes discussed elsewhere in Greek and arrive as English drafts. Treat them as input to refine, not as final spec.
- **Learning matters as much as shipping.** Explain the reasoning behind non-obvious choices (SQL patterns, PnL methodology, data modelling) briefly.

**Claude's role: senior engineer, not just an executor.**
- Lay out trade-offs and give a recommendation.
- Push back on under-specified requirements (e.g. "profitable": over which window, realized or unrealized, minimum sample size?).
- Suggest better patterns when you see them, and explain why.
- Record significant decisions as short ADRs in `docs/decisions/NNNN-title.md`.

## 9. Open Questions & Provisional Decisions

Items marked *provisional* are working assumptions, to be validated in Week 1/2 with real data. Record any change as an ADR.

### Decided
- [x] **Local cache:** DuckDB (better suited to analytics workloads)
- [x] **Scheduled jobs:** GitHub Actions to start with (free, integrated, sufficient for V1)

### Provisional (validate in Week 1/2)
- [~] **PnL methodology:** realized PnL with FIFO cost basis for V1; unrealized PnL in V1.5.
- [~] **Price source:** Dune price tables. Start with `prices.hour` (hourly resolution, cheaper than minute-level `prices.usd`); consider `prices.day` if hourly is still too expensive at scale. Fall back to the CoinGecko API only for tokens missing from Dune. Final choice validated in Week 1.
- [~] **"Consistently profitable":** at least 10 trades in the window, positive net PnL, activity within the last 30 days
- [~] **Wallet-universe filters:** exclude known CEX hot wallets and bridge addresses; exclude contracts EXCEPT known smart-wallet types (Safe multisigs, ERC-4337 accounts, Argent, Ambire) identified via factory-deployment patterns; identify and flag (not exclude) MEV bots using a combination of Dune MEV/Flashbots tables (mev.* schema, flashbots.transactions), sandwich-attack pattern matching, and Flashbots bundle inclusion. Concrete thresholds to be validated in Week 1 with real data.
- [~] **Category rules:** a wallet can belong to multiple categories, with a primary + secondary designation

### Open
- [ ] Category thresholds: numeric values per category, to be set from real data
- [ ] Airdrops distributed outside ETH/Base (see Scope notes)
