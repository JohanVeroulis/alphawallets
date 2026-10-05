# Pipeline exploration

One-shot sanity queries over the local cache. Not production pipeline stages: an exploration script answers a specific question once, loudly, so a design decision downstream rests on measured behaviour instead of an assumption. They are kept in the repo because the answer matters later, and because re-running one is how a regression gets caught.

Everything here **reads DuckDB only — never Alchemy** (CLAUDE.md §6). A stage works from the cache the fetchers fill, so it can be re-run freely and its cost is never a provider's rate limit.

| Script | Question it answers |
|---|---|
| `defillama_coverage.py` | Does DefiLlama price every V1 tracked token? (→ [ADR 0008](../../../../docs/decisions/0008-defillama-historical-prices.md)) |
| `wallet_activity_proof.py` | Do the AW_01, AW_02 and AW_03 schemas actually join? |

## wallet_activity_proof

The first pipeline-stage module in the repo, and the first place the three Week 1 schemas are joined rather than written in isolation. It shows one wallet's swaps and transfers of one token, priced from the hourly grid, as a chronological timeline.

It exists to fail *before* Week 3. PnL is a join across these three tables; if the addresses, the chain field or the timestamps disagree, PnL would return plausible numbers computed from a fraction of the rows. Better to find that here, on data small enough to read by eye.

```bash
uv run python -m alphawallets.pipeline.exploration.wallet_activity_proof
```

### What this proof validates

Each of these was an assumption until the proof ran. Three of them turned out to be wrong.

- **Address lowercasing is consistent across all three fetchers**, so joins match. Every query lowercases its inputs and compares against `lower(column)` rather than trusting the writers — the invariant is enforced where the join happens, not where the data is written.
- **The chain field means the same thing in all three tables**, so the chain filter is meaningful rather than decorative. The test fixture carries a Base row for the exact hour Ethereum lacks, so a dropped chain filter shows up as a *wrong* price rather than a missing one.
- **Timestamp alignment holds**: `date_trunc('hour', event_ts)` on the query side matches AW_03's hour-aligned writes. This only works because `db.connect()` pins the session timezone to UTC — DuckDB otherwise truncates in the host locale, and on a half-hour-offset machine a 14:53Z event truncates to 14:30Z and matches no price row at all. That would have reported **0% coverage with no error**.
- **`tx_from` is what makes swaps joinable to trader activity.** Added in PR #15 (closing issue #13). Without it a swap keys off `sender`/`recipient`, which for a router-mediated trade are the router and the pool — not the human.
- **Big-integer VARCHAR columns round-trip cleanly.** `amount0`, `amount1` and `value_raw` hold uint256-scale values as strings (a float would lose the low-order digits). They convert through `Decimal` to whole token units and render as readable amounts with no precision loss.
- **Semantic agreement, not just schema agreement.** The strongest result, from the live run:

  ```
  [2026-10-01 05:35Z]  SWAP      SELL           14.00 UNI  @ $8.94  = $125.14  (-> 0.05 WETH)
  [2026-10-01 05:35Z]  TRANSFER  OUT            14.00 UNI  @ $8.94  = $125.14
  ```

  These two rows are the same economic act seen from two sides: a Uniswap V3 `Swap` event decoded from `eth_getLogs` by AW_01, and an ERC-20 `Transfer` returned by the Alchemy Transfers API to AW_02. Two fetchers, two APIs, two schemas, two writers — agreeing on amount to the cent, on timestamp to the second, and both resolving to the same DefiLlama hour. A passing test suite proves the code does what it was told; this proves the data describes reality.

### Four-state price classification

Coverage is not priced-or-missing. "No price" has four distinct meanings and only one of them is a problem.

| Status | Condition | Resolves automatically? | Meaning |
|---|---|---|---|
| `priced` | A price row exists for the event's hour | — | Normal |
| `pending` | The event's hour is **above** the grid head for that `(chain, token)` | **Yes** — next AW_03 run | The backfill has not reached it. Not a bug |
| `unavailable` | The hour is **at or below** the grid head and still has no row | **No** — re-run AW_03 for those hours | A real hole inside the range we believe we cover. Worth alerting on |
| `unpriceable` | No configured price route serves the token at all | **No** — needs ADR 0010 | No price exists on any hour. Re-running AW_03 cannot help |

The grid head is `MAX(token_price.ts)` per `(chain, token)`.

