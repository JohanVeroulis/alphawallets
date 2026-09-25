# AlphaWallets

An on-chain analytics tool that identifies consistently profitable wallets on Ethereum and Base, sorts them into four categories (Traders, DeFi users, Airdrop Hunters, Yield Farmers), and shows what they are doing right now — so retail traders can learn from real profitable actors rather than from influencers.

> **Status:** V1 in progress. See [STATUS.md](STATUS.md) for the current state and [ROADMAP.md](ROADMAP.md) for the ~8-week plan.

## What this is

A read-only analytics engine over on-chain data. It ranks wallets by realized PnL, tags them by activity pattern, and separates trading returns from airdrop windfalls. The V1 scope is deliberately narrow — locked in [CLAUDE.md](CLAUDE.md) Section 2 — so it can actually ship.

## V1 scope

Full details in [CLAUDE.md](CLAUDE.md) Section 2 (locked scope).

- **Chains:** Ethereum mainnet, Base
- **Categories:** Traders, DeFi users, Airdrop Hunters, Yield Farmers
- **Tracked pairs:** ETH/USDC, ETH/USDT, WBTC/USDC, WBTC/ETH
- **Tracked DeFi tokens:** UNI, AAVE, LDO, PENDLE, CRV, ENA, MKR, MORPHO, LINK, ARB
- **Tracked airdrops:** UNI, ARB, ENA, EIGEN, MORPHO, ETHFI (airdrop-aware PnL — see [ADR 0001](docs/decisions/0001-airdrop-selection.md))
- **Stablecoin activity tracked in:** Aave, Compound, Morpho, Pendle
- **Timeframes:** 30d, 90d
- **Features:** Leaderboard, wallet detail page, filters (timeframe, category, trade size, minimum activity)

Explicitly out of scope for V1: Solana, real-time alerts, cross-chain wallet linking, NFTs, unrealized PnL.

## Tech stack

| Layer | Choice |
|---|---|
| Data source | Alchemy free tier (JSON-RPC + Transfers API + Token API) — see [ADR 0003](docs/decisions/0003-alchemy-over-dune.md) |
| Historical prices | DefiLlama free API (primary), CoinGecko fallback |
| Local cache | DuckDB — see [ADR 0004](docs/decisions/0004-duckdb-cache.md) |
| Pipeline / backend | Python 3.11+, managed with `uv` |
| Frontend (Week 5+) | Next.js 14 + TailwindCSS |
| Deployment | Vercel (frontend); scheduled jobs on GitHub Actions |

## Project structure

The tree below is what exists today. Only `web/` is planned but not yet created.

- `CLAUDE.md` — Persistent context for Claude Code sessions
- `README.md` — This file
- `ROADMAP.md` — Week-by-week plan to V1
- `STATUS.md` — Current state (updated per session)
- `pyproject.toml` — Python packaging + tooling config
- `uv.lock` — Reproducible dependency lock
- `.env.example` — Environment variable template
- `docs/decisions/` — ADRs (architectural decision records)
- `src/alphawallets/` — Python package
  - `fetchers/` — Raw data collection from Alchemy
    - `uniswap_v3/` — Uniswap V3 swap events
    - `erc20/` — ERC-20 transfers (Transfers API + Transfer events)
  - `pipeline/` — Derived analysis over DuckDB
    - `exploration/` — Ad-hoc notebooks and scripts
    - `pnl/` — FIFO cost-basis PnL, airdrop-aware
    - `categorization/` — Wallet category detection
    - `ranking/` — Leaderboard ranking logic
- `tests/` — pytest suite
- `notebooks/` — Jupyter exploration
- `data/` — DuckDB cache and raw exports (git-ignored)
- `.github/ISSUE_TEMPLATE/` — Task, bug, idea templates
- `web/` — Next.js frontend (**planned Week 5**)

## Getting started

Prerequisites:
- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) installed
- An Alchemy account with an API key (free tier is enough) — [dashboard.alchemy.com](https://dashboard.alchemy.com)

Setup:

```bash
# Clone
git clone https://github.com/JohanVeroulis/alphawallets.git
cd alphawallets

# Install dependencies
uv sync

# Configure environment
cp .env.example .env
# then edit .env and set ALCHEMY_API_KEY
```

Verify:

```bash
uv run ruff check .
uv run pytest --collect-only
```

## Roadmap

Full plan: [ROADMAP.md](ROADMAP.md). Summary:

- **Weeks 1–2:** Alchemy fetchers, DuckDB indexing layer, historical prices
- **Week 3:** PnL calculation (FIFO, airdrop-aware)
- **Week 4:** Categorization (4 categories)
- **Weeks 5–6:** Leaderboard + wallet detail (Next.js frontend)
- **Weeks 7–8:** Polish, testing, launch

The original ROADMAP targeted 5–6 weeks against Dune Analytics. Dune's Free tier went view-only in 2026, so V1 now uses Alchemy directly and owns its own indexing layer — hence the ~8-week plan. Full reasoning in [ADR 0003](docs/decisions/0003-alchemy-over-dune.md).

## Decisions

Architectural decisions live in [`docs/decisions/`](docs/decisions/) as ADRs. Current ones:

- [ADR 0001](docs/decisions/0001-airdrop-selection.md) — V1 airdrop selection
- [ADR 0002](docs/decisions/0002-pandas-2x-pin.md) — Pin pandas to 2.x for V1
- [ADR 0003](docs/decisions/0003-alchemy-over-dune.md) — Alchemy as V1 data source (replaces Dune)
- [ADR 0004](docs/decisions/0004-duckdb-cache.md) — DuckDB as V1 local cache and analytical store

## License

MIT.
