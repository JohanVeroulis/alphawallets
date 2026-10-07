# 0015 — Per-swap executed price, from swaps as an event source

- **Status:** Accepted
- **Date:** 2026-10-07

## Context

PR #42's dry-run against the live cache quantified a failure the design had treated as a rounding concern:

```
rows with realizations: 1,016
of those, pnl == 0.00:    664  (65.4%)
of those, pnl != 0.00:    352
```

**Exactly** zero, not approximately. The mechanism is deterministic. Realizations are priced from `token_price`, which is an hourly grid, so a buy and a sell inside the same hour resolve to the *identical* unit price and `(sale − cost)` is precisely 0. Intra-hour round trips — arbitrage, MEV, routine router splitting — are most of on-chain trading.

These are not small errors. For scalpers, searchers and arb bots they are **complete signal loss**, and that is the trader category this project exists to identify. A leaderboard that ranks "consistently profitable wallets" while reporting $0 for two thirds of all realizations is not measuring what it claims to.

[ADR 0014](0014-pool-destination-realization.md) considered reading `uniswap_v3_swap` for executed prices and rejected it, describing the accuracy gain over hourly pricing as "modest" and the orchestration cost as large. **The dry-run disproved the first half of that premise.** This ADR reverses that rejection.

## Decision

**`uniswap_v3_swap` becomes a second event source for the PnL calculator**, alongside `erc20_transfer`. A swap is priced from what it actually executed at, not from the hour it happened in.

### The swap row is self-sufficient

No join is needed, because every field the calculator wants is already on the swap row:

| Field | Role |
|---|---|
| `tx_from` | The trader EOA — the partition's wallet (PR #15's enrichment) |
| `amount0`, `amount1` | The **executed** amounts, signed from the pool's perspective |
| `pool_address` | Resolves the token layout and which amount is the tracked token |
| `block_timestamp` | Event ordering and the window the realization falls in |
| `sqrt_price_x96` | Executed price, available for cross-checking |

Direction comes from the sign of the tracked token's amount, exactly as `queries._swap_direction` already derives it: positive means the token went into the pool (a sale), negative means it came out (an acquisition).

### Transfer legs are deduplicated

A V3 swap emits two ERC-20 `Transfer` events, and AW_02 reads both. Once swaps are an event source, those legs must be **excluded** from the transfer stream or the same trade is counted twice — once at its executed price and once at the hourly grid price.

The dedup rule is **"exclude every `(tx_hash, token_address)` pair that appears in `uniswap_v3_swap` from the transfer event stream."** Not `(tx_hash, token_address, log_index)`, because transfers carry no log index — see the next section.

**This is the implementation PR's hardest step**, and it is coarser than it should be. Because the key is `(tx_hash, token_address)` rather than per-log, a transaction that *both* swaps and plainly transfers the same token — possible through a multicall contract that routes a trade and pays a fee in the same token in one tx — would have its plain transfer dropped along with the swap legs. Rare, but real. The implementation PR must have a test for exactly that shape, asserting the known behaviour rather than discovering it later, and the row should carry a flag if the count of excluded transfer legs for a tx exceeds what the swap legs account for.

### Pricing the realization

The tracked token's USD price at execution comes from the swap's own ratio combined with the *other* side's hourly price:

```
executed_ratio   = |other_amount| / |tracked_amount|        (decimal-adjusted)
tracked_usd      = executed_ratio × hourly_price(other_token)
```

