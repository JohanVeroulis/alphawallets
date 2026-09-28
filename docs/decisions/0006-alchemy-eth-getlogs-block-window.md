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

Implications for V1 backfill scope. Wall-clock has been measured three times as the pipeline gained more per-window work; the picture that plans should use is the rightmost column:

| Scope                       | Blocks    | Windows | Fetch only ¹ | + Persist ² | + tx_from ³ |
| --------------------------- | --------- | ------- | ------------ | ----------- | ----------- |
| Ethereum, 30 days, 1 pool   | 216,000   | 21,600  | ~0.5 h       | ~1.0 h      | ~5-7 h      |
| Ethereum, 90 days, 1 pool   | 648,000   | 64,800  | ~1.4 h       | ~2.7 h      | ~15-20 h    |
| Base, 30 days, 1 pool       | 1,296,000 | 129,600 | ~2.8 h       | ~5.4 h      | ~28-35 h    |
| Base, 90 days, 1 pool       | 3,900,000 | 390,000 | ~8.4 h       | ~17 h       | ~4-5 days   |

¹ Measured 2026-09-26: warm eth_getLogs only, 0.078s per window on both chains.
² Measured 2026-09-27, PR #12: fetch + per-window DuckDB write, 0.151s per window on Ethereum.
³ Measured 2026-09-28, PR #13 (#15): full pipeline with tx_from enrichment via one eth_getTransactionByHash per unique tx. ~0.9s per window with ~13 swaps/window on Ethereum. Extrapolations assume similar swap density on Base.

The tx_from step's cost dominates. It surfaced from a data-model gap (Swap.sender is the router, not the trader — see PR #13) that couldn't be avoided. JSON-RPC batching of eth_getTransactionByHash was investigated as a mitigation and abandoned — see [ADR 0007](0007-alchemy-cups-constraint.md) for why the free-tier compute-units-per-second cap makes batching counter-productive on this plan.

These are per pool. V1 tracks ~4 major pairs on each chain, and ERC-20 fetchers add more; total backfill is many days on the free tier. Once caught up, incremental fetching (new blocks only) stays fast — the pain is one-time. Restoring practical backfill wall-clock requires either the Alchemy Growth tier or a scope decision on the 90d window (see ADR 0007).

## Decision

For V1:

- Set BLOCK_WINDOW_SIZE = 10 in every eth_getLogs-based fetcher.
- Accept that initial backfill runs are the pipeline's dominant cost, and that they are re-measured as the pipeline evolves. The rightmost column of the Context table is the current planning figure.
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
- Initial backfill for a single pool on Base at 90d is ~4-5 days of wall-clock with the current pipeline (measured 2026-09-28, PR #13). Batching was investigated and rejected — [ADR 0007](0007-alchemy-cups-constraint.md) explains why the free-tier CUPS cap makes batching counter-productive. Restoring practical backfill requires either the Alchemy Growth tier or a scope decision on the 90d window.
- Fetchers must handle long-running runs gracefully: resumable state, progress logging, and idempotent writes to DuckDB (won't re-insert duplicates on restart).
- Any tutorial or example that assumes larger eth_getLogs windows won't apply here — a small friction on future development.
