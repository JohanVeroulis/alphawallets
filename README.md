# AlphaWallets

**Find the wallets that are actually making money on-chain — and see what they're doing right now.**

`Status: private · work in progress · targeting V1` · [Roadmap](ROADMAP.md) · [Current status](STATUS.md)

## What is AlphaWallets?

AlphaWallets is an on-chain analytics tool that identifies consistently profitable wallets on Ethereum and Base. It ranks them by airdrop-aware PnL, sorts them into four behavioural categories, and shows their recent activity.

The point is to replace "influencer said so" with evidence: real wallets, real positions, real results, straight from on-chain data.

## Who it's for

- **Retail crypto traders** who want to see what profitable wallets are doing instead of following calls on social media
- **DeFi users** learning to read on-chain data, who want a worked example of wallet analytics
- **Anyone studying on-chain behaviour**: which strategies actually produce returns over 30 and 90 days

## V1 scope

| | |
|---|---|
| **Chains** | Ethereum mainnet, Base |
| **Categories** | Traders, DeFi users, Airdrop Hunters, Yield Farmers |
| **PnL** | Realized, FIFO cost basis, airdrop-aware (airdrop income kept separate) |
| **Timeframes** | 30d, 90d |
| **Features** | Leaderboard, wallet detail pages, filters (timeframe, category, trade size, min activity) |

Tracked assets: ETH/USDC, ETH/USDT, WBTC/USDC, WBTC/ETH; UNI, AAVE, LDO, PENDLE, CRV, ENA, MKR, MORPHO, LINK, ARB; stablecoin activity in Aave, Compound, Morpho and Pendle. Six airdrops are tracked separately (UNI, ARB, ENA, EIGEN, MORPHO, ETHFI) — see [ADR 0001](docs/decisions/0001-airdrop-selection.md).

Full scope, conventions and open questions: [CLAUDE.md](CLAUDE.md).

## Tech stack

- **Data:** Dune Analytics (DuneSQL / Trino) as the primary V1 source
- **Pipeline:** Python 3.11+, managed with `uv`
- **Local cache:** DuckDB
- **Frontend:** Next.js 14 + TailwindCSS (from Week 4)
- **Deployment:** Vercel for the frontend; scheduled jobs on GitHub Actions
- **Quality:** `ruff` for linting and formatting, `pytest` for tests

## Project structure

```
alphawallets/
├── CLAUDE.md              # Project context and working conventions
├── README.md              # This file
├── ROADMAP.md             # 5–6 week plan to V1
├── STATUS.md              # Where the project stands right now
├── docs/
│   └── decisions/         # Architecture Decision Records (ADRs)
├── queries/               # Dune SQL queries (AW_XX_*.sql)      [planned]
├── src/alphawallets/      # Python pipeline package             [planned]
├── tests/                 # pytest suite                        [planned]
└── web/                   # Next.js frontend                    [planned, Week 4]
```

Folders marked `[planned]` don't exist yet. They arrive in the week they're needed.

## Getting started

> The pipeline isn't built yet. These steps will grow as Week 1 progresses.

**Prerequisites**

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- A Dune Analytics account with API access

**Install**

```bash
git clone https://github.com/JohanVeroulis/alphawallets.git
cd alphawallets
uv sync                      # once pyproject.toml exists
```

**Configure**

```bash
cp .env.example .env         # once .env.example exists
# then add your DUNE_API_KEY to .env
```

`.env` is git-ignored and must never be committed.

**Run**

```bash
uv run pytest                # tests
uv run ruff check .          # lint
uv run ruff format .         # format
```

## Roadmap

Six weeks from data foundation to launch. Weekly milestones, deliverables and expected decisions: [ROADMAP.md](ROADMAP.md).

Current state of play: [STATUS.md](STATUS.md).

## Decisions

Significant choices are recorded as short ADRs in [docs/decisions/](docs/decisions/), numbered and sequential. Start with the [README there](docs/decisions/README.md) for the pattern.

## License

[MIT](LICENSE) © Ioannis Veroulis
