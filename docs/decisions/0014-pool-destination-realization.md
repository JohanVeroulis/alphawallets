# 0014 — Transfer-OUT to a Uniswap V3 pool is a realization

- **Status:** Accepted
- **Date:** 2026-10-07

> **Numbering note:** 0013 is reserved for the provider grid-granularity decision proposed in PR #31 and referenced from CLAUDE.md §9. That ADR is not yet written; the gap is deliberate, not a missing file.

## Context

[ADR 0012](0012-pnl-methodology.md) decision 3 says a transfer-OUT reduces the FIFO stack without realizing anything. The reasoning holds for the case it was written about: we cannot know whether a wallet sold on a CEX, paid a counterparty, bridged, or moved tokens to its own cold storage, so inventing a sale price would be a fabrication.

That reasoning does not hold when the counterparty is a Uniswap V3 pool.

A V3 swap emits **two** ERC-20 `Transfer` events: the input token moves from the wallet to the pool, and the output token moves from the pool to the wallet. AW_02 reads both. So under decision 3 as written, the input leg — a sale, at a price we already know — reduces the stack and realizes nothing.

The consequence is not a rounding detail. **It discards every unit of realized trading PnL from on-chain swaps**, which is the primary signal the leaderboard exists to rank on and the one ADR 0012 calls "the skill signal". A wallet that bought UNI at $8 and sold it at $12 through a pool would show a shrinking balance and zero realized PnL.

And the price is knowable without inference: it is the market price of the outgoing token at the swap's timestamp, which `token_price` already holds on the hourly grid the PnL join uses.

This was found while wiring the calculator in PR #37, where the transfer-OUT path had to pass a sentinel sale price to `FIFOEngine.consume()` and discard the resulting Realizations — a call site whose own comment noted that the discarded value was the thing decision 3 required.

## Decision

**A transfer-OUT whose destination is a known Uniswap V3 pool is treated as a realization.** `FIFOEngine.consume()` is called with `unit_sale_usd` set to the outgoing token's price at the event timestamp, and the returned Realizations are **kept** rather than discarded.

Every other transfer-OUT keeps decision 3's treatment unchanged: stack reduced, nothing realized, cost basis lost.

This narrows decision 3 rather than replacing it. The general rule stands — an unknown counterparty cannot imply a sale — and this carves out the case where the counterparty is known and its economic meaning is unambiguous.

### Pool identification

The known-pool set is resolved from the fetcher's own configuration: the union of `DEFAULT_ETHEREUM_POOLS` and `DEFAULT_BASE_POOLS` in `fetchers/uniswap_v3/aw_01_uniswap_v3_swaps.py`, which is 19 pools today (PR #28).

One hardcoded set, derived from config at import, not a per-run lookup. This is accounting infrastructure: the same input must produce the same PnL on every run, and a set that changes shape depending on what happens to be in the cache would make a recomputed figure differ for reasons unrelated to the data.

**Correction to the original proposal.** The plan for this ADR was to expand that set with every distinct `pool_address` observed in `uniswap_v3_swap`. Measured against the live cache, that step adds nothing and cannot:

```
configured pools:                    19
distinct pools in uniswap_v3_swap:   17
observed but not configured:          0
configured but never observed:        2
```

`uniswap_v3_swap` is populated *by* AW_01, which fetches per-pool from an address argument sourced from those same dicts. The swap table is therefore a subset of the configured set by construction — a closed loop that can never surface a pool we did not already know about. The union is implemented anyway, because it costs one query and makes the set self-healing if swaps ever arrive from another path, but it must not be presented as coverage.

### The residual gap, stated plainly

Because the pool set comes from AW_01's configuration, **a wallet trading in a V3 pool we do not track still falls under decision 3** and loses that realization. This is not hypothetical: PR #28's pool research found most V1 pairs have two or three fee tiers deployed with live liquidity, while the registry configures exactly one primary pool per `(token, chain)`. A wallet trading UNI/WETH at 1% on Ethereum, where we configure the 0.3% pool, is invisible to this decision.

