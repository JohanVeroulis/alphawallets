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
| Data source | Alchemy free tier (300M CUs/month) — JSON-RPC + Transfers API + Token API. V1 chains: Ethereum + Base. Arbitrum is enabled on the account but out of V1 scope (kept as V1.5 optionality). |
| Historical prices | DefiLlama free API — coverage verified for all V1 tracked tokens, see [ADR 0008](docs/decisions/0008-defillama-historical-prices.md) |
| Local cache | DuckDB single-file store — see [ADR 0004](docs/decisions/0004-duckdb-cache.md) |
| Pipeline / backend | Python 3.11+, managed with `uv` |
| Frontend (Week 5+) | Next.js 14 + TailwindCSS |
| Deployment | Vercel (frontend); scheduled jobs on GitHub Actions |
| Tooling | Git + GitHub, VS Code, Claude Code |

Data source history: V1 originally planned on Dune Analytics; [ADR 0003](docs/decisions/0003-alchemy-over-dune.md) records the switch to Alchemy after Dune's Free tier went view-only in 2026.

**Architecture constraint:** the Alchemy API key must never reach the browser. The frontend reads data through server-side code (Next.js route handlers / server components) or from pre-computed results written by the pipeline, never by calling Alchemy directly from the client.

## 4. Timeline (~8 weeks to V1)

| Week | Focus | Deliverables |
|---|---|---|
| 1 | Data foundation | Alchemy fetchers scaffolded; first real fetcher (Uniswap V3 swaps on ETH + Base) into DuckDB; DefiLlama coverage investigation for the 6 airdrop tokens and 10 major DeFi tokens |
| 2 | Indexing layer | Per-protocol decoders (Uniswap V2/V3, key DeFi protocols); raw → decoded tables in DuckDB; price join for PnL prep |
| 3 | PnL calculation | FIFO cost basis; airdrop-aware separation; wallet-level PnL tables |
| 4 | Categorization | Detection rules for the 4 categories from real data |
| 5 | Leaderboard + first UI | Ranking logic; Next.js scaffold; leaderboard page |
| 6 | Wallet detail | Wallet page; filters (timeframe, category, trade size, min activity) |
| 7 | Polish | Testing, performance, edge cases |
| 8 | Launch prep | README, screenshots, first posts, submissions |

Extra weeks vs the original 5–6 come from taking on indexing ourselves (raw event fetching, per-protocol decoding, price joins) instead of reading Dune's pre-decoded tables. Recorded in [ADR 0003](docs/decisions/0003-alchemy-over-dune.md).

When a task belongs to a later week, point that out before starting it.

## 5. Commands

Fill these in as tooling is added. Do not invent commands that do not exist yet.

```bash
uv sync                 # install dependencies
uv run pytest           # run tests
uv run ruff check .     # lint
uv run ruff format .    # format

# Run the Uniswap V3 fetcher (last 1000 blocks of USDC/WETH 0.05% on Ethereum)
uv run python -m alphawallets.fetchers.uniswap_v3.aw_01_uniswap_v3_swaps \
    --chain ethereum \
    --pool 0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640

# ...or continue from the highest block already stored for that pool
uv run python -m alphawallets.fetchers.uniswap_v3.aw_01_uniswap_v3_swaps \
    --chain ethereum \
    --pool 0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640 \
    --resume

# Run the ERC-20 transfers fetcher (last 1000 blocks of UNI on Ethereum)
uv run python -m alphawallets.fetchers.erc20.aw_02_erc20_transfers \
    --chain ethereum \
    --contract 0x1f9840a85d5af5bf1d1762f925bdaddc4201f984 \
    --blocks 1000

# ...or continue from the highest block already stored for that token
uv run python -m alphawallets.fetchers.erc20.aw_02_erc20_transfers \
    --chain ethereum \
    --contract 0x1f9840a85d5af5bf1d1762f925bdaddc4201f984 \
    --resume

# ...or name the token by symbol, resolved per chain from the V1 registry
# (src/alphawallets/tokens.py — 12 tokens, 18 verified (token, chain) pairs)
uv run python -m alphawallets.fetchers.erc20.aw_02_erc20_transfers \
    --chain base \
    --token-symbol LINK \
    --blocks 1000

# Run the DefiLlama historical prices fetcher (30 days hourly UNI on Ethereum)
uv run python -m alphawallets.fetchers.prices.aw_03_defillama_historical_prices \
    --chain ethereum \
    --token 0x1f9840a85d5af5bf1d1762f925bdaddc4201f984 \
    --span-days 30

# ...or continue from the newest hour already stored for that token
uv run python -m alphawallets.fetchers.prices.aw_03_defillama_historical_prices \
    --chain ethereum \
    --token 0x1f9840a85d5af5bf1d1762f925bdaddc4201f984 \
    --resume

# ...or name the token by symbol from the V1 registry (src/alphawallets/tokens.py).
# 17 of the 18 pairs are priced; MKR has no DefiLlama /chart timeseries — see ADR 0008.
uv run python -m alphawallets.fetchers.prices.aw_03_defillama_historical_prices \
    --chain ethereum \
    --token-symbol AAVE \
    --span-days 7

# Prove the three schemas join: one wallet's priced UNI timeline (auto-picks a wallet)
uv run python -m alphawallets.pipeline.exploration.wallet_activity_proof
```

