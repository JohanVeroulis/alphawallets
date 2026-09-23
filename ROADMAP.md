# Roadmap

The plan from data foundation to V1 launch, in six weeks. This is a living document: weeks get re-scoped as real data tells us what's feasible. When a week's scope changes, edit it here and say why. Decisions that outlive a week belong in an [ADR](docs/decisions/), not in this file.

Scope reference: [CLAUDE.md](CLAUDE.md) Section 2. Current state: [STATUS.md](STATUS.md).

## Contents

- [Week 1 — Data foundation](#week-1--data-foundation)
- [Week 2 — Leaderboard logic](#week-2--leaderboard-logic)
- [Week 3 — Categorization](#week-3--categorization)
- [Week 4 — Frontend V1](#week-4--frontend-v1)
- [Week 5 — Polish, testing, performance](#week-5--polish-testing-performance)
- [Week 6 — Launch prep](#week-6--launch-prep)
- [Post-V1](#post-v1)

## Week 1 — Data foundation

**Focus:** get a trustworthy wallet universe out of Dune, and understand what the data can and can't tell us.

**Deliverables**
- Dune workspace set up, API key stored locally in `.env`
- `AW_01_wallet_universe`: candidate wallets on Ethereum + Base, filtered by the rules in CLAUDE.md Section 9
- `AW_02_wallet_activity`: per-wallet trade counts, volumes and active days over 30d/90d
- Exploratory PnL query on a small wallet sample, to sanity-check the FIFO approach
- Python project skeleton: `pyproject.toml`, `uv` lockfile, `ruff` config, `pytest` running
- Dune client module with local DuckDB caching of query results

**Key decisions expected**
- Price source: `prices.hour` vs. `prices.day`, and how much CoinGecko fallback is needed → ADR
- Exact exclusion lists: CEX hot wallets, bridges, contracts, and which smart-wallet types to keep
- How MEV bots are flagged (not excluded), and which Dune tables actually carry that data
- Minimum activity thresholds that keep the universe a sensible size

**Blockers / dependencies**
- Dune plan limits: query credits and API rate limits shape how often the pipeline can run
- Verify the real table names for MEV and Flashbots data (`mev.*`, `flashbots.*`) — assumed, not yet confirmed
- Base coverage in Dune's DEX tables may be thinner than Ethereum's

## Week 2 — Leaderboard logic

**Focus:** turn raw activity into a PnL number we trust, and rank on it.

**Deliverables**
- Realized PnL with FIFO cost basis, implemented and unit-tested against hand-worked examples
- Airdrop-aware split: airdrop income separated from trading PnL (the six airdrops from [ADR 0001](docs/decisions/0001-airdrop-selection.md))
- Ranking over both timeframes (30d, 90d) with the "consistently profitable" filter applied
- Leaderboard table materialized in DuckDB, with a stable schema the frontend can rely on
- Tests covering edge cases: partial sells, re-buys, transfers in/out, zero-cost-basis tokens

**Key decisions expected**
- How transfers in and out are treated (bridge? CEX withdrawal? ignored?) — this materially changes PnL
- Whether to publish a single PnL number or split it into trading / airdrop / yield components
- The final "consistently profitable" thresholds, validated against real distributions → ADR

**Blockers / dependencies**
- Depends on the Week 1 wallet universe and price source being settled
- Wallets that trade through contracts may need special handling before their PnL is meaningful

## Week 3 — Categorization

**Focus:** classify wallets into the four categories, with rules that survive inspection.

**Deliverables**
- Detection rules for Traders, DeFi users, Airdrop Hunters and Yield Farmers
- Primary + secondary category per wallet, with a confidence signal
- Airdrop claim detection for the six V1 airdrops, including ARB's bridged-only caveat
- Category thresholds set from real distributions, not guesses
- A labelled sample (30–50 wallets) checked by hand to measure how often the rules are right

**Key decisions expected**
- Numeric thresholds per category → ADR
- How to treat wallets that fit no category, or fit all of them
- Whether MORPHO rewards were also distributed on Base (open item from ADR 0001)

**Blockers / dependencies**
- Needs Week 2 PnL, since profitability feeds the categories
- Yield Farmer detection depends on protocol-level position data (Aave, Compound, Morpho, Pendle), which is more fragmented than DEX data

## Week 4 — Frontend V1

**Focus:** make it usable by someone who isn't us.

**Deliverables**
- Next.js 14 + Tailwind scaffold, deployed to Vercel
- Leaderboard page: ranking, category badges, PnL, timeframe switch
- Wallet detail page: PnL breakdown, category reasoning, recent activity
- Filters: timeframe, category, trade size, minimum activity
- Server-side data access only — the Dune API key never reaches the browser (CLAUDE.md Section 3)

**Key decisions expected**
- How data reaches the frontend: pre-computed JSON committed by a job, a hosted database, or server-side Dune calls → ADR
- How stale the data is allowed to be, and how that's shown to the user
- Whether wallet addresses are displayed in full, shortened, or with ENS names

**Blockers / dependencies**
- Needs a stable leaderboard schema from Week 2
- Vercel project and environment variables set up

## Week 5 — Polish, testing, performance

**Focus:** make it fast, correct and presentable.

**Deliverables**
- End-to-end pipeline run on a schedule via GitHub Actions
- Query cost and runtime measured, then reduced where it's worst
- Test coverage on PnL and categorization; a smoke test for the pipeline
- Mobile layout, loading states, empty states, error states
- Spot-check of leaderboard results against block explorers, with any discrepancies fixed

**Key decisions expected**
- Refresh frequency, balanced against Dune credit consumption
- What to do when a scheduled run fails: serve stale data, or show an error

**Blockers / dependencies**
- GitHub Actions runtime limits versus how long a full refresh takes

## Week 6 — Launch prep

**Focus:** ship it and explain it.

**Deliverables**
- README polished with screenshots and a live link
- A short methodology page: how PnL is computed and what the categories mean
- Twitter/X thread drafted
- Product Hunt listing prepared
- Announcement plan executed: Twitter/X thread published, cross-posted to r/ethdev and r/defi, and shared with Greek crypto communities
- Decision on making the repo public

**Key decisions expected**
- Public repo or private, and whether the methodology is published in full
- Whether the leaderboard is open to everyone or gated in some way

**Blockers / dependencies**
- Needs a stable deployment from Week 5

## Post-V1

On hold by design. Don't build these during V1.

| Item | Target | Note |
|---|---|---|
| Solana support | V1.5 | Brings the Solana airdrops (JUP, JITO, W, LAYER) with it |
| Real-time alerts | V1.5 | Needs streaming data, not batch queries |
| Upcoming-airdrop feed | V1.5 | Different product surface from the leaderboard |
| Cross-chain wallet linking | V1.5+ | Prerequisite for attributing ZRO, OP, STRK, ZK — see [ADR 0001](docs/decisions/0001-airdrop-selection.md) |
| Unrealized PnL | V1.5 | V1 deliberately ships realized PnL only |
| Native BTC and XRP | Not planned | The data doesn't fit this use case |
