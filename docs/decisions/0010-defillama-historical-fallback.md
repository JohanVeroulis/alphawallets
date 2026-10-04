# 0010 — DefiLlama historical price fallback for /chart gaps

- **Status:** Accepted
- **Date:** 2026-10-04

## Context

AW_03 fetches bulk historical hourly prices from DefiLlama's `/chart` endpoint. That route was chosen because V1's access pattern is "many timestamps for a handful of tokens" — one request covers hundreds of points, where the per-timestamp endpoint would need one request per hour per token (ADR 0008, and the route decision recorded in `fetchers/prices/README.md`).

The multi-token registry work (PR #25) surfaced a gap. MKR is served by `/prices/current` and `/prices/historical` but is **absent from `/chart` entirely**, at every span tried:

| | `/prices/current` | `/prices/historical` | `/chart` (span 168, 24, 2) |
|---|---|---|---|
| **MKR** `0x9f8f72aa…` | $2032.3869 | $1939.6375 | coin absent, 0 points |
| UNI (control) | $9.1510 | $9.1370 | 168 / 24 / 2 points |
| SKY `0x56072c95…` | $0.0890 | $0.0844 | 168 / 24 / 2 points |

UNI and SKY both return full timeseries, so this is specific to MKR rather than a route-wide problem — plausibly a consequence of the MakerDAO MKR-to-SKY migration. AW_03 fails on it with `DefiLlama returned no data for ethereum:0x9f8f72aa...`.

Two things make this worth a decision rather than a patch:

1. **Coverage is endpoint-specific, and ADR 0008's verification could not have shown it.** That ADR's "52/52, 100% coverage" result was measured on `/prices/historical`; AW_03 fetches with `/chart`. The amendment scoped the claim and left the fallback as a tracked follow-up. Measured across all 18 verified `(token, chain)` pairs, `/chart` covers 17.
2. **A permanently uncovered token is a third classification state.** ADR 0009's grid-head rule distinguishes `pending` (above `MAX(token_price.ts)`, the backfill has not arrived) from `unavailable` (at or below it, a real hole). A token with no `/chart` coverage at all never gets a grid head, so every one of its events stays `pending` forever — indistinguishable from a backfill that simply has not run. That erodes `pending`'s meaning and leaves `unavailable` unable to tell a route gap from a genuine hole.

This ADR records how AW_03 will cover gap tokens without giving up the `/chart` fast path for the 17 of 18 that work.

## Decision

Add a per-token fallback route, selected by a cached probe, writing into the same table.

### Route

`/prices/historical/{timestamp}/{coins}` — one call per `(token, hour)`. Expensive per call, and invoked only for tokens `/chart` does not serve. This is the endpoint the prices README already reserved for gap-fill, so the route is not new; only the logic that selects it is.

### Discovery

At run time the fetcher consults a per-`(chain, token)` route cache. On a miss it tries `/chart`, records the verdict — `chart` when the response contains the coin, `historical` when it does not — and proceeds on that route. An explicit re-probe is supported so an upstream change (a token gaining `/chart` coverage, or losing it) can be picked up without hand-editing the cache.

Discovery lives in the fetcher rather than in a config file because the gap is a property of the provider at a point in time, not of our token set. A new V1 token, or a token that migrates the way MKR did, must be classified by the code rather than by someone remembering to update a list.

### Granularity

Hourly, matching `/chart`. A 30-day backfill for a gap token is 720 points and therefore 720 requests. That is acceptable against the free tier (no API key, roughly 300 requests/minute observed) when it applies to one or two tokens; the actual sustainable rate is to be measured on the live run during implementation rather than assumed from this number.

### Storage

The same `token_price` table, not a sibling. A new metadata column `route: Literal["chart", "historical"]` records how each row was obtained.

The primary key stays `(chain, token_address, ts, source)`. A token has **one authoritative price per hour, not two**, so `route` is forensic rather than canonical — it answers "how did this row get here" for an operator, and must not become a dimension the PnL join has to reason about. Adding `route` to the key would permit two prices for one hour and push a choice downstream that belongs here.

`source` stays `'defillama'` for both routes, because both are DefiLlama. The `source` column exists to separate *providers* (ADR 0008 reserves CoinGecko), and overloading it with route would conflate two different axes.

### Resume semantics

The fallback uses the same `get_resume_point` logic as the primary route — `MAX(ts)` per `(chain, token_address, source='defillama')` — so `--resume` behaves identically whichever route a token is on. The route cache lives in a sibling `token_price_route` table and is not part of the resume calculation.

### Rate limiting

A sleep-based throttle plus honouring `Retry-After`. ADR 0007's CUPS lesson applies to a different provider: the constraint is request concentration, and 720 sequential calls is exactly the shape that found Alchemy's ceiling. Measure the real rate on the live run.

## Alternatives Considered

**Daily resolution for gap tokens.** Rejected. It breaks the hourly join invariant the whole pipeline is built on: AW_03 enforces hour alignment in its model validator, the PnL join is `date_trunc('hour', block_timestamp) = token_price.ts`, and ADR 0009's classification is hour-granular. A daily row would either miss the join entirely or need a special case in every consumer.

**Spot checkpoints with interpolation.** Rejected, and this is the one worth being explicit about: it adds a lie layer. An interpolated price is indistinguishable in the table from an observed one, so downstream PnL would compound estimation error with no way for an operator to know which numbers were measured. The project has consistently chosen to make absence visible — three-state classification, `decimals_assumed`, `confidence` — rather than fill it in plausibly.

**CoinGecko as the fallback.** Rejected for V1. A second provider means a new HTTP client, a different auth story, an API key to manage, and a tighter free-tier rate limit — all to solve a problem a second DefiLlama endpoint already solves. ADR 0008 kept CoinGecko in reserve for tokens outside the V1 tracked set; this ADR affirms it stays there. The `source` column in the primary key remains its reserved seat.

**Skip gap tokens entirely.** Rejected. MKR has real trading activity — 2,429 transfers in a single day of the PR #25 backfill — so excluding it would silently drop valid wallets from PnL, which is precisely the class of failure this project keeps trying to make loud instead of quiet.

**Pre-populate the route cache from a manual probe.** Considered and partially adopted. Seeding the cache with today's known verdicts makes the first run faster and is acceptable as a starting state, but it must not be the mechanism: a seeded-only cache hides discovery from the codebase and leaves the next MKR to be found by a failing run. Seed as an optimisation, discover as the contract.

## Consequences

**What we gain:**
- Universal price coverage for V1 tokens, short of DefiLlama failing on every endpoint at once.
- `pending` and `unavailable` recover their meanings. A route gap stops masquerading as a permanently-not-backfilled token, so `unavailable` becomes a signal worth alerting on — which was the stated goal of ADR 0009's grid-head rule and is undermined by any token that can never acquire a grid head.
- The route cache is observable. `SELECT * FROM token_price_route` tells an operator which tokens are on the slow path, when each verdict was established, and which are stale enough to re-probe — rather than that knowledge living in a maintainer's memory.
- `route` on each row makes the provenance of a price answerable after the fact, which matters the first time a PnL number is disputed.

**What we lose / take on:**
- A second endpoint client. The two routes return different response shapes, so there is real mapping code and real test surface, not a parameter change.
- Another table to create, guard and keep consistent. The schema-drift guard from ADR 0009 applies to `token_price_route` from its first commit, and `token_price` gaining a `route` column is itself a drift event for every existing cache file — the guard will catch it, and the remedy is the documented drop-and-re-fetch.
- One wasted `/chart` call per newly-discovered gap token. Acceptable: it happens once per token, and the alternative is a config file that goes stale.
- A gap token's backfill is roughly three orders of magnitude more requests than a `/chart` token's. Fine at one or two tokens; if the gap set grows, the throttle becomes the dominant cost of a scheduled run and this decision should be revisited alongside the pagination work.

## Implementation plan

A follow-up PR, in this order:

1. `defillama_client.py` — add `fetch_historical_price(client, chain, token_address, timestamp) -> PricePoint | None`, with mocked tests covering the response shape, a missing coin, and the retry path.
2. `route_cache.py` — a new module with DuckDB-backed read/write for `token_price_route`, its own `EXPECTED_COLUMNS` and schema-drift guard per ADR 0009, and mocked tests.
3. Orchestrator extension — the probe → cache → route flow, plus the `route` column on `token_price`.
4. Live verification against MKR, confirming a gap token gets priced end to end, with the measured request rate reported.
5. Docs and PR.

## References

- [ADR 0008](0008-defillama-historical-prices.md) — DefiLlama as the V1 price source, and the endpoint-specific-coverage amendment that triggered this ADR.
- [ADR 0009](0009-duckdb-connection-and-schema-conventions.md) — the UTC session pin and schema-drift guard both apply to the new `token_price_route` table and to `token_price` gaining a column.
- [ADR 0007](0007-alchemy-cups-constraint.md) — request concentration as the real rate-limit constraint, the lesson the throttle here is built against.
- **PR #25** (`feat/multi-token-expansion`) — origin of the MKR finding, the 17/18 `/chart` coverage measurement, and the Week 3 awareness note on a permanently-uncovered token as a classification state.
- `src/alphawallets/fetchers/prices/README.md` — the route decision and the recorded `/chart` quirks.
