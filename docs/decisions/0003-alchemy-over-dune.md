# 0003 — Alchemy as V1 data source (replaces Dune)

- **Status:** Accepted
- **Date:** 2026-09-23

## Context

The original V1 plan assumed Dune Analytics as the primary data source. ADR 0001, CLAUDE.md, ROADMAP, and all six `queries/` READMEs are Dune SQL-oriented. `pyproject.toml` declared `dune-client` as a dependency.

On 2026-09-23 we discovered:

- Dune Free tier is now **view-only** (as of 2026-09-23): it cannot run custom queries or provide API keys.
- The Dune Analyst plan required to unblock V1 costs **$75/month** ($65/mo billed annually, as of 2026-09-23).
- That monthly cost is not comfortable for a pre-revenue project whose owner has explicitly flagged tight discretionary spend.

We evaluated three alternatives:

1. Pay for Dune Analyst.
2. Hybrid: Dune Analyst for aggregate discovery, Alchemy for per-wallet detail.
3. Alchemy alone: replace Dune entirely, build a custom indexing layer on top of raw RPC.

## Decision

**Alchemy is the sole V1 data source.** No Dune usage in V1.

Alchemy's free tier provides:

- 300M compute units per month (expected sufficient for V1 scope; to be measured in Week 1)
- Full JSON-RPC access to Ethereum, Base, Arbitrum (all enabled 2026-09-23)
- Transfers API and Token API (built-in decoding for common cases)
- No cost, no time limit
- One API key covers all enabled networks

End-to-end connectivity was verified from Python on 2026-09-23 against all three chains (live block heights returned).

## Alternatives Considered

| Alternative | Why not |
|---|---|
| Pay Dune Analyst ($75/mo) | Contradicts the pre-revenue cost constraint that motivated this review. The whole reason we're here. |
| Hybrid Dune + Alchemy | Still requires paying Dune. Two systems to maintain, vendor lock across two providers, and no clean line between what belongs where until real usage teaches us. Reconsider in V2 if a Dune-only capability becomes essential. |

## Consequences

**What we gain**

- Zero data-source cost throughout V1.
- Full control over indexing, cache layout, and query semantics.
- Stronger portfolio signal: "built custom indexer from raw RPC" reads more senior than "wrote Dune queries."
- Real-time capability path (Alchemy webhooks) opens up for V1.5+ features like Exit Signal Detection.
- Foundation for advanced features (Correlation Warning, Wallet DNA, Rug Warning) that Dune's aggregate SQL cannot support cleanly.

**What we lose / take on**

- +2–3 weeks in the V1 timeline for the indexing layer.
- Grunt work up front: fetch raw logs, decode swaps, store, before any wallet-level insight appears.
- Domain knowledge required: ABIs, event signatures, per-protocol decoders (Uniswap V3, Aave, Compound, etc.).
- We forgo Dune's pre-decoded tables (`dex.trades`, `prices.hour`); we build the equivalents for what V1 needs.
- **Historical USD prices are a hard prerequisite we now own.** Dune's `prices.hour` and `prices.day` tables gave clean per-token per-timestamp USD prices for free. Alchemy does not. FIFO PnL for wallet-level analytics cannot be computed without them. Candidate sources: DefiLlama's free price API (historical, no key required — verify coverage and rate limits in Week 1), CoinGecko free tier (rate-limited and mostly daily-granularity), or deriving USD from DEX pool swap rates at trade time (technically complete, operationally heavy). This is a Week 1 investigation, not a Week 2 discovery.

**Revisit when**

- V2 planning, if Dune adds a free execution tier again.
- Any V1 feature is genuinely blocked without a dataset only Dune has.
- Alchemy free-tier limits become a real constraint on scope.
- Historical pricing coverage from the chosen free source (Week 1 outcome) turns out to be insufficient for the airdrop and thin-market cases the V1 scope depends on.

## Impact on existing artifacts (deferred to PR #6)

CLAUDE.md, README, ROADMAP, the `queries/` folder structure, ADR 0001, and ADR 0002 all reference Dune. **None are changed in this PR.** They are rewritten in a follow-up architectural PR that:

- Renames `queries/` → `src/alphawallets/fetchers/`.
- Rewrites CLAUDE.md scope, conventions, and open questions for the Alchemy path.
- Rewrites ROADMAP week-by-week to reflect the +2–3 week indexing-layer budget.
- Rewrites `.env.example` (Dune → Alchemy).
- Removes `dune-client` from `pyproject.toml`.
- Adds this ADR's number to the affected ADRs' "superseded / amended by" notes where relevant.

Until PR #6 lands, existing artifacts still describe the pre-Alchemy plan. Read them with that in mind.
