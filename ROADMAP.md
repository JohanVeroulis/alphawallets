# Roadmap

The plan from data foundation to V1 launch, in ~8 weeks. This is a living document: weeks get re-scoped as real data tells us what's feasible. When a week's scope changes, edit it here and say why. Decisions that outlive a week belong in an [ADR](docs/decisions/), not in this file.

Scope reference: [CLAUDE.md](CLAUDE.md) Section 2. Data source: [ADR 0003](docs/decisions/0003-alchemy-over-dune.md). Local cache: [ADR 0004](docs/decisions/0004-duckdb-cache.md). Current state: [STATUS.md](STATUS.md).

## Contents

- [Week 1 — Data foundation](#week-1--data-foundation)
- [Week 2 — Indexing layer](#week-2--indexing-layer)
- [Week 3 — PnL calculation](#week-3--pnl-calculation)
- [Week 4 — Categorization](#week-4--categorization)
- [Week 5 — Leaderboard + first UI](#week-5--leaderboard--first-ui)
- [Week 6 — Wallet detail](#week-6--wallet-detail)
- [Week 7 — Polish, testing, performance](#week-7--polish-testing-performance)
- [Week 8 — Launch prep](#week-8--launch-prep)
- [Post-V1](#post-v1)

## Week 1 — Data foundation

**Focus:** first real Alchemy fetcher writing to DuckDB, plus historical price source validation. The whole indexing plan depends on both working.

**Deliverables**
- First fetcher: `AW_01_uniswap_v3_swaps.py` under `src/alphawallets/fetchers/uniswap_v3/` — pulls Swap events from top V3 pools on Ethereum + Base, decodes them, writes raw + decoded tables to DuckDB
- `AW_02_erc20_transfers.py` under `src/alphawallets/fetchers/erc20/` — Transfers API for tracked DeFi tokens and airdrop distributions
- DefiLlama coverage investigation: verify historical price availability for the 6 V1 airdrop tokens (UNI, ARB, ENA, EIGEN, MORPHO, ETHFI) and 10 major DeFi tokens (UNI, AAVE, LDO, PENDLE, CRV, ENA, MKR, MORPHO, LINK, ARB). Document gaps.
- Alchemy CU consumption measured for a full fetch cycle, extrapolated to a monthly budget
- Fetcher conventions finalized in CLAUDE.md Section 6 (block-range windowing, ABI storage, raw vs decoded table naming)

**Key decisions expected**
- Historical price source: DefiLlama primary, fallback plan for gaps → ADR
- Block-range windowing pattern for `eth_getLogs` under Alchemy limits
- ABI storage: local JSON files in `src/alphawallets/abis/` vs bundled from web3 libraries
- Transfers API vs `eth_getLogs` split per protocol/data type

**Blockers / dependencies**
- Alchemy free tier CU budget (300M/month) — Week 1 measurement establishes whether V1 scope fits
- DefiLlama historical coverage — a hard gate; without prices we cannot compute PnL. If DefiLlama comes up short, Week 1 extends until a solution is found.
- Base pool coverage and event volume — first empirical check of Base as a first-class chain

## Week 2 — Indexing layer

**Focus:** turn raw events into decoded, joined, price-annotated tables that PnL can consume. This is the work Dune's pre-decoded tables used to do for us.

**Deliverables**
- Per-protocol decoders: Uniswap V2 (added this week alongside V3), plus scaffolding for future DEX protocols
- Aave and Compound position-data fetchers (deposits, borrows, positions) — the two largest DeFi protocols in V1 scope, needed for Yield Farmer categorization
- Wallet-universe fetcher: candidate wallets from `AW_01`/`AW_02` output — recent large swap actors, known whale seeds, top airdrop recipients (see CLAUDE.md Section 9 seed strategy)
- Per-wallet activity summary tables (trade counts, volumes, active days over 30d/90d)
- Historical price join: chosen source (Week 1 outcome) integrated as a DuckDB table joinable to swap timestamps
- Wallet-universe filters applied: exclude CEX hot wallets and bridges, exclude contracts except known smart-wallet types, flag MEV bots

**Key decisions expected**
- MEV detection with Alchemy: sandwich-attack pattern matching threshold, Flashbots bundle inclusion signal, searcher address list source → note in ADR or CLAUDE.md
- Which smart-wallet types are worth the effort in V1 (Safe multisigs and ERC-4337 accounts likely; Argent/Ambire deferred if factory patterns are painful)
- Minimum activity thresholds that keep the wallet universe a workable size (target: low thousands, not tens of thousands)

**Blockers / dependencies**
- Week 1 fetchers must be stable and writing well-shaped tables
- Price coverage must be sufficient — gaps here become PnL holes in Week 3
- Aave/Compound event data volume — first empirical check of whether position tracking fits the CU budget alongside DEX events

## Week 3 — PnL calculation

**Focus:** turn indexed activity into a PnL number that survives spot-checking against block explorers.

**Deliverables**
- Realized PnL with FIFO cost basis, implemented in `src/alphawallets/pipeline/pnl/` and unit-tested against hand-worked examples
- Airdrop-aware split: airdrop-sourced balances tagged and their PnL kept separate from trading PnL (the six airdrops from [ADR 0001](docs/decisions/0001-airdrop-selection.md))
- Per-wallet PnL tables materialized in DuckDB with a stable schema
- Tests covering edge cases: partial sells, re-buys, transfers in/out, zero-cost-basis tokens, wallets that trade through smart-contract wallets

**Key decisions expected**
- How transfers in and out are treated (bridge? CEX withdrawal? ignored?) — this materially changes PnL and needs an ADR
- Whether to publish a single PnL number or split it into trading / airdrop / yield components
- How to handle wallets whose activity predates the block range we've indexed (assume zero-cost basis? backfill? exclude?)

**Blockers / dependencies**
- Depends entirely on Week 2's indexing layer and price join
- Missing prices force per-wallet exclusions; PnL confidence depends on coverage completeness

## Week 4 — Categorization

**Focus:** classify wallets into the four categories with rules that survive inspection.

**Deliverables**
- Detection rules for Traders, DeFi users, Airdrop Hunters and Yield Farmers, implemented in `src/alphawallets/pipeline/categorization/`
- Primary + secondary category per wallet, with a confidence signal
- Airdrop claim detection for the six V1 airdrops, including ARB's bridged-only caveat
- Category thresholds set from real distributions, not guesses
- A labelled sample (30-50 wallets) checked by hand to measure how often the rules are right

**Key decisions expected**
- Numeric thresholds per category → ADR
- How to treat wallets that fit no category or fit all of them
- Yield Farmer detection: Aave and Compound land in Week 2. Morpho and Pendle fetchers get built here if the CU budget allows; if not, their categorization is weaker in V1 and improved in V1.5.

**Blockers / dependencies**
- Needs Week 3 PnL — profitability feeds the categories
- Yield Farmer detection quality depends on how many DeFi protocols were indexed in Week 2. Morpho and Pendle may slip if the CU budget is tight; accept degraded categorization for those protocols in V1.

## Week 5 — Leaderboard + first UI

**Focus:** turn ranked wallets into something someone else can look at.

**Deliverables**
- Ranking logic in `src/alphawallets/pipeline/ranking/`: per-timeframe (30d, 90d), per-category leaderboards with the "consistently profitable" filter applied
- Ranked wallet tables in DuckDB, stable schema for the frontend to consume
- Next.js 14 + Tailwind scaffold, deployed to Vercel
- Leaderboard page: ranking, category badges, PnL, timeframe switch
- Server-side data access only — the Alchemy API key never reaches the browser (CLAUDE.md Section 3)

**Key decisions expected**
- How data reaches the frontend: pre-computed JSON committed by a job, a hosted database, or an API layer over the DuckDB file → ADR
- How stale the data is allowed to be, and how that's shown to the user
- Whether wallet addresses are displayed in full, shortened, or with ENS names

**Blockers / dependencies**
- Needs a stable ranking output schema
- Vercel project and environment variables set up

## Week 6 — Wallet detail

**Focus:** the per-wallet story — PnL breakdown, category reasoning, recent activity.

**Deliverables**
- Wallet detail page: PnL breakdown (trading vs airdrop), category reasoning (why this wallet is a Trader), recent activity feed
- Filters on the leaderboard: timeframe, category, trade size, minimum activity
- Deep-link support for individual wallets (shareable URLs)

**Key decisions expected**
- How much recent activity to show, and what "recent" means
- Whether wallet detail is public URL-accessible or gated behind the leaderboard

**Blockers / dependencies**
- Needs Week 5 leaderboard and stable ranking data

## Week 7 — Polish, testing, performance

**Focus:** make it fast, correct and presentable.

**Deliverables**
- End-to-end pipeline run on a schedule via GitHub Actions (single-writer DuckDB implications noted in [ADR 0004](docs/decisions/0004-duckdb-cache.md) — decide serialization strategy here)
- Alchemy CU consumption and pipeline runtime measured, then reduced where it's worst
- Test coverage on fetchers, decoders, PnL, and categorization; a smoke test for the full pipeline
- Mobile layout, loading states, empty states, error states
- Spot-check of leaderboard results against block explorers, with any discrepancies fixed

**Key decisions expected**
- Refresh frequency, balanced against Alchemy CU consumption
- What to do when a scheduled run fails: serve stale data, or show an error
- DuckDB single-writer approach for scheduled runs: serialize, fan out to per-chain files, or another option (per ADR 0004)

**Blockers / dependencies**
- GitHub Actions runtime limits vs how long a full refresh takes
- Alchemy CU monthly budget

## Week 8 — Launch prep

**Focus:** ship it and explain it.

**Deliverables**
- README polished with screenshots and a live link
- A short methodology page: how PnL is computed, what the categories mean, what the data limitations are
- Build-in-public posts drafted for X/Twitter around the build — early ones can go live before launch to build momentum
- Announcement plan: X/Twitter thread published, cross-posted to r/ethdev and r/defi, shared with Greek crypto communities
- Decision on making the repo public

**Key decisions expected**
- Public repo or private, and whether the methodology is published in full
- Whether the leaderboard is open to everyone or gated in some way

**Blockers / dependencies**
- Needs a stable deployment from Week 7

## Post-V1

On hold by design. Don't build these during V1.

| Item | Target | Note |
|---|---|---|
| Solana support | V1.5 | Brings the Solana airdrops (JUP, JITO, W, LAYER) with it |
| Real-time alerts | V1.5 | Needs Alchemy webhooks; unlocks Exit Signal Detection |
| Wallet DNA (basic panel) | V1.5 | Hold time distribution, chain preference, active hours, top categories — deferred to keep V1 scope locked |
| Full Wallet DNA | V1.5+ | The full metric set (entry patterns, trade frequency, position sizing, response speed), beyond the basic panel row above |
| Watchlist alerts | V1.5 | User-selected wallets → Telegram / Discord / email notifications |
| Rug/Scam Warning | V1.5 | Anti-signal for tokens no profitable wallet touches |
| Exit Signal Detection | V1.5 | Real-time position-shrink alerts on tracked wallets |
| Consensus Alerts | V2 | 3+ profitable wallets buying the same token in a short window |
| Wallet cluster / correlation analysis | V2 | Detects wallets that share funding or coordinated behavior |
| Best Trade of the Week | V2 | Automated storytelling from real trades — content engine |
| Wallet Case Studies | V1.5-V2 | Manually researched deep-dives; content-driven |
| Cross-chain wallet linking | V2 | Prerequisite for attributing ZRO, OP, STRK, ZK across chains — see [ADR 0001](docs/decisions/0001-airdrop-selection.md) |
| Unrealized PnL | V1.5 | V1 deliberately ships realized PnL only |
| Native BTC and XRP | Not planned | The data doesn't fit this use case |

## Note on the original 5–6 week plan

The original ROADMAP targeted 5–6 weeks against Dune Analytics. [ADR 0003](docs/decisions/0003-alchemy-over-dune.md) records the move to Alchemy after Dune's Free tier went view-only in 2026, adding an indexing layer we now own (raw event fetching, per-protocol decoding, historical price integration). The 8-week plan absorbs that budget.
