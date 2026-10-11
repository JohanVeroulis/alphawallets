# Wallet-universe seed registry

> ## ⚠️ `smart_money.json` IS PLACEHOLDER DATA
>
> Every entry carries `"source": "placeholder-local-cache"`. These are **not**
> Smart Money wallets. They are twelve addresses observed in our own DuckDB
> cache on 2026-10-11, chosen to exercise the filter — a mix of clean traders,
> contract-mediated signers and one verified custody contract.
>
> **They exist to prove the infrastructure works, not to rank anybody.** Any
> leaderboard produced from this file is a test fixture, not a result.
>
> Proper curation from Nansen, Arkham and DeBank is a **separate task**, per
> [ADR 0017](../../../docs/decisions/0017-smart-money-seeding.md).

## Why these addresses, and not real Smart Money labels

The bootstrap entries were taken from the local cache rather than invented to
look like curated labels. Writing plausible-sounding addresses under sources
like `placeholder-dune-query-123` or `placeholder-arkham-public` would put
**false provenance** into a committed registry — and provenance is the entire
point of the schema ADR 0017 specifies. An address labelled "Nansen Smart Money
DEX trader" that nobody ever read off a Nansen dashboard is worse than no entry
at all, because the next person to touch this file has no way to tell the
difference.

So every current entry is real, locally verifiable, and honestly labelled. The
`source` value says exactly where it came from, which is also what makes the
whole file trivially greppable when the real curation lands.

One side effect worth knowing: because these addresses have activity in our
7-day cache, `--seeded-only` returns **non-empty** results. That makes the filter
testable end to end, which a registry of inactive real whales would not have.

## Schema

A JSON list. Every field is required; a missing one raises
`SeedRegistryError` rather than being skipped, because a silently dropped entry
would shrink the ranked universe invisibly.

```json
{
  "address": "0x6420a51576732468eff4daffdb168f573cd2b2df",
  "chain": "ethereum",
  "source": "placeholder-local-cache",
  "label": "top clean UNI trader (+$67.90 over 2 realizations)",
  "snapshot_date": "2026-10-11"
}
```

| field | meaning |
|---|---|
| `address` | 0x-prefixed, 42 chars. Any case — lowercased on load. |
| `chain` | `ethereum` or `base`. An address is only seeded on the chain it is listed for. |
| `source` | Where the entry came from. Prefix real curated sources plainly (`nansen-smart-money`, `arkham-public`, `debank-whales`). |
| `label` | What this wallet is, in human terms. Shown in reports. |
| `snapshot_date` | ISO date the entry was captured, so staleness is visible rather than inferred. |

## How it is used

The registry is an **eligibility filter in the PnL calculator**, never an
argument to a fetcher. AW_01 and AW_02 remain universe-agnostic, so:

- Non-seeded wallets are still computed and still written to `wallet_pnl`. They
  are excluded from *ranking*, not from the cache.
- Provider cost is unchanged. A seed list cannot reduce `eth_getLogs` cost
  (`tx_from` is not a log topic) and would multiply `getAssetTransfers` calls by
  the address count.
- A registry refresh is a recompute, not a backfill.

```bash
uv run python -m alphawallets.pipeline.pnl --dry-run              # full universe
uv run python -m alphawallets.pipeline.pnl --dry-run --seeded-only # ranked universe
```

The seed filter is **additional** to the existing exclusions, not a replacement:
a seeded wallet carrying `has_pre_window_activity` (ADR 0012 decision 5) or
`has_smart_wallet_signal` (ADR 0016) is still excluded.

## Maintenance

**Refresh monthly for V1, by hand.** Not automated: an automatic refresh would
change the ranked population underneath a comparison without anyone noticing,
and the registry is committed precisely so a population change shows up in
`git log`.

When replacing the placeholders:

1. Snapshot the **labels** from public dashboards — observable facts about
   addresses — and not the dashboards' proprietary scoring or rankings.
2. Set `source` and `snapshot_date` per entry. Do not reuse
   `placeholder-local-cache` for anything curated.
3. Attribute the sources on the app's Methodology page when the frontend lands
   (Week 5+). Legal diligence is owed before that ships.
4. Note that the large-swap source cannot seed swap `tx_from`: 167 of the 184
   originators of ≥$100k swaps (91%) are addresses ADR 0016 flags as
   contract-mediated, because large swaps are routed through contracts. Seed the
   custody contract instead, or defer that source.
