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

**Quote assets, not scope.** WETH and USDC are in the token registry (`src/alphawallets/tokens.py`) but are **not** leaderboard subjects — `QUOTE_ONLY_SYMBOLS` marks them and `SUBJECT_SYMBOLS` is still the twelve above. Every V1 pool is TOKEN/WETH, and [ADR 0015](docs/decisions/0015-per-swap-executed-price.md) prices a swap from its executed amount ratio anchored to the quote side's hourly price — which requires that price to exist. They are the denominator of a trade, not an asset V1 ranks wallets on; ranking a wallet by its WETH PnL would rank it on the numeraire every one of its trades passes through.

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

# Pool choice: one primary pool per V1 token is configured per chain in
# DEFAULT_ETHEREUM_POOLS / DEFAULT_BASE_POOLS. Pass any of their addresses to
# --pool; see fetchers/uniswap_v3/README.md for the table and the pair/fee policy.

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

# Compute realized PnL into wallet_pnl (reads the cache only; --dry-run to preview)
uv run python -m alphawallets.pipeline.pnl
uv run python -m alphawallets.pipeline.pnl --dry-run
uv run python -m alphawallets.pipeline.pnl --wallet 0xABC... --chains ethereum
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

**PnL methodology is decided.** Ten interdependent decisions — FIFO cost basis, transfer-IN priced at receipt rather than zero, transfer-OUT ending tracking without realizing, airdrop separation by distribution contract, running cost basis sliced by realization time, and three caveat flags that surface limitations instead of excluding wallets — are recorded in [ADR 0012](docs/decisions/0012-pnl-methodology.md), with the `wallet_pnl` schema. This supersedes the provisional "PnL methodology" and "transfer treatment" entries below. Implementation lives in `pipeline/pnl/` (not an `AW_*` module, per Section 6) and is complete through step 5 — see the implementation note below. [ADR 0014](docs/decisions/0014-pool-destination-realization.md) narrows decision 3: a transfer-OUT to a known Uniswap V3 pool *is* a realization, because the counterparty is known — every other OUT keeps decision 3's treatment. [ADR 0015](docs/decisions/0015-per-swap-executed-price.md) then changes how those realizations are priced: PR #42's dry-run measured that the hourly grid books **exactly $0 for 65.4% of realizations**, because an intra-hour buy and sell resolve to the same unit price — total signal loss for the scalpers and arb bots the leaderboard exists to find. `uniswap_v3_swap` therefore becomes a second event source, priced at the executed ratio, with its transfer legs deduplicated. Implemented in PRs #44, #45 and #46; intra-hour trading PnL is live, and the measured effect is in the implementation note below.

