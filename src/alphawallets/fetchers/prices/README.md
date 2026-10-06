# Price fetchers

Historical USD prices for V1 tracked tokens. Every swap and transfer the on-chain fetchers collect needs a price at its timestamp before PnL can be computed, so this is a hard prerequisite for the Week 3 pipeline work rather than an enrichment.

The pipeline joins on `date_trunc('hour', block_timestamp)`, so prices are stored on hour boundaries and queries assume cache hits — the pre-warm backfill runs first, and a daily top-up keeps it current (scheduled runs land in Week 5+).

Coverage justification: [ADR 0008](../../../../docs/decisions/0008-defillama-historical-prices.md) — 52/52 probes across a full year returned prices for all 12 V1 tracked tokens.

## Source

**DefiLlama**, free and keyless (`https://coins.llama.fi`). No credential to store, no rate-limit tier to manage. CoinGecko stays in reserve for tokens outside the V1 tracked set, where DefiLlama coverage is likelier to thin.

## Route decision

**`/chart/{coins}?start=&span=&period=` (bulk timeseries), not `/prices/historical/{timestamp}/{coins}` (single point).**

The V1 access pattern is "many timestamps for a handful of tokens", not "one timestamp at a time". One `/chart` request covers hundreds of hours; the per-timestamp endpoint would need one request per hour per token — hundreds of times the requests for the same data. `/prices/historical` is reserved for filling a specific gap, and is not used by AW_03.

## /chart quirks

Empirical findings from a live probe on 2026-09-30. Recorded here so a future reader doesn't re-discover them the hard way.

**1. `span` is a point count, not a duration.** `span="30d"` returns **30 points**, not 30 days of them — the `"d"` is silently ignored, and `span="30"` behaves identically. At `period=1h` that's about 1.2 days of history. Anything that reads `span` as a duration will quietly fetch a tiny fraction of what it asked for.

**2. A single request is capped at 500 points.** Exceeding it returns HTTP 400:

```json
{"message":"Requested 720 data points exceeds the maximum of 500."}
```

30 days hourly is 720 points, so it cannot be fetched in one call. `defillama_client.fetch_chart` handles this internally: callers pass `span_days` and the client splits into balanced chunks (720 → 2 × 360, not 500 + 220, so every request has the same shape and the logs stay comparable), walking `start` forward by `chunk_span × period_seconds`. `chunk_plan()` is shared with the CLI summary so the stated plan can't drift from the executed one.

**3. Timestamps are approximate, not hour-aligned.** Even with `period=1h`, points arrive at ~58 minute intervals on average and none land on an exact hour. Alignment happens in `mapper.py` by **truncation, not rounding** — a point observed at 10:58 belongs to hour 10, because rounding up would attribute a price to an hour it was never observed in and would push boundary points outside the requested range. See the `mapper.py` docstring.

A consequence: two observations can fall inside one hour (10:02 and 10:58), producing two rows with identical `ts`. The mapper does not deduplicate — see storage semantics below.

## Storage semantics

Table `token_price`, primary key **`(chain, token_address, ts, source)`**.

`source` is in the key deliberately. It reserves space for a CoinGecko fallback: a CoinGecko row for the same `(chain, token, hour)` coexists with the DefiLlama one instead of colliding, so the pipeline can select a provider per query and each price keeps its lineage. A three-column key would have forced silent overwriting or a table per provider.

**First-observation wins.** When two points land in the same hour, `INSERT OR IGNORE` keeps the first one written. A row therefore represents the hour's **first observed price**, not its last and not an average — consistent with truncation semantics, where the row is anchored to the start of the hour. `FetchResult.duplicate_after_alignment` counts the absorbed rows, so the overlap is visible rather than silent.

Writes are idempotent: re-running a span writes nothing new.

## Modules

| Module | Role |
|---|---|
| `models.py` | `RawPricePoint` (as reported) and `TokenPrice` (hour-aligned, alignment enforced by validator) |
| `defillama_client.py` | `/chart` HTTP access, chunking, retry. Owns wire-shape sanity |
| `mapper.py` | Entry → model mapping and hour-alignment. Owns value semantics |
| `writer.py` | `token_price` DDL and idempotent writes. Owns collision resolution |
| `aw_03_defillama_historical_prices.py` | Orchestrator (`fetch_and_persist_prices`) and CLI |

The layer split is enforced by tests: a point with a null timestamp is dropped by the client (it sorts and dedupes on that field), while a negative price is dropped by the mapper.

## Usage

```bash
uv run python -m alphawallets.fetchers.prices.aw_03_defillama_historical_prices \
    --chain ethereum \
    --token 0x1f9840a85d5af5bf1d1762f925bdaddc4201f984 \
    --span-days 30
```

