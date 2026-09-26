# 0006 — Alchemy free-tier eth_getLogs is capped at 10 blocks per request

- **Status:** Accepted
- **Date:** 2026-09-26

## Context

During the first live fetch of Uniswap V3 Swap events (AW_01 fetcher, PR #12), an eth_getLogs call with a 500-block window returned HTTP 400 from Alchemy:

    {'code': -32600, 'message': 'Under the Free tier plan, you can make eth_getLogs
     requests with up to a 10 block range. ... Upgrade to PAYG for expanded block range.'}

Follow-up measurement confirmed the exact boundary on both V1 chains:

| Chain    | 10-block window        | 11-block window |
| -------- | ---------------------- | --------------- |
| Ethereum | OK — 70 logs, 0.20s    | Rejected        |
| Base     | OK — 105 logs, 0.27s   | Rejected        |

The block-window planning in ADR 0003 assumed generous eth_getLogs limits (the JSON-RPC spec allows up to 10,000 blocks in principle). The free-tier cap is 10, inclusive. This surfaced only when the fetcher went live — no Alchemy documentation page listed it prominently.

Implications for V1 backfill scope, measured on a warm connection (2026-09-26, USDC/WETH 0.05% pool, 100 sequential requests on Ethereum; independently confirmed on Base's USDC/WETH 0.05% pool at 0.077s/window):

| Scope                                | Blocks    | Requests @ 10/window | Wall-clock @ 0.079s/request |
| ------------------------------------ | --------- | -------------------- | --------------------------- |
| Ethereum, 30 days, 1 pool            | 216,000   | 21,600               | ~0.5 hours                  |
| Ethereum, 90 days, 1 pool            | 648,000   | 64,800               | ~1.4 hours                  |
| Base, 30 days, 1 pool                | 1,296,000 | 129,600              | ~2.8 hours                  |
| Base, 90 days, 1 pool                | 3,900,000 | 390,000              | ~8.4 hours                  |

Initial estimates used 0.2s/request based on a single cold call. Steady-state cost with connection reuse is 2.5x lower. Numbers assume no rate limiting; the free tier's requests-per-second cap may extend real backfill.

These are per pool. V1 tracks ~4 major pairs on each chain, and ERC-20 fetchers add more, so total backfill is roughly a day across all fetchers and both chains at worst. Once caught up, incremental fetching (new blocks only) is fast — the pain is one-time.

## Decision

For V1:

- Set BLOCK_WINDOW_SIZE = 10 in every eth_getLogs-based fetcher.
- Accept that initial backfill runs several hours per chain per fetcher — ~1.4h for Ethereum 90d, ~8.4h for Base 90d (measured 2026-09-26).
- Do not adopt Alchemy PAYG at this stage. Backfill runs are a one-off cost and V1 must ship pre-revenue (ADR 0003 constraint).
- Timeline impact: absorb inside the existing Week 1-2 window rather than extending the ROADMAP again. Backfill runs happen in the background while other pipeline work continues.

For V1.5 and later:

- Reassess Alchemy PAYG once the leaderboard is live and there's a reason to refresh data more aggressively.
- Investigate Alchemy's Transfers API (paged differently) for ERC-20 fetchers where it applies — it may bypass this limit for the specific case of transfer history.
- Investigate whether a free Base RPC (e.g. Base's public endpoint, Ankr, Blast) offers a larger eth_getLogs window for Base backfill, where the cost is largest.

## Alternatives Considered

**Adopt Alchemy PAYG for backfill only.** Rejected for V1. Even a short PAYG stint burns unbudgeted money and, more importantly, hides the constraint from the ROADMAP planning we'll do for V1.5. Better to design around the limit and know exactly what backfill costs on the free tier.

**Narrow V1 scope to 30 days only, dropping 90d.** Not adopted here. The 90d timeframe is in CLAUDE.md Section 2 (LOCKED). Reducing scope to work around infrastructure is a bigger change than living with slow backfill. Revisit only if backfill actually blocks progress in Week 2-3.

**Use a non-Alchemy RPC provider.** Deferred. Free public RPCs typically have their own eth_getLogs caps (often lower) or aggressive rate limits. Worth testing in V1.5 as a specific investigation rather than swapped in blindly now.

## Consequences

**What we gain:**
- Grounded understanding of the real backfill cost, replacing the ADR 0003 assumption.
- No infrastructure spend during V1.
- A concrete window size that all eth_getLogs-based fetchers can share as a constant.

**What we lose / take on:**
- Initial backfill for a single pool on Base at 90d is ~8.4 hours of wall-clock (measured). Full V1 backfill across all fetchers and both chains fits inside a day at a stretch, longer if rate limiting kicks in.
- Fetchers must handle long-running runs gracefully: resumable state, progress logging, and idempotent writes to DuckDB (won't re-insert duplicates on restart).
- Any tutorial or example that assumes larger eth_getLogs windows won't apply here — a small friction on future development.
