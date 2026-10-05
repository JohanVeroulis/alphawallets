# 0011 — Chain-aware backfill parallelization

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

Two findings from recent PRs reframed what the backfill performance problem actually is.

**PR #25** measured serial pagination in AW_02, where each request needs the previous response's `pageKey`: roughly **3.4s per page on Ethereum and 9.4s per page on Base**. It also found the cost badly concentrated — **57% of V1-scope AW_02 backfill runtime fell in 11% of pairs** (MORPHO/base and AAVE/base alone). The first framing of the follow-up was "optimise serial pagination".

**PR #28** corrected that framing. The AW_01 pool smoke tests showed Base is systematically more expensive to backfill than Ethereum on **two independent axes**:

| Axis | Fetcher | Cause | Factor |
|---|---|---|---|
| Request count | AW_01 | Base's ~2s block time against Ethereum's ~12s, divided by ADR 0006's 10-block `eth_getLogs` cap | ~6× |
| Request latency | AW_02 | Provider-side page latency measured in PR #25 | ~3× |

Combined, **a Base backfill is roughly 18× more costly than an equivalent-duration Ethereum backfill.** The same PR hit ADR 0007's CUPS ceiling doing it: a 3,600-block Base probe is 360 concentrated `eth_getLogs` calls and returned HTTP 429.

The two axes have different causes and therefore different fixes. One is request *count*, set by block time and a provider cap we cannot change. The other is per-request *latency* at the provider. Neither is "pagination is written inefficiently".

And pagination within a single `(token, chain)` pair **cannot** be parallelised. `pageKey` requires the previous response by construction, and `eth_getLogs` windows carry explicit block ranges that must be walked. The sequencing is a property of the data model, not of our code.

So the problem is not "paginate faster". It is **"run more `(token, chain)` pairs concurrently"**, under two existing constraints: ADR 0004's single-writer DuckDB file, and ADR 0007's per-endpoint CUPS ceiling.

## Decision

Five parts, in dependency order.

### 1. Per-chain DuckDB files

Split `data/cache.duckdb` into `cache_ethereum.duckdb` and `cache_base.duckdb`. Each chain's worker holds the write lock on its own file, so the two never contend. Pipeline stages that read across chains use DuckDB `ATTACH` — one file as the active connection, the other attached read-only.

Chosen because it is the simplest coordination that works: there is no shared state to get wrong, and ADR 0004's single-writer semantics are preserved exactly, per file, rather than worked around. It also fixes an observability problem as a side effect — PR #28 could not read progress mid-backfill because the writer held the lock, and with separate files an operator can query Ethereum while Base is being written.

The cost is one `ATTACH` statement in cross-chain queries.

### 2. Concurrent `(token, chain)` pair execution

Each `(token, chain)` pair runs in its own worker. Pagination **within** a pair stays strictly sequential — it must. The parallelization axis is across pairs, which is where the available concurrency actually is: V1 has 18 verified pairs and the slow ones are a small subset.

### 3. Per-chain CUPS budget

Ethereum and Base reach Alchemy on separate RPC endpoints, each with its own ceiling of roughly 300 CUPS. Allocate **250 CUPS per chain worker pool** as a safety buffer. Concurrent workers on the same chain share that budget through a token bucket; workers on different chains do not contend at all.

This is the part most likely to need adjustment against measurement rather than arithmetic, for the reason ADR 0007 records: the 429 that established the constraint carried no numbers in its body, so the figures here are working values and not provider-documented limits.

### 4. Chain-weighted concurrency allocation

Because Base is ~18× more expensive, worker allocation should favour it rather than split evenly. A starting point: **6 workers for the 6 Base pairs, 2 workers for the 12 Ethereum pairs.** Even allocation would leave Ethereum workers idle while Base remains the critical path.

An alternative worth considering once scheduled runs exist (Week 5+): run the two chains at **different cadences** — Base daily, Ethereum two to four times daily — which spends the same total budget while keeping the cheaper chain fresher.

### 5. Observability sidecar

Per-chain files remove cross-chain blocking, but a long Base backfill still holds Base's own lock, so progress for the chain being written is not readable from the DB. Emit structured progress events — per pair: pages seen, rows written, elapsed — to stdout and optionally a sidecar file. The Week 5 GitHub Actions work can ingest those for monitoring without touching the database at all.

This is not a nice-to-have. In PR #28 a 7-day Base backfill had to be killed blind because there was no way to tell how far it had got.

## Alternatives Considered

**Write queue plus a single DB** — an asyncio queue feeding one dedicated writer thread. Rejected. It adds shared-state coordination to solve a problem that per-chain files solve structurally, and it does not improve observability at all: the single writer still holds the single lock, so mid-run reads stay blocked.

