# Wallet-universe seed registry

## Source and methodology

`smart_money.json` holds **12 real wallets, 16 rows** (one per chain the wallet
is seeded on), curated by hand.

| field | value |
|---|---|
| Source | [DeBank Web3 Social Ranking](https://debank.com/ranking) |
| `source` tag | `debank-web3-social-ranking` |
| Collected | 2026-10-12 |
| Selection | Top 12 wallets by DeBank's own blended rank |
| Ranking criterion | DeBank's blend of net worth + TVF (Total Value Facilitated) |
| DeBank's own pre-filter | Wallet must have logged in to DeBank at least once |
| Our filter | Some Ethereum or Base holdings — all 12 pass |

### This is a net-worth ranking, not a PnL ranking

**Net worth is not skill.** A wallet holding $73.8M may be a brilliant trader, a
early entrant who has held since 2017, or a custody address. DeBank's ranking
cannot tell those apart and neither can this file.

That is fine, and it is the point of ADR 0017: **the seed registry is an
eligibility filter, not a quality filter.** It decides whose PnL gets ranked;
[ADR 0012](../../../docs/decisions/0012-pnl-methodology.md)'s calculator decides
who was actually skilled. Expect most of these 12 to rank poorly or not at all —
that is the pipeline working, not a bad seed list.

Rank 11 (`Mesut`, ~$24K total) is kept deliberately for the same reason. It would
fail any net-worth threshold and is retained because a threshold on holdings
would be a quality judgement the registry has no business making.

### The chain split

A seed is keyed on `(chain, address)`, because an address is only meaningful on
the chain it is listed for. Wallets with an Ethereum-only or Ethereum-heavy
profile get one row; the four with Base-heavy or balanced profiles get two.

| rank | nickname | ETH | Base | chains seeded | profile |
|---|---|---|---|---|---|
| 1 | an0n | $3.47M | $167K | ethereum, base | DeFi + trading, multi-chain |
| 2 | Ethereum | $26.5M | unknown | ethereum | heavy DeFi (Aave V4/V3, Morpho, Pendle, Fluid) |
| 3 | 33EE | $42.9M | $100 | ethereum | pure Ethereum; Aave V3 + Lido + Yearn + Curve |
| 4 | TradFiWhale | $12.1M | unknown | ethereum | Ethereum 98% |
| 5 | 4What | $16.4M | unknown | ethereum | Ethereum 99% |
| 6 | Tson | $73.8M | unknown | ethereum | Ethereum 100% |
| 7 | just_a_guy | $6.9M | unknown | ethereum | Ethereum 99% |
| 8 | cucbku | $2.6M | $26.7M | ethereum, base | Base-heavy (87%), rare pure-Base whale |
| 9 | 0xbbbc | $9.8M | unknown | ethereum | Ethereum 100% |
| 10 | Powerstake | $21.7M | unknown | ethereum | Ethereum 100% |
| 11 | Mesut | $10.8K | $13K | ethereum, base | small holdings; kept intentionally, see above |
| 12 | Gekko | $436.6K | $400.3K | ethereum, base | balanced ETH + Base |

Rank 3's $100 of Base holdings is treated as noise rather than a position, so
`33EE` is seeded on Ethereum only. Ranks 2, 4–7, 9 and 10 have unknown Base
holdings and are seeded on Ethereum only; if a later snapshot establishes Base
activity, add the row then rather than guessing now.

### Coverage caveat

These are large, mostly Ethereum-native DeFi wallets. Our cache currently holds
a shallow backfill of twelve tokens over roughly seven days, so **most of these
wallets have no events in it at all** — see the PR that introduced them for the
measured figure. That is a statement about backfill depth, not about the seeds.

## Schema

A JSON list. Every field is required; a missing one raises
`SeedRegistryError` rather than being skipped, because a silently dropped entry
would shrink the ranked universe invisibly.

```json
{
  "address": "0x33eecc48943aaeabb5328a25ff28eb85f67945c2",
  "chain": "ethereum",
  "source": "debank-web3-social-ranking",
  "label": "33EE",
  "snapshot_date": "2026-10-12"
}
```

| field | meaning |
|---|---|
| `address` | 0x-prefixed, 42 chars. Any case — lowercased on load. |
| `chain` | `ethereum` or `base`. An address is only seeded on the chain it is listed for. |
| `source` | Where the entry came from. Currently all `debank-web3-social-ranking`; name other dashboards plainly (`nansen-smart-money`, `arkham-public`). |
| `label` | What this wallet is, in human terms. Currently the DeBank nickname, original casing. Shown in reports. |
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

When refreshing or extending the registry:

1. Snapshot the **labels** from public dashboards — observable facts about
   addresses — and not the dashboards' proprietary scoring or rankings.
2. Set `source` and `snapshot_date` per entry, naming the dashboard the entry
   was actually read from. Never write provenance for a source nobody read: the
   schema exists so a later reader can tell a curated entry from a guess, and a
   plausible-looking invented entry destroys that distinction for the whole file.
3. Attribute the sources on the app's Methodology page when the frontend lands
   (Week 5+). Legal diligence is owed before that ships.
4. Note that the large-swap source cannot seed swap `tx_from`: 167 of the 184
   originators of ≥$100k swaps (91%) are addresses ADR 0016 flags as
   contract-mediated, because large swaps are routed through contracts. Seed the
   custody contract instead, or defer that source.