**Backfill cost is chain-asymmetric.** Base is roughly 18x more expensive to backfill than Ethereum — ~6x more `eth_getLogs` windows per wall-clock minute (2s block time against ADR 0006's 10-block cap) and ~3x slower Transfers API pages. Pagination within a `(token, chain)` pair cannot be parallelised, so the design response is concurrency across pairs with per-chain DuckDB files and per-chain CUPS budgets: [ADR 0011](docs/decisions/0011-chain-aware-backfill-parallelization.md), implementation tracked as a follow-up PR. Until it lands the cache is one file, backfills are serial, and mid-run progress is unreadable because the writer holds the lock.

**Price coverage classification is four-state.** `priced` / `pending` / `unavailable` / `unpriceable`, with only `unavailable` representing a real gap. The fourth state closes the Week 3 awareness item raised in PR #25: a token no route can serve never acquires a grid head, so without it every such event read as indefinitely `pending`. Source is `src/alphawallets/unpriceable.py` until ADR 0010's route cache lands. See `pipeline/exploration/README.md` for the table.

**DefiLlama price coverage is endpoint-specific, and the fallback is implemented.** The `/chart` route covers 17 of the 18 verified (token, chain) pairs; MKR is served only per-timestamp. [ADR 0010](docs/decisions/0010-defillama-historical-fallback.md) is now built: AW_03 probes `/chart` once per token, caches the verdict in `token_price_route`, and falls back to `/prices/historical` where needed. MKR is priced. `unpriceable` is no longer a hand-maintained list — `is_unpriceable()` reads the route cache, so a token becomes priceable the moment a route is found for it.

**ADR 0012 implementation is NOT complete.** Step 6 — the hand spot-check against a block explorer — found a methodology bug that invalidates the dollar figures below, so the phase stays open. See [ADR 0016](docs/decisions/0016-contract-mediated-attribution.md). Steps 1-5 are built and the calculator runs end to end: FIFO cost basis (PR #33), transfer treatment (#34), event reader (#35), price layer (#36), engine wiring (#37), pool-destination realization (#39, #40), window emission (#41), writer + CLI (#42), swap event source (#44, #45) and quote assets (#46). Step 6 has now been **run**, and it failed: ADR 0016's exclusion is the next implementation PR, after which step 6 must be repeated against the corrected output. That repeat should still include a wallet trading outside the primary fee tier, per ADR 0014 — the 2026-10-09 pass did, and quantified that gap as the majority case (2,598 pool-bound sales to unconfigured 0.05% pools against 2,066 to configured ones).

The first full real run (2026-10-08) wrote 37,214 rows over 18,607 `(chain, wallet, token)` partitions. An operational AW_01 backfill the same day aligned the swap and transfer windows on Ethereum, which moved the headline measurement — **but read the dollar rows with ADR 0016's caveat: both of them are swap-only partitions and therefore misattributed.**

| metric | before | after |
|---|---|---|
| zero-PnL share of realizations | 53.4% (658/1,232) | **20.1%** (250/1,246) |
| — ethereum | 51.3% (460/896) | **5.7%** (52/910) |
| — base | 58.9% (198/336) | 58.9% — unchanged |
| top eligible 30d trading PnL | $476.17 | $544.27 |

The zero-share rows hold: the Ethereum collapse from 51.3% to 5.7% is real. The **dollar rows do not** — $476.17 and $544.27 are both swap-only partitions whose PnL ADR 0016 excludes from ranking, so they are not a measured improvement in anything.

**Step 6 is the step that worked.** External validation found what five internal steps and a closed-to-the-cent FIFO identity check could not: a wrong *subject*. Internal consistency cannot detect misattribution, because it is internally derived. Any future ADR touching attribution needs an independent oracle, not a self-check.

**Open: swap-only EOAs are double-counted against their custody contracts (V1.5+).** ADR 0016 flags a signer EOA that never holds the token it trades, and excludes it from ranking — but the custody contract keeps its own `wallet_pnl` partition built from the same transaction hashes. Measured 2026-10-09: `0x7bf30399db…` (signer) carries $544.27 and a phantom 20,831.96 UNI balance while `0xbdb3ba9ffe…` (holder, 7,175 transfer rows, 22 `wallet_pnl` rows) carries $0.00 and 12,684.88 UNI, sharing 10 transaction hashes. Reconciling the two into one subject needs custody-address attribution, which needs the transfer legs ADR 0015's `(tx_hash, token_address)` dedup currently drops plus an AW_02 refetch for contract addresses. Deferred to V1.5.

**Correction owed to ADR 0015.** Its field table calls `tx_from` "the trader EOA — the partition's wallet". It is **the transaction signer, which may or may not be the token holder**. ADR 0016 records the correction; it lands in that ADR's implementation PR, not before.

**The leaderboard is too thin to be meaningful yet.** With ADR 0016's exclusion applied to the 2026-10-08 run, eligible profitable 30d rows fall from 90 to 32, the top entry from $544.27 to $67.90, and only **2 wallets** meet the provisional "consistently profitable" bar of ≥10 trades. Widening the wallet universe is now a blocking dependency for the leaderboard, not an enhancement.

The remaining zero-PnL rows are **79% Base**. The fix was not a code change: 15 of 19 configured pools held only PR #28's 100-block smoke test, sitting above the transfer ceiling and therefore structurally unmatchable. Two lessons worth keeping: `--resume` cannot close a gap *below* the highest stored block (it only moves forward to head, so explicit `--from-block`/`--to-block` is the instrument for backfilling a hole), and CU budget was never the constraint — 8,484 windows cost ~0.64M CU of 300M/month, while wall clock and ADR 0007's CUPS ceiling were what actually bounded the work.

**Open: `_resolve_as_of` ignores the swap table.** `pipeline/pnl/calculator.py` resolves the default `as_of` from `MAX(erc20_transfer.block_timestamp)`, but ADR 0015 made `uniswap_v3_swap` a second event source — so its docstring's claim to include "every indexed event" is no longer true. Measured 2026-10-08: 178 of 5,154 swaps (3.5%) fall past the bound, **including all 10 Base swaps**, which is why Base PnL is insensitive to swap backfilling. Fix is `GREATEST` over both tables' maxima.

**Future bootstrap work: two Base pools have never been fetched.** `LINK/WETH 0.3%` (`0x224a5d3f2155f2f85af70b6d72aea61a15273ff4`) and `PENDLE/WETH 0.3%` (`0xd7042869277c75ca56f1f6cc7e18ff0d83410dee`) are configured in `DEFAULT_BASE_POOLS` and hold zero rows. They need an initial backfill, not window alignment — a different decision from the Ethereum work above, and deliberately deferred. Note that because [ADR 0014](docs/decisions/0014-pool-destination-realization.md) makes the pool set accounting-relevant, a configured-but-empty pool still counts as a known pool for realization purposes while contributing no executed prices.

**Base needs volume before it needs alignment.** Its swap table holds 10 rows against 173,991 transfers. Aligning its windows was costed at 44,734 `eth_getLogs` windows (~104 min, ~3.4M CU) and rejected on that ratio: the chain needs a broader initial backfill first.

**Open: provider grid granularity.** MKR's historical data is on a 4-hour grid, so events in the other three hours of each block classify as `unavailable` and print a "re-run AW_03" remedy that cannot help. Same error shape ADR 0010 prevents, one level down. Proposed as ADR 0013 — likely a bounded nearest-prior lookup plus a distinct off-grid state.

### Decided
- [x] **Data source:** Alchemy free tier — see [ADR 0003](docs/decisions/0003-alchemy-over-dune.md)
- [x] **Local cache:** DuckDB — see [ADR 0004](docs/decisions/0004-duckdb-cache.md)
- [x] **Scheduled jobs:** GitHub Actions to start with (free, integrated, sufficient for V1; single-writer DuckDB implications noted in ADR 0004)
- [x] **NFTs:** excluded from V1 tracking entirely — noise/wash-trading, illiquid pricing, and different PnL semantics from ERC-20
- [x] **Transfers API vs `eth_getLogs`:** Decided — Transfers API for bulk ERC-20 backfill (no block-window cap, pre-decoded, `logIndex` always absent for ERC-20 category which mandates a `unique_id`-based PK); `eth_getLogs` for protocol-specific event decoding (e.g. Uniswap Swap). See PR #20 and `fetchers/erc20/README.md`.
- [x] **Historical price coverage:** verified against DefiLlama for the 12 V1 tracked tokens (union of 6 airdrops + 10 DeFi) across 4 dates spanning a full year. 100% coverage — see [ADR 0008](docs/decisions/0008-defillama-historical-prices.md). CoinGecko stays in reserve for tokens outside the tracked set.

### Provisional (validate in Weeks 1–3 with real data)

- [x] **PnL methodology:** realized PnL with FIFO cost basis for V1; unrealized PnL in V1.5. No longer provisional — the full methodology, including transfer treatment and the airdrop split, is decided in [ADR 0012](docs/decisions/0012-pnl-methodology.md).

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
