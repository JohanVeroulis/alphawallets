# 0016 — Contract-mediated trading is flagged, not ranked

- **Status:** Accepted
- **Date:** 2026-10-09

## Context

[ADR 0012](0012-pnl-methodology.md)'s step 6 — the hand spot-check against a block explorer — was run on 2026-10-09 against candidate A, a clean row: 9 realizations, zero caveat flags, $544.27 realized trading PnL, the top of the eligible leaderboard.

**Etherscan reports zero UNI history and a zero UNI balance for that wallet.**

The cache is not wrong. Both sampled transactions exist on chain with `status = 1`, `tx.from` is byte-exactly the attributed address, `token_address` is byte-exactly UNI, and the decoded amounts match the swap rows to the wei. The address spellings carry no case drift. What the receipts show is where the tokens actually went:

```
tx 0xd6cc4dbb…  pool 0x1d42064f… ──2,435.001260 UNI──> 0xBdb3ba9f…
tx 0x6f5ff480…  0xBdb3ba9f… ──1,323.074500 UNI──> pool 0x1d42064f…

tx.to = 0xBdb3ba9ffe392549E1f8658DD2630c141fDF47B6  (contract, 11,703 bytes)
```

The UNI never touches the signing EOA. It moves between the pool and a contract.

The root cause is one line of [ADR 0015](0015-per-swap-executed-price.md). Its field table asserts `tx_from` is "The trader EOA — the partition's wallet", which conflates the transaction **signer** with the token **holder**. For contract-routed trading — arb bots, MEV searchers, aggregator executors, any proxied bot architecture — those are different addresses, and the inventory lives in the contract.

### Measured scale

| | value |
|---|---|
| swap-only partitions (zero `erc20_transfer` rows, ≥1 `uniswap_v3_swap` row with `tx_from` = wallet) | **1,760 of 18,939 (9.3%)** |
| realizations inside them (30d) | **3,642 of 9,724 (37.5%)** |
| signer EOAs that never hold a token anywhere in the cache | **933 of 1,327 (70.3%)** |
| top 10 of the **eligible** 30d leaderboard that are swap-only | **10 of 10** |

The same economic activity is **double-counted under two addresses**. The signer EOA and the custody contract both carry partitions built from the same transaction hashes:

| address | role | 30d trading PnL | `balance_token` |
|---|---|---|---|
| `0x7bf30399db…` | signer | $544.27 | 20,831.96 UNI — **phantom**, Etherscan says 0 |
| `0xbdb3ba9ffe…` | custody contract | $0.00 | 12,684.88 UNI |

They share 10 transaction hashes, and the contract is independently present in the cache with 7,175 transfer rows and 22 `wallet_pnl` rows.

ADR 0012 decision 7 reserved `has_smart_wallet_signal` for Safe and ERC-4337 accounts and deferred it to V1.5. The structural shape here is the same — a mismatch between who signs and who holds — but unlike the Safe case the trigger is **observable from data we already have**. The flag has never fired on a single one of the 37,214 rows written.

## Decision

**A partition is flagged `has_smart_wallet_signal = True` when it is swap-only**: zero rows in `erc20_transfer` for that `(chain, wallet, token_address)`, and at least one row in `uniswap_v3_swap` where `tx_from` is that wallet.

A wallet that trades a token but has never once sent or received it is not the token's holder. The absence of any transfer row is a stronger and cheaper signal than inspecting counterparties, because it does not depend on having fetched the counterparty's own history.

**Flagged rows are excluded from leaderboard rankings**, by the same mechanism that already excludes `has_pre_window_activity` (ADR 0012 decision 5). They stay in `wallet_pnl` for transparency and for the V1.5 attribution work.

**The internal arithmetic is unchanged.** FIFO on the signer EOA still reconciles to the cent, as the spot-check confirmed. The figure is correct; its *subject* is wrong. This ADR changes who gets ranked, not how anything is computed.

## Alternatives Considered

**Attribute to the custody address.** Correct in principle, and the eventual answer. It needs the transfer legs inside each transaction to identify the holder — but ADR 0015's dedup drops them on `(tx_hash, token_address)`, and AW_02 rarely holds a bot contract's history in the first place. That is a new reader, new dedup semantics and a refetch strategy: multi-week work, deferred to V1.5.

**Drop swap-only partitions.** Safe and simple, and rejected on posture. It discards 37.5% of realizations and the entire top of the board while leaving no trace that a whole class of trading was removed. This project has consistently chosen to make absence visible rather than quietly tidy it away — the four-state price classification, the three caveat flags, ADR 0014's residual gap stated plainly. A silent drop here would be the opposite.

**Infer custody from the swap's recipient.** `uniswap_v3_swap` carries the output destination, which for contract-mediated trading is usually the custody contract. But for a direct EOA swap it is the EOA itself, and the two cases cannot be told apart without a code-at-address check per candidate. Rejected as insufficiently reliable when a stronger signal is already available for free.

## Consequences

**What we gain:**
- The leaderboard stops ranking addresses that provably do not hold the positions it credits them with. This is the difference between ranking traders and ranking transaction signers.
- `has_smart_wallet_signal` becomes a meaningful column instead of a dormant one, with a clear reading: *the signing EOA is probably not the position holder; the number is right for the signer, not necessarily for the trader.*
- The detection costs one query against data already in the cache. No fetch, no provider call, no new table.

**What we lose / take on:**
- **The leaderboard shrinks hard.** Measured on the 2026-10-08 run, the eligible profitable 30d rows fall from **90 to 32**, and the top entry from **$544.27 to $67.90**. Under CLAUDE.md's provisional "consistently profitable" bar of ≥10 trades, **2 wallets survive**. V1 needs a much wider wallet universe before the leaderboard is meaningful, and that is now a blocking dependency rather than a nice-to-have.
- **Double-counting is not resolved here.** The custody contract keeps its own partition, so the same activity still appears twice under two addresses — once flagged, once not. Reconciling them is V1.5 work, tracked in CLAUDE.md §9.
- **Some of the 2026-10-08 headline is misattributed.** The Ethereum zero-share collapse (51.3% → 5.7%) is real and holds. The dollar comparisons that rested on swap-only partitions — including $476.17 → $544.27, both of which are swap-only rows — do not, and should be read with this caveat rather than as a measured improvement.
- **ADR 0015's field table is wrong and stays wrong until the implementation PR.** `tx_from` is "the transaction signer, which may or may not be the token holder." Correcting it is part of the follow-up, not this ADR.
- **Internal consistency checks were proven insufficient on their own.** The $5,376 spot-check verified that `bought − sold − inventory = −realized_pnl` closed to the cent and concluded the figure was legitimate. That test cannot fail on a wrong subject: the arithmetic is self-consistent precisely because it is internally derived. External validation against an independent oracle is load-bearing, not confirmatory, and belongs in every future ADR that touches attribution.

## References

- **Hand spot-check, 2026-10-09** — the Etherscan verification of candidate A that surfaced this, and the `eth_getTransactionReceipt` calls that located the custody contract.
- [ADR 0012](0012-pnl-methodology.md) — decision 7 reserved the `has_smart_wallet_signal` flag this ADR activates; decision 5 is the exclusion mechanism it reuses. Step 6 of its implementation plan is what found the bug, which is the step working as designed.
- [ADR 0015](0015-per-swap-executed-price.md) — the `tx_from` attribution assumption this ADR corrects, and the dedup key that stands in the way of the V1.5 fix.
- [ADR 0014](0014-pool-destination-realization.md) — the pool set whose coverage gap the same spot-check also quantified.
- CLAUDE.md §9 — the double-counting item follow-up work must resolve.
