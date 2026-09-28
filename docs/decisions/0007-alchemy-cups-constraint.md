# 0007 — Alchemy free-tier CUPS constrains request concentration, not total volume

- **Status:** Accepted
- **Date:** 2026-09-28

## Context

PR #13 added `tx_from` enrichment to Uniswap V3 swap decoding via one `eth_getTransactionByHash` call per unique tx_hash. That took the AW_01 pipeline from ~0.15s per 10-block window to ~0.9s, and pushed Base 90d backfill to ~4-5 days per pool. Issue #17 proposed JSON-RPC batching of the `eth_getTransactionByHash` calls as the fix, with the hypothesis that batching would cut HTTP round-trip overhead by ~10× while leaving CU cost unchanged.

Implementation on branch `feat/batch-tx-from-lookups` used web3.py 7.x's `batch_requests()` context manager with a batch size of 50. Unit tests passed (batching worked in isolation with mocked responses). The first live benchmark against Alchemy — 1000 blocks on Ethereum — failed with cascading HTTP 429 errors:

    'Your app has exceeded its compute units per second capacity...'

Batches of 16, 15, 18, 29, 33 unique hashes all rate-limited. The per-tx fallback path fired but was rate-limited immediately as well because the retries had no backoff, and the whole run died.

Root cause. Alchemy's free tier enforces a **compute-units-per-second (CUPS)** ceiling in addition to the monthly budget. Batching 50 `eth_getTransactionByHash` calls into one HTTP request causes Alchemy to charge their combined CU cost in a single instant, which exceeds the CUPS ceiling and returns 429. Public pricing pages should be consulted for the current CUPS value and per-method CU cost; what the benchmark proved is the mechanism — concentrating calls trips the rate limit — not any specific number. The premise of Issue #17 was correct about total CU (batching doesn't change it) but missed the rate dimension.

Why the unbatched implementation was already at a local optimum. HTTP round-trip time (~80-100ms per call to Alchemy from Greece) naturally spreads consumption:

- Unbatched: HTTP round-trips (~80-100ms each to Alchemy from Greece) cap the pipeline at roughly 10 requests per second, spreading CU consumption over time — enough headroom that no 429s ever fired on any run
- Batched (50/req): the same CU consumed in a single instant — 429s on every batch

The HTTP latency was accidentally acting as a rate limiter, and it was doing its job.

## Decision

**Do not batch `eth_getTransactionByHash` calls on the free tier.** The `feat/batch-tx-from-lookups` branch is abandoned; the code stays as PR #13 shipped it.

Backfill wall-clock stays at the "+ tx_from" column of ADR 0006's table (Ethereum 90d ~15-20h, Base 90d ~4-5 days per pool). This is honest cost, not a bug.

Concrete rules going forward:

1. **Assume the free-tier CUPS ceiling is a real constraint on request concentration.** Any pattern that concentrates calls (batching, parallelism, tight loops with cheap RPC methods) needs to be checked against it before it lands. The exact CU-per-method and CUPS values live in Alchemy's docs and should be re-checked before any concentrated-call design.
2. **HTTP latency is a resource, not just cost.** It provides free rate limiting. Removing it — via batching, keep-alive tuning, HTTP/2, or a geographically closer endpoint — requires a matching CUPS strategy.
3. **A paid Alchemy tier stays open as an escape hatch.** Higher-tier plans lift the CUPS ceiling, which would make batching viable and cut Base 90d backfill from days to hours. This becomes a real option when V1 traction or a specific business need justifies the spend. Exact tier pricing to be verified when the decision is on the table.

## Alternatives Considered

**Fix batching with sleep + exponential backoff + smaller batch size.** A batch size and inter-batch sleep tuned to stay under the CUPS ceiling would likely yield a modest speedup (rough estimate ~2×). Rejected: the complexity (backoff, retry, timing) is real and the benefit is marginal — Base 90d still lands at ~2 days, still too slow for scheduled backfill. Not worth the failure surface on a path that will be replaced by paid-tier batching or scope narrowing anyway.

**Adopt a paid Alchemy tier now.** Rejected. V1 is pre-revenue (see ADR 0003) and the current dev cycle doesn't need faster backfill. Real backfill is a Week 5-7 concern per the ROADMAP; the decision can be made then with more information and current pricing.

**Use multiple free-tier API keys in rotation.** Rejected. Almost certainly against Alchemy's terms of service; not a portfolio-appropriate hack.

**Shrink V1 scope to 30d.** Not adopted here. The 90d timeframe is LOCKED in CLAUDE.md Section 2. Still an escape hatch if backfill actually blocks V1 delivery.

## Consequences

**What we gain:**
- Clear-eyed understanding of the free-tier rate model. Future fetchers (AW_02 ERC-20 and beyond) can be designed with CUPS in mind from the start rather than discovering it live.
- Repository stays simple. No dead-end batching code, no elaborate backoff/retry logic supporting a workaround that doesn't work.
- ADR 0006's cost figures are now grounded — they're not something batching will fix on the free tier.
- Portfolio signal: hypothesis tested against real infrastructure, invalidated cleanly, decision documented rather than the code force-fit around a wrong assumption.

**What we lose / take on:**
- Backfill wall-clock stays at the "+ tx_from" column of ADR 0006. Base 90d ~4-5 days per pool is the free-tier reality.
- The Growth-tier decision is deferred, not removed. It will need to be revisited before real scheduled backfill runs land.
- Future patterns that concentrate CU consumption need the same CUPS check applied here. Worth mentioning explicitly in any new fetcher's design review.