**Note on dependencies:** `pyproject.toml` declares `duckdb`, `pandas`, `python-dotenv`, `httpx`, `web3` (pinned to 7.x per [ADR 0005](docs/decisions/0005-web3-7x-pin.md)), `eth-abi` (5.x), and `pydantic` (2.x). Dev dependencies: `ruff`, `pytest`, `pytest-cov`, `ipykernel`.

## 6. Conventions

### Git
- **Main branch:** `main`. Never commit directly to it for non-trivial work.
- **Branches:** `feat/`, `fix/`, `docs/`, `chore/`, `refactor/`, `test/` + kebab-case slug (e.g. `feat/aw-01-uniswap-swaps-fetcher`)
- **Commits:** Conventional Commits with the types `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`, `style:`
- Small, focused commits: one logical change each
- Never commit `.env`, secrets, tokens, API keys, or the owner's personal wallet addresses

### Python
- PEP 8; **type hints everywhere** (not optional — this project treats type hints as part of the contract, not a nice-to-have)
- Docstrings on all public functions and classes
- Pydantic models for structured data crossing module boundaries (fetcher outputs, DuckDB schemas, price API responses)
- Lint with `ruff`, format with `ruff format`
- Modules: `snake_case.py`
- Comments explain **why**, not what

### Fetchers

Every fetcher pulls raw data from Alchemy, decodes what it needs, and writes to DuckDB. The patterns below are the working conventions as of the first fetcher (AW_01 Uniswap V3 swaps), and apply to all subsequent fetchers unless a new ADR supersedes them.