**Shared DB with distributed locks**, e.g. Redis-backed advisory locks. Rejected. It contradicts ADR 0004's single-writer premise rather than working within it, adds an external service to a project whose cache is deliberately a single file, and the lock contention would reintroduce exactly the serialization this ADR exists to remove.

**Provider-side batch endpoints.** Rejected as a solution to this problem. Alchemy's batch RPC is within-chain only, so it cannot span `(token, chain)` pairs — it does not touch the slow axis. ADR 0007 also found that concentrating requests through batching is what triggers the CUPS ceiling, so batching here would make the rate-limit problem worse while leaving the cost asymmetry unchanged.

**Sequential execution with aggressive caching.** Rejected. Caching improves re-runs; the 18× Base cost is a **first-run** cost, and first runs are what the backfill is for. Re-run cost is already handled: `INSERT OR IGNORE` makes overlap free, and `--resume` means a scheduled job only ever fetches the delta.

**Parallelising pagination within a pair.** Rejected on data-model grounds rather than preference. `pageKey` is opaque and only obtainable from the previous response; `eth_getLogs` ranges must be walked in sequence to avoid gaps or duplicates. Attempting it means losing or double-counting rows.

## Consequences

**What we gain:**
- Backfill throughput scales with worker count, bounded by per-chain CUPS rather than by a single write lock. The ceiling moves from "one writer" to "what the provider allows", which is where it should be.
- Observability during long backfills. An operator can query Ethereum data while Base is mid-write, which was impossible in PR #28.
- Independent failure domains per chain. A Base rate-limit incident no longer stalls Ethereum ingestion — the two share no lock and no budget.
- A clean path to scheduled runs. GitHub Actions workflows split per chain, each with its own cadence and timeout budget, which also makes a Base timeout a Base-only failure.

**What we lose / take on:**
- Cross-chain pipeline queries need one `ATTACH`. A one-time syntactic cost, but it means every cross-chain stage has a line that can be forgotten, and forgetting it produces a "table does not exist" error rather than a wrong answer — the right failure mode, at least.
- One cache file becomes two. Anything assuming a single path needs a per-chain abstraction: `get_cache_db_path()` gains a chain argument, and `ALPHAWALLETS_CACHE_DB` as a single-path override no longer expresses the production layout.
- Migration from the current single file: either rebuild from scratch, which is the pattern PR #22 already used for the schema-drift rebuild and is safe because every table is a reproducible cache of upstream data, or a one-time export/split script. The schema-drift guard from ADR 0009 will flag the new files as empty rather than drifted, so nothing silently half-migrates.
- Pipeline-stage tests need per-chain fixtures or the `ATTACH` pattern. The existing adversarial fixture deliberately mixes chains in one file to prove the chain filter works, and that test design has to survive the split — the filter still matters, because a single chain's file can hold rows for several tokens.
- Concurrency is new failure surface. Serial code that worked gains interleaving, partial failure across workers, and a rate limiter that can itself be wrong. The per-chain file split keeps the data safe, but a run can now fail halfway through more interestingly than before.

## Implementation plan

A follow-up PR, in this order:

1. `config.py` / `db.py` — `get_cache_db_path(chain)` and `connect(chain=...)` resolving to the per-chain file. The no-argument form stays working for development and tests.
2. Migration script — split the existing `cache.duckdb` by its `chain` column into the two per-chain files, with row counts reported per table so the split is verifiable rather than assumed.
3. Per-chain token-bucket rate limiter, as its own module with its own tests.
4. Worker pool for concurrent `(token, chain)` pair fetches.
5. `ATTACH` helper for cross-chain pipeline reads, plus the pipeline-stage test updates.
6. Structured progress events for observability.
7. Live verification — a parallel 1-day backfill measured against the serial baselines from PR #25 (AW_02: 18/18 pairs, 142,210 transfers, 1,149s) and PR #28, reporting wall-clock improvement per chain rather than in aggregate, since the whole point is that the two chains behave differently.

## References

- **PR #25** (`feat/multi-token-expansion`) — the AW_02 pagination latency measurements and the 57%-in-11% concentration finding.
- **PR #28** (`feat/aw-01-pool-expansion`) — the 18× Base cost analysis, and the 429 that re-confirmed ADR 0007 on a second chain.
- [ADR 0004](0004-duckdb-cache.md) — DuckDB as the cache, and the single-writer constraint that drives the per-chain split.
- [ADR 0006](0006-alchemy-eth-getlogs-block-window.md) — the free-tier 10-block `eth_getLogs` cap, one half of the request-count axis.
- [ADR 0007](0007-alchemy-cups-constraint.md) — request concentration as the real rate limit, which the per-chain budget is built against.
- [ADR 0009](0009-duckdb-connection-and-schema-conventions.md) — the UTC pin and schema-drift guard, both of which apply per file after the split.
- [ADR 0010](0010-defillama-historical-fallback.md) — a related slow-path decision for a different problem: coverage rather than throughput.