## Resume mode

```bash
uv run python -m alphawallets.fetchers.prices.aw_03_defillama_historical_prices \
    --chain ethereum \
    --token 0x1f9840a85d5af5bf1d1762f925bdaddc4201f984 \
    --resume
```

Continues from `MAX(ts)` in `token_price` for that `(chain, token_address)` **and `source='defillama'`**, converting the gap into a span in whole days.

The source filter is load-bearing. `source` is in the primary key precisely so a CoinGecko backfill can sit beside the DefiLlama rows (see Storage semantics above), which means an unfiltered `MAX(ts)` would let another provider's coverage convince this fetcher it had already fetched a span it never requested.

The span is rounded **up** and never below 1, so a partial day is covered rather than left as a hole. Erring long costs a few redundant points because the writer's `INSERT OR IGNORE` drops hours already stored; erring short would leave a gap. When nothing is stored, it logs at INFO and falls back to the default 30-day span. Mutually exclusive with `--span-days`.

Note the price grid trails the chain head by roughly two hours, so a resume run will not reach the current hour — see [ADR 0009](../../../../docs/decisions/0009-duckdb-connection-and-schema-conventions.md) for how pipeline stages distinguish that lag from a real gap.

## Multi-token

Same `--token-symbol` flag as AW_02, resolved against `--chain` through the V1 registry:

```bash
uv run python -m alphawallets.fetchers.prices.aw_03_defillama_historical_prices \
    --chain ethereum \
    --token-symbol AAVE \
    --span-days 7
```

`--token 0x...` still works and is mutually exclusive with `--token-symbol`; passing neither keeps the UNI default. The flag is named `--token-symbol` rather than `--token` because `--token` already means the contract address here.

### Route selection

AW_03 picks its endpoint per token. `token_price_route` remembers the verdict, so the coverage probe costs one request per token *ever* rather than one per run.

| Verdict | Meaning | Cost for 30 days hourly |
|---|---|---|
| `chart` | The bulk route serves it | 2 requests |
| `historical` | Absent from `/chart`, served per timestamp | ~720 requests |
| `unpriceable` | Neither route returns a price | 0 — skipped |

**Probe on miss.** With no cached verdict, AW_03 probes `/chart` with a 2-point request. Covered becomes `chart`; empty becomes `historical`, which is then attempted. Only if the fallback *also* returns nothing is `unpriceable` recorded — `/chart` being empty says nothing about `/prices/historical`, so a single probe never concludes it.

Discovery lives in the fetcher rather than a config file because the gap is a property of the provider at a point in time, not of our token set: the next token to migrate the way MKR did gets classified by code, not by someone remembering to edit a list.

A verdict is a snapshot. `last_verified` records when it was established and `route_cache.clear_route()` forces a re-probe — a token that gains `/chart` coverage keeps using the slow path until something clears it, and nothing notices that on its own.

Measured on 2026-10-06 across all 18 verified `(token, chain)` pairs: **17 `chart`, 1 `historical`** (MKR on Ethereum), 2,893 rows in 63s. The fallback ran at **3.12 req/sec** with no 429s.

Each `token_price` row carries the `route` that produced it, so the provenance of a price is answerable after the fact. `route` is deliberately **not** in the primary key — a token has one authoritative price per hour, not one per route.

### Known limitation: provider grid granularity

MKR is now priced, but DefiLlama's `/prices/historical` data for it sits on a **4-hour grid**, not hourly: 168 hourly requests returned 43 points, with every gap exactly 4 hours and the stored hours exactly `{0, 4, 8, 12, 16, 20}`.

The pipeline joins on the hour, so MKR events in the other three hours of each block find no price row and classify as `unavailable` — which prints a warning telling the operator to re-run AW_03. **That remedy is false:** the provider has no hourly MKR data, so re-running cannot help.

This is the same error shape [ADR 0010](../../../../docs/decisions/0010-defillama-historical-fallback.md) exists to prevent, one level down. Wall-clock `pending` once told operators to re-run when the data did not exist upstream; now `unavailable` tells them to re-run when the data does not exist *at that resolution*. Proposed for its own record as ADR 0013 — likely a bounded nearest-prior-price lookup for PnL, plus a distinct off-grid state so the `unavailable` warning stays worth acting on.

### Verifying coverage

Coverage is a query against the registry, not a judgement call:

```sql
SELECT COUNT(DISTINCT (chain, token_address)) FROM token_price;
```

Comparing that set against `tokens.all_token_chain_pairs()` returned exactly one missing pair on 2026-10-03 — `('0x9f8f72aa…', 'ethereum')`, MKR — and zero missing from `erc20_transfer`. Any future gap surfaces the same way.