- Location: `src/alphawallets/fetchers/<protocol_or_domain>/<aw_XX_description>.py` where `<protocol_or_domain>` is a protocol (`uniswap_v3/`, `aave/`) or a generic data type (`erc20/`). V1 Week 1 starts with `uniswap_v3/` and `erc20/`; new protocols land as their own subfolders when needed.
- Derived analysis lives in a sibling `src/alphawallets/pipeline/` package, organized by stage (`exploration/`, `pnl/`, `categorization/`, `ranking/`). Pipeline stages read from DuckDB and never call Alchemy directly.
- Fetcher module naming: `aw_XX_description.py` where `XX` is a zero-padded sequential number, never reused (`aw_01_uniswap_v3_swaps.py`). Lowercase — Python's snake_case module convention, enforced by ruff's N999 rule.
- Each fetcher module exposes a single entry point (function or class) that takes explicit inputs (chain, block range, target DuckDB path) and returns a summary of what was written — no hidden globals, no reading config from module scope
- Block-range windowing pattern: fixed 10-block windows per `eth_getLogs` request. This is Alchemy's free-tier cap, discovered live and recorded in [ADR 0006](docs/decisions/0006-alchemy-eth-getlogs-block-window.md). Warm-connection rate is ~0.078s per window on both chains; backfill implications are documented in the ADR.
- ABI storage: local JSON files under `<fetcher_dir>/abis/`, sourced from the canonical published artifact (e.g. `@uniswap/v3-core@1.0.1` via unpkg). Reproducible and bytes-identical for anyone re-fetching. Hatchling's default packaging ships them in the wheel without extra config (verified by wheel inspection).
- Raw vs decoded tables in DuckDB: `raw_<protocol>_<event>` for verbatim audit trail, primary key `(chain, block_hash, log_index)` so reorged variants can coexist. `<protocol>_<event>` for decoded pipeline input, primary key `(chain, tx_hash, log_index)`. Reorged logs (`removed=True`) are written to the raw table but excluded from the decoded table. Writes use `INSERT OR IGNORE` for idempotent re-runs. Big integers (`int256` amounts, `uint160` sqrtPriceX96, `uint128` liquidity) are stored as `VARCHAR` — none fit DuckDB's signed HUGEINT reliably.
- Every fetcher module starts with a docstring header: purpose, chain(s), block-range strategy, output tables

Example header (shape only, not a spec — the real one lands in the first fetcher):

```python
"""AW_01 — Uniswap V3 Swap events on Ethereum + Base.

Fetches Swap events from configured V3 pools, decodes them via the pool ABI,
and writes both raw logs and decoded rows to DuckDB.

Chains: ethereum, base
Block-range strategy: fixed window of N blocks per request, retry on 429
Output tables: raw_uniswap_v3_swap, uniswap_v3_swap
"""
```

### File naming
- Python modules and fetchers: `snake_case.py` (fetchers additionally follow the `aw_XX_description.py` pattern above)
- Docs: `kebab-case.md`
- ADRs: `NNNN-title.md` in `docs/decisions/`

## 7. Operating Principles for Claude Code

1. **Read this file first** in every session.
2. **Never guess** credentials, API keys, or env vars. If one is missing, stop and ask.
3. **Never commit** `.env`, secrets, tokens, or the owner's personal wallet addresses. Public wallet addresses that appear as analysis *output* (in DuckDB tables, reports, or leaderboards) are fine; they must never be hard-coded as personal data or committed as fixtures.
4. **Confirm before anything destructive**: `rm`, force push, `git reset --hard`, dropping or overwriting DuckDB tables or files, deleting fetcher modules or ABIs.
5. **New code** comes with type hints, docstrings, and at least one usage example (docstring example or test).
6. **Edited code:** run the relevant tests before calling the change done. Report failures as they are; do not hide them.
7. **Small commits.** Commit or push only when asked.
8. **If scope or intent is unclear, ask before writing code.**
9. **Docs are a deliverable.** Every feature ships with docs (`docs/` or its README section).
10. **English only** in everything produced.
11. **No co-author or "generated by" attribution.** Do not add any of the following to commit messages, PR titles, PR descriptions, issue comments, or any other artifact you create or edit:
    - `Co-Authored-By: Claude <noreply@anthropic.com>` (or similar co-author trailers)
    - `🤖 Generated with Claude Code` (or similar generation attributions)
    - Any variant that credits Claude, Anthropic, or the AI tool as author, co-author, or generator.

    This is a solo project by the owner; Claude Code is a tool, not a co-author. Every artifact is authored solely by Ioannis Veroulis <john.veroulis@gmail.com>.

## 8. Working Together

- Gioxan reviews **step by step**: deliver in small, reviewable increments and pause at natural checkpoints. No big batches.
- Decisions are sometimes discussed elsewhere in Greek and arrive as English drafts. Treat them as input to refine, not as final spec.
- **Learning matters as much as shipping.** Explain the reasoning behind non-obvious choices (fetcher patterns, PnL methodology, data modelling, decoder trade-offs) briefly.

