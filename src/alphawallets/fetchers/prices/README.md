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