Every V1 pool is TOKEN/WETH (PR #28), and WETH is grid-reliable, so the other side is always priced. This recovers the intra-hour movement that the grid collapses: two trades in one hour against the same WETH price produce different `executed_ratio` values and therefore different PnL.

Where neither side is priced on the hourly grid — not the case for any V1 pair today — the calculator falls back to the grid price for the tracked token and sets `has_unpriceable_executed_price` on the row. V1 scope; the fallback exists so an unforeseen pool cannot abort a run.

### Two bonuses that make this cheaper than expected

1. **The executed quantity comes from `amount0`/`amount1` directly**, as a signed integer in base units. No conversion from a transfer's `value_raw`, and no reliance on a transfer row existing at all.
2. **`sqrtPriceX96` decimal math is off the critical path.** Deriving price from the amount ratio needs only the two token decimals the pool layout already carries. `sqrt_price_x96` remains useful for sanity-checking the ratio-derived price, which is a far safer place for error-prone math than the number that lands in the output.

## Why the originally considered join is impossible

The first version of this decision was to join each pool-involved transfer to its swap on `(chain, tx_hash, log_index ± 1)`, since a swap and its transfer legs share a transaction and sit at adjacent log indices on chain. **That join cannot be expressed against our data.** Recorded here so a future reader does not retry it.

`erc20_transfer.log_index` is **NULL for every row**:

```
erc20_transfer: 242,196 rows, 0 with a non-NULL log_index (0.0%)
```

This is [ADR 0007](0007-alchemy-cups-constraint.md)'s finding in force: Alchemy's Transfers API omits `logIndex` for the ERC-20 category, which is why `erc20_transfer`'s primary key is `(chain, unique_id)` rather than `(chain, tx_hash, log_index)` in the first place. The adjacency the join depends on is therefore not merely unreliable, it is **not computable** — measured across every matched swap/transfer pair, the `|log_index delta|` distribution is empty.

Falling back to `tx_hash` alone does not work either:

- **A transaction can hold several swaps.** Measured on the live cache: 1,333 transactions with one swap, 19 with two, four with three, one with five, one with seven. Without a log index there is no way to say which swap produced which transfer leg.
- **Overlap is incidental.** Only 68 of 1,358 swap transactions appear in `erc20_transfer` at all, because AW_01 fetches per pool and AW_02 per token, so the two tables cover different slices by construction.

Reading the swap row directly avoids all of this. The dead end is what made the simpler decision visible.

## Alternatives Considered

**Keep hourly grid prices (the status quo).** Rejected on data, not on taste: 65.4% of realizations book exactly $0, which is total signal loss for the trader category the project is built to find. This is the alternative the measurement eliminated.

**Fetch per-second prices from another provider.** Rejected on cost. Sub-hourly historical price APIs are paid tiers, which defeats the free-tier design goal that ADR 0003 and ADR 0008 were both chosen to preserve. It would also add a second provider's coverage gaps on top of the ones ADR 0010 already handles.

**Interpolate linearly between hourly points.** Rejected on arithmetic. Interpolation averages the information away instead of recovering it: a scalper's $0.02 edge inside an hour disappears into the hour's trend, and the result would be indistinguishable in the table from a measured price. ADR 0012 already rejected interpolation for the same reason — it adds a lie layer, and this project has consistently chosen to make absence visible rather than fill it plausibly.

## Consequences

**What we gain:**
- Intra-hour trading PnL becomes visible and quantitatively correct, which is the difference between a leaderboard that ranks trading skill and one that ranks noise for two thirds of its input.
- Executed amounts as well as executed prices, so a realization reflects what the trade actually moved rather than what a transfer row reported.
- Swap coverage no longer depends on a transfer row existing. A wallet whose swaps AW_02 never indexed still gets its trades priced.

**What we lose / take on:**
- **A second event source in the calculator.** Two streams must be merged into one chronological order before reaching the FIFO engine, whose no-sort contract (PR #33) makes that ordering load-bearing. `uniswap_v3_swap.log_index` is non-nullable, so the swap stream's own ordering is simpler than the transfer stream's — but the merge is new surface.
- **Deduplication is coarse**, per the Decision section. The multicall case is a known, tested, accepted limitation rather than a surprise.
- **The pool set becomes load-bearing twice.** ADR 0014 already made it accounting-relevant configuration; now a pool absent from AW_01's config means both no realization *and* no executed price. The fee-tier gap ADR 0014 recorded is correspondingly more costly, which strengthens the case for the factory enumeration it deferred.
- **Decimal handling in the ratio math is error-prone.** Two tokens, two decimal values, and an inversion that depends on which slot the tracked token occupies. The implementation PR must include tests with **hand-calculated reference values taken from a known swap on a block explorer** — not values generated by the same code under test, which would only prove self-consistency.

## References

- **PR #42** (`feat/pnl-writer-and-cli`) — the dry-run that quantified the 65.4% signal loss and triggered this ADR.
- [ADR 0014](0014-pool-destination-realization.md) — this ADR reverses its **rejected alternative #2** ("read `uniswap_v3_swap` as a second event source"), and nothing else in it. Its pool-destination policy and `KNOWN_POOLS` set stand unchanged.
- [ADR 0012](0012-pnl-methodology.md) — **no scope decision is reversed here.** Its ten decisions cover cost basis, classification and scope; reading transfers only was never one of them, it was an implementation choice in `pipeline/pnl/` flagged as deferred in PRs #37 and #41. This ADR adds a source to that implementation. Decision 1 is FIFO cost basis and is untouched.
- [ADR 0007](0007-alchemy-cups-constraint.md) — the Transfers API's absent `logIndex`, which is why the join in "Why the originally considered join is impossible" cannot be built.
- [ADR 0010](0010-defillama-historical-fallback.md) — the hourly grid this decision stops depending on for swap pricing, still the source for transfer-in cost basis.
- **PR #28** — the pool configuration and the factory sweep whose deferred fee-tier expansion this decision makes more valuable.
- Uniswap V3 whitepaper §6.1 — `sqrtPriceX96` definition, for the cross-check path.