The remedy is enumeration rather than observation: the V3 factory's `getPool(tokenA, tokenB, fee)` returns every deployed tier for a pair, and PR #28 already ran exactly that sweep. Adding the non-primary tiers to the known-pool set — for *identification* only, without fetching their swaps — would close most of the gap cheaply. Deferred to a follow-up so this ADR stays a policy decision rather than a pool-expansion exercise, and recorded here so the limitation is a known quantity rather than a surprise during the spot-check.

## Alternatives Considered

**Treat every transfer-OUT as a realization.** Rejected. It would price a CEX withdrawal as a sale, inflating PnL for the commonest wallet shape there is — the same failure mode ADR 0012 decision 2 rejects on the acquisition side, and for the same reason: a wallet moving tokens is not a wallet selling them.

**Read `uniswap_v3_swap` as a second event source.** Rejected for V1, though it is the more accurate answer. A swap row carries the actual executed amounts for both legs, so realized PnL would come from the trade itself rather than from the hourly price grid — no hour-truncation error, and the paired leg available for free. The cost is a second reader, a merge of two event streams into one chronological order, and deduplication against the transfer legs of the same swap, which is a large increase in orchestration for a modest accuracy gain over pricing the outgoing leg at its hour. Revisit in V1.5, particularly if the spot-check shows hour-grid error is material.

**Keep only the 19 configured pools, with no union and no expansion.** Rejected as the stated policy, though it is functionally what today's data yields. The union is kept because it is nearly free and removes a silent dependency on AW_01 being the only writer of swap rows; the fee-tier expansion is the part that would actually extend coverage, and it is deferred explicitly rather than implied.

## Consequences

**What we gain:**
- Uniswap V3 swaps produce correct realized PnL. For V1's tracked tokens this is the dominant on-chain trading venue, so this is the difference between a leaderboard that ranks trading skill and one that ranks nothing.
- The trading/airdrop split becomes meaningful on the realization side as well as the acquisition side: a Realization carries its lot's `source`, so selling an airdropped token through a pool realizes airdrop PnL and selling a bought one realizes trading PnL, which is exactly what ADR 0012 decision 4 wanted and could not previously observe.
- `FIFOEngine.consume()` stops being called with a sentinel price it never uses.

**What we lose / take on:**
- **The pool set is now accounting-relevant configuration.** Adding a pool to AW_01 changes PnL for any wallet that traded there, which means the two must be maintained together and a pool addition is no longer a purely additive data decision. Worth a note in the AW_01 pool docs.
- **Swaps in untracked pools still lose their realization**, per the residual gap above. A wallet's PnL can therefore be understated with no local signal that it happened — which argues for the fee-tier expansion sooner rather than later, and for the hand spot-check in ADR 0012 step 5 choosing at least one wallet that trades outside the primary tier.
- **Realizations are priced on the hourly grid**, not at the executed swap rate. Within an hour of real volatility the two differ, and MKR's 4-hour provider grid (PR #31) makes the error larger for that token specifically. The second rejected alternative above is the fix if it proves material.
- **A new `TransferTreatment` value** is needed in the classifier. This ADR defines the policy; the follow-up PR implements it, and that PR inherits the obligation to keep every non-pool OUT on decision 3's path.

## References

- [ADR 0012](0012-pnl-methodology.md) — decision 3, the general rule this narrows, and decisions 2 and 4 whose reasoning is reused above.
- **PR #37** (`feat/pnl-engine-wiring`) — where the conflict surfaced, in the transfer-OUT call site that had to discard a Realization it could not price.
- **PR #28** (`feat/aw-01-pool-expansion`) — the 19-pool configuration this set is resolved from, and the factory sweep that enumerated the non-primary fee tiers the residual gap refers to.
- `src/alphawallets/fetchers/uniswap_v3/aw_01_uniswap_v3_swaps.py` — `DEFAULT_ETHEREUM_POOLS`, `DEFAULT_BASE_POOLS`, `DEFAULT_POOLS_BY_CHAIN`.