**Claude's role: senior engineer, not just an executor.**
- Lay out trade-offs and give a recommendation.
- Push back on under-specified requirements (e.g. "profitable": over which window, realized or unrealized, minimum sample size?).
- Suggest better patterns when you see them, and explain why.
- Record significant decisions as short ADRs in `docs/decisions/NNNN-title.md`.

## 9. Open Questions & Provisional Decisions

Items marked *provisional* are working assumptions, to be validated in Weeks 1–3 with real data. Record any change as an ADR.

**DuckDB operational conventions** discovered through PR #22 — the UTC session pin, the schema-drift guard, and grid-head-relative price classification — are recorded in [ADR 0009](docs/decisions/0009-duckdb-connection-and-schema-conventions.md). They were never tracked as open questions here; the ADR is their record, and it is the place to look before changing how a connection is opened, a table is created, or an unpriced event is classified.

### Decided
- [x] **Data source:** Alchemy free tier — see [ADR 0003](docs/decisions/0003-alchemy-over-dune.md)
- [x] **Local cache:** DuckDB — see [ADR 0004](docs/decisions/0004-duckdb-cache.md)
- [x] **Scheduled jobs:** GitHub Actions to start with (free, integrated, sufficient for V1; single-writer DuckDB implications noted in ADR 0004)
- [x] **NFTs:** excluded from V1 tracking entirely — noise/wash-trading, illiquid pricing, and different PnL semantics from ERC-20
- [x] **Transfers API vs `eth_getLogs`:** Decided — Transfers API for bulk ERC-20 backfill (no block-window cap, pre-decoded, `logIndex` always absent for ERC-20 category which mandates a `unique_id`-based PK); `eth_getLogs` for protocol-specific event decoding (e.g. Uniswap Swap). See PR #20 and `fetchers/erc20/README.md`.
- [x] **Historical price coverage:** verified against DefiLlama for the 12 V1 tracked tokens (union of 6 airdrops + 10 DeFi) across 4 dates spanning a full year. 100% coverage — see [ADR 0008](docs/decisions/0008-defillama-historical-prices.md). CoinGecko stays in reserve for tokens outside the tracked set.

### Provisional (validate in Weeks 1–3 with real data)

- [~] **PnL methodology:** realized PnL with FIFO cost basis for V1; unrealized PnL in V1.5.

- [~] **"Consistently profitable":** at least 10 trades in the window, positive net PnL, activity within the last 30 days.

- [~] **Wallet-universe filters:** exclude known CEX hot wallets and bridge addresses; exclude contracts EXCEPT known smart-wallet types (Safe multisigs, ERC-4337 accounts, Argent, Ambire) identified via factory-deployment patterns; identify and flag (not exclude) MEV bots. MEV detection approach with Alchemy: Flashbots bundle inclusion (via Flashbots public data), sandwich-attack pattern matching on decoded swaps, and searcher address lists from public sources. Concrete thresholds to be validated in Week 2 with real data.

- [~] **Category rules:** a wallet can belong to multiple categories, with a primary + secondary designation.

- [~] **Wallet-universe seed strategy:** bootstrap from ~500 recent large swaps (>$100k) on Uniswap V3 top pools + known whale lists from public sources (DeBank leaderboards, Arkham labels) + top airdrop recipients who sold $100k+. Expand organically via counterparty analysis in V1.5.

### Open

- [ ] **Uniswap version priority:** V2 and V3 both exist across ETH + Base. Start with V3 (higher volume, better data), add V2 in Week 2. Confirm in Week 1.
- [ ] **ABI storage strategy:** local JSON files in `src/alphawallets/abis/` vs bundled from web3 libraries — decide with the first fetcher.
- [ ] **Category thresholds:** numeric values per category, to be set from real data (Week 4).
- [ ] **Wallet cluster detection:** approach for identifying wallets that share funding origin or coordinated behavior (V2 feature, but detection method affects V1 data collection). Placeholder for later ADR.
- [ ] **Airdrops distributed outside ETH/Base:** LayerZero (ZRO, multi-chain EVM) and similar remain out of V1 scope; revisit for V1.5.