`unpriceable` is checked **before** everything else, including before the price-row lookup: a listed token with a stray cached row is still a token the fetcher cannot cover, and the classification should describe what the fetcher can do rather than what happens to be in the cache.

**Why `unpriceable` is its own state rather than a flavour of `pending`.** A route-less token never acquires a grid head, so the grid-head rule below would label every one of its events `pending` forever — indistinguishable from a backfill that simply has not run. That erodes what `pending` means and leaves `unavailable` unable to separate a route gap from a genuine hole, which was the entire purpose of keying off the grid head. The source is `unpriceable.py` today, a static set seeded with MKR; it moves to the `token_price_route` table when [ADR 0010](../../../../docs/decisions/0010-defillama-historical-fallback.md) is implemented.

**Why data-driven rather than time-driven** (for the `pending`/`unavailable` split). The first version keyed `pending` to the current wall-clock hour. The live run disproved it: AW_03 ran at 06:30Z and its newest point was 05:00Z, so DefiLlama's publishing lag is around **two hours**, not one. At 07:08Z the wall-clock rule labelled 40 events in hour 06 as `unavailable` and advised re-running AW_03 — advice that could not have worked, because the data did not exist upstream yet.

Keyed off the grid's own head instead, the classification depends only on what the cache contains. Consecutive runs agree, the result is decoupled from how long ago AW_03 last ran, no injected clock is needed for deterministic tests, and `unavailable` becomes a signal worth acting on.

Coverage counts **covered-range events only** — `priced + unavailable`. `pending` and `unpriceable` are both legitimate skips: one has no price *yet*, the other will not have one until a new route ships. Counting either would report a provider characteristic as falling coverage on every run, forever. `is_fully_priced` turns on `unavailable` alone, so a real hole cannot hide behind a legitimate skip.

The output reflects the distinction: `unavailable` emits a **Warning** naming the hours and telling the operator to re-run AW_03, while `unpriceable` emits a **Note** saying re-running will not help and pointing at ADR 0010.

### Auto-pick strategy

With no `--wallet`, the proof picks a wallet itself. The ranking is **overlap-first** — wallets present in both tables, then by combined row count, then by address for determinism.

Not `ORDER BY COUNT(*)`, because swaps and transfers live in different address spaces. A swap keys on `tx_from`, the EOA that submitted the transaction. A transfer keys on `from_addr`/`to_addr`, the token-level participants — which for a router-mediated swap are the pool and the router. Measured on the live cache:

```
distinct UNI/WETH swappers (tx_from):  33
distinct transfer participants:       469
addresses in BOTH:                      8
```

Raw-count ranking therefore favours infrastructure — routers and pool managers touch an enormous number of transfers and submit no transactions of their own. The first live run picked one such contract: 238 transfers, **zero swaps**, so the three-way join the module exists to demonstrate never appeared in its own output. Overlap-first picks a real trader, even one with 59× less activity.

### Exit codes

- **0** — the proof ran, including when it reports findings. A non-zero `unavailable` count is a *data* finding: it prints a warning naming the affected hours and the remedy, and still exits 0.
- **1** — an operational failure: no cache, no candidate wallet for that `(chain, token)`, or a named wallet with no events. Each prints what was looked for and which backfill to run.

The strict "every covered-range event is priced" assertion belongs to the live proof, not to the CLI. A pipeline stage reporting the state of the data honestly is working correctly; whether that state is acceptable is the caller's judgement.

### Layering

```
models.py                   Event, TimelineSummary's inputs, price classification. Imports nothing local
queries.py                  Literal SQL + composition. Imports models
wallet_activity_proof.py    Orchestrator, output, CLI. Imports both
```

Same shape as the fetchers (`models` → `mapper`/`writer` → `aw_XX`). `models.py` exists because `queries` needs `Event` and the orchestrator needs `queries` — a cycle until the model layer moved to the bottom.

`POOL_TOKEN_LAYOUT` in `queries.py` records which token sits in which slot of each tracked pool, with decimals. The swap table stores `amount0`/`amount1` but not `token0`/`token1`, so the sign of an amount cannot be interpreted without it. Declared rather than fetched, because calling the pool contract would put an Alchemy call inside a pipeline stage. Every entry was verified live against `token0()`, `token1()`, `symbol()` and `decimals()`; a pool missing from the dict raises rather than defaulting a slot, since a guessed slot inverts every direction for that pool.
