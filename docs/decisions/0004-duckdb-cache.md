# 0004 — DuckDB as V1 local cache and analytical store

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

ADR 0003 committed the project to Alchemy as the sole V1 data source. That decision made a local storage layer a hard requirement: raw RPC responses need to be persisted, decoded events indexed, and analytical queries answered without re-fetching from Alchemy every time (both to stay under the 300M CU/month free-tier limit and to keep query latency reasonable).

The store needs to:

- Handle millions of rows (Uniswap V3 swap events alone reach millions per month per chain)
- Support fast analytical queries: aggregations, joins, group-by, window functions
- Live locally with no server to run — the V1 team is one person
- Deploy trivially (single file, no infrastructure)
- Work well with pandas 2.x (ADR 0002) for downstream analysis
- Cost nothing to run

## Decision

**DuckDB is the V1 local cache and analytical store.**

The `data/cache.duckdb` file (already reserved in `.env.example` via `ALPHAWALLETS_CACHE_DB`) holds:

- Raw fetched events (swaps, transfers, logs) per chain
- Decoded per-protocol tables (Uniswap V3 swaps, ERC-20 transfers, etc.)
- Historical price snapshots (from DefiLlama or fallback source — open question in ADR 0003)
- Derived analytical tables (per-wallet PnL, categorization, rankings)

Fetchers write to it; the analytical layer reads from it.

## Alternatives Considered

| Alternative | Why not |
|---|---|
| SQLite | Row-oriented, weak on analytical aggregations at the scale we expect. Popular and stable, but wrong tool for millions of rows of swap events. |
| PostgreSQL | Overkill for V1. Server to run, connection management, deployment overhead. Reconsider only if V1 grows into a hosted product. |
| Parquet files only | Great for archive, poor for interactive queries. DuckDB reads Parquet natively anyway, so we can add Parquet layers later without changing the query engine. |
| Cloud data warehouse (BigQuery, Snowflake) | Cost, latency, and vendor lock-in. Wrong shape for a solo pre-revenue project. |
| MongoDB or other document stores | Document semantics don't match analytical workloads. Would fight the tool at every join. |

## Consequences

**What we gain**

- Zero-config setup: one file on disk, no server, no ports.
- Columnar storage: fast aggregations, group-by, and analytical scans out of the box.
- Native Parquet read/write: keeps the door open to a Parquet-based archive layer without migration.
- First-class pandas integration: `duckdb.query(...).df()` returns a DataFrame; no manual cursor plumbing.
- SQL familiarity: the project owner is a 3-year data engineer working in SQL every day.
- Portable: the whole store is a single file we can hand to a fresh machine or attach to a Jupyter notebook.

**What we lose / take on**

- **Single-writer at a time.** DuckDB does not allow concurrent writes to one file. This will bite the moment scheduled fetchers land — the ROADMAP has GitHub Actions runs, and if per-chain fetchers ever run in parallel there, the second writer is refused. Concrete options when scheduling work starts: serialize the fetchers (single job with sequential chain calls), fan out to per-chain files (`cache_ethereum.duckdb`, `cache_base.duckdb`), or move to Postgres. Decide in the scheduling PR, not later.
- Not designed as a hosted OLTP system — if V2 exposes an API used by many users concurrently, we revisit (likely by adding a read replica or moving hot tables to Postgres).
- Local backup is our responsibility. `data/` is git-ignored, so the store lives on disk only; a lost laptop means a rebuild from Alchemy.
- Schema management is manual for V1. No migrations framework yet; we accept ad-hoc `CREATE TABLE IF NOT EXISTS` and version schemas by convention until pain justifies a tool.

**Revisit when**

- V2 introduces multi-user concurrent reads or writes.
- The store's size or query patterns push DuckDB past a comfortable single-node envelope.
- A cloud-hosted V2 makes central storage a hard requirement.
- Schema drift becomes painful enough to justify a migrations tool.
