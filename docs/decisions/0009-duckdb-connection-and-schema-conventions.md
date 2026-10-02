# 0009 — DuckDB connection and schema conventions

- **Status:** Accepted
- **Date:** 2026-10-02

## Context

The three-way integration proof (PR #22) was the first time all three Week 1 fetcher schemas — `uniswap_v3_swap`, `erc20_transfer` and `token_price` — were joined under real conditions rather than written in isolation. Three silent-correctness classes surfaced, none of which any unit test had caught, and none of which would have announced itself in development:

1. **`date_trunc('hour', ts)` on a `TIMESTAMPTZ` truncates in the session timezone**, which DuckDB defaults to the host locale. The PnL join is `date_trunc('hour', block_timestamp) = token_price.ts` and `token_price.ts` is always hour-aligned UTC. On Europe/Athens (UTC+3, a whole-hour offset) truncation coincides with UTC truncation and the join works. On Asia/Kolkata (UTC+5:30) a 14:53:11Z event truncates to 14:30Z — not an hour boundary, so it matches no price row. The query returns zero priced events and raises nothing.

2. **`CREATE TABLE IF NOT EXISTS` is a no-op against an existing table.** `uniswap_v3_swap` gained a `tx_from` column in PR #15; the local cache file predated it and was never migrated. AW_01 had therefore been broken against that file since #15 merged, and the only symptom was a write failing with `table uniswap_v3_swap has 13 columns but 14 values were supplied` — an error that names neither the column nor the cause.

3. **Classifying unpriced events against the wall clock misread the price provider's publishing lag as a backfill defect.** The initial design called an unpriced event "pending" if it fell in the current hour and "missing" otherwise. Measurement disproved the premise: AW_03 ran at 06:30Z and its newest published point was already 05:00Z, so DefiLlama runs roughly **two hours** behind the chain head, not under one. At 07:08Z that rule labelled 40 events as a backfill hole and advised re-running AW_03 — advice that could not have helped, because the data did not yet exist upstream.

The common thread is that the data layer was permissive where it should have been strict. Each failure depended on something outside the code's control — the operator's locale, the age of a cache file, how long ago a fetcher last ran — and each produced a plausible-looking result rather than an error. These are not three unrelated bugs; they are one principle applied in three places, which is why they are recorded as one ADR rather than fragmented across three.

## Decision

Three conventions, enforced in the data layer rather than left to operator discipline.

### 1. Every DuckDB session is pinned to UTC

`db.connect()` executes `SET TimeZone='UTC'` on every connection. `SESSION_TIMEZONE = "UTC"` is a module constant and is **not configurable** — the stored instants are UTC, the price grid is UTC, and an operator's locale must not be able to change what a query means. A "configurable for debugging" switch would eventually be set in something that matters.

Pinning in `connect()` rather than in each pipeline stage means no stage has to remember.

### 2. Every writer's `create_tables()` guards against schema drift

After its `CREATE TABLE IF NOT EXISTS` statements, each writer calls `assert_table_matches_ddl(conn, table_name, EXPECTED_COLUMNS)`. On a mismatch it raises `SchemaDriftError` naming missing columns, extra columns and type mismatches **together** rather than first-one-wins, with the remedy in the message.

Concrete rules:

1. `EXPECTED_COLUMNS` is hand-written immediately beside the DDL constant it describes. Deliberately not parsed out of the DDL: a parser would agree with a typo in both sources, whereas a reviewer changing one and not the other is exactly the drift being caught. A per-writer test asserts the two agree on a freshly created table.
2. Scope is **column names and types**. Nullability, defaults, primary key and column order are out of scope — each needs its own comparison, all are V1-stable, and none has yet caused a failure.
3. The guard **does not migrate**, and its error message says so explicitly. Migration is a decision, not a side effect of calling `create_tables()`.

Applied to all five tables: `raw_uniswap_v3_swap`, `uniswap_v3_swap`, `raw_erc20_transfer`, `erc20_transfer`, `token_price`.

### 3. Price coverage is classified against the grid head, not the clock

Events are classified against `MAX(token_price.ts)` for their `(chain, token_address)` — the price grid's head:

| Status | Condition | Meaning |
|---|---|---|
| `priced` | A price row exists for the event's hour | Normal |
| `pending` | The event's hour is **above** the grid head | The backfill has not reached it. Resolves on the next AW_03 run. Not a defect |
| `unavailable` | The hour is **at or below** the grid head and still has no row | A real hole inside the range we believe we cover |

When the price table holds nothing for that `(chain, token)`, every event is `pending` with a message naming AW_03, and nothing raises — nothing can be a hole when there is no covered range to have a hole in.

Coverage percentages count **covered-range events only** (`priced + unavailable`). Including `pending` would report a permanent provider characteristic as falling coverage on every run, forever.

## Alternatives Considered

**Making the session timezone configurable, defaulting to UTC.** Rejected. The only reason to change it is to inspect timestamps in local time, which a formatter can do at the point of display without touching what a `date_trunc` means. A configurable correctness invariant is not an invariant.

**Auto-migrating on drift — `ALTER TABLE ADD COLUMN` for anything missing.** Rejected, and the case that triggered this ADR shows why. `tx_from` is declared `NOT NULL`, existing rows have no value for it, and the value cannot be reconstructed without re-fetching transaction receipts. An automatic migration would have had to invent a default, silently producing a table that satisfies the schema while carrying rows that are wrong in exactly the column PR #15 existed to add. Dropping and re-fetching was the correct remedy, and it is a judgement a human should make.

**Parsing `EXPECTED_COLUMNS` out of the DDL string.** Rejected. It would eliminate the duplication, but the duplication is the mechanism: two independent statements of the same shape disagree when one is edited carelessly. A parser derives both from one source and so can never disagree with it.

**Keeping wall-clock classification and widening the "current" window to two hours.** Rejected. It encodes a measurement of one provider's behaviour as a constant, which is wrong the moment DefiLlama changes its cadence or a second source is added, and it still makes the result depend on how long ago the backfill ran. The grid head is the same information, read from the data instead of assumed.

**Three separate ADRs.** Rejected. Each decision is individually small and would read as incidental; together they state a principle about where correctness invariants belong, which is the part worth preserving.

## Consequences

**What we gain:**
- Three classes of silent failure now either fail loudly or cannot occur. A future reader meets them as an error message naming the problem and the remedy, rather than as a day of debugging a result that looked reasonable.
- Locale independence. A laptop in Athens, a laptop in Kolkata, a CI runner in UTC and GitHub Actions all produce identical join behaviour.
- Determinism in tests. The grid-head rule removed an injected clock from the price classification entirely: tests pass an explicit `price_grid_head` and get the same answer whenever they run. The UTC pin is verified under a `TZ=Asia/Kolkata` fixture, including a control asserting that an *unpinned* connection is not UTC on that host — without which the pin tests would pass vacuously on a UTC machine and prove nothing.
- `unavailable` became a signal worth alerting on. It now fires only on a genuine hole inside covered range, never during normal provider lag, so it can be wired to an alert without a tuning exercise.
- A stale cache surfaces at `create_tables()` — at startup, before any fetching — instead of part-way through a run.

**What we lose / take on:**
- `EXPECTED_COLUMNS` is maintained by hand in five places. That is the intended cost, but it is a cost: adding a column means editing two adjacent blocks, and forgetting the second one fails the writer's own test rather than production.
- Drift detection is partial. Nullability, default and primary-key changes still pass the guard silently. Deferred deliberately until one of them bites; `PRAGMA table_info` already returns `notnull` and `pk`, so extending it is cheap when that happens.
- Migrations remain manual. There is no migration tooling and no migration history, so "drop and re-fetch" is the standing remedy. Acceptable while every table is a reproducible cache of upstream data; it stops being acceptable the moment a table holds anything derived that cannot be regenerated.
- The grid-head rule is per `(chain, token)`, so a token whose backfill is far behind reports most of its events as `pending` and its coverage percentage over a small denominator. Honest, but a caller reading only the percentage can miss that it covers very little — which is why the summary prints the grid head and both hour lists alongside it.

## References

- **PR #22** (`feat/exploration-wallet-activity-proof`) — origin of all three findings. Squash-merged to `main` as `4b1c2e0`; the branch's individual commits (`6c128d9` UTC pin, `82d06c2` drift guard, `e9a71cc` grid-head classification) exist only in that PR's history, not on `main`.
- [ADR 0004](0004-duckdb-cache.md) — why DuckDB is the cache layer these conventions apply to.
- [ADR 0008](0008-defillama-historical-prices.md) — DefiLlama as the price source whose publishing lag the third decision accommodates.
- Implementation: `src/alphawallets/db.py` (`SESSION_TIMEZONE`, `SchemaDriftError`, `assert_table_matches_ddl`), `src/alphawallets/pipeline/exploration/models.py` (`classify_price_status`), `src/alphawallets/pipeline/exploration/queries.py` (`query_price_grid_head`).
- Tests pinning each decision:
  - `tests/test_db.py::TestSessionTimezone` — the UTC pin, including `test_unpinned_connection_would_not_be_utc` as the anti-vacuity control.
  - `tests/test_db.py::TestAssertTableMatchesDdl` — missing, extra and mismatched columns, all three reported together, and `test_does_not_alter_the_table`.
  - `tests/fetchers/uniswap_v3/test_writer.py::TestCreateTables::test_pre_tx_from_table_raises_schema_drift` — the real pre-PR#15 shape, end-to-end through `create_tables()`.
  - `tests/pipeline/exploration/test_wallet_activity_proof.py::TestClassifyPriceStatus` — the grid-head boundary, including the empty-price-table case.
  - `tests/pipeline/exploration/test_wallet_activity_proof.py::TestSummarise::test_grid_head_advance_prices_the_pending_event` — inserts the missing price row and asserts a `pending` event becomes `priced`, simulating the AW_03 re-run that resolves it.
- `src/alphawallets/pipeline/exploration/README.md` — operational description of the three-state classification and the exit-code contract.
