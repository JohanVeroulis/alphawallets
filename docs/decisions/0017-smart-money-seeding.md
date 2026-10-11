# 0017 — Seed the wallet universe from curated Smart Money sources

- **Status:** Accepted
- **Date:** 2026-10-11

## Context

[ADR 0016](0016-contract-mediated-attribution.md)'s exclusion left the clean 30-day leaderboard with a top entry of **$67.90 on 2 trades**, and exactly **one** wallet that is both profitable and meets CLAUDE.md's provisional ≥10-trade bar (`0x5f545768…`, LINK, $0.91 over 20 trades). Thirteen wallets clear ≥10 trades at any PnL; two of those are profitable.

ADR 0016 was the right fix, and it exposed a problem underneath it that is not about attribution at all: **the wallet universe is the wrong population.**

Measured against the 2026-10-09 run, across 18,939 thirty-day partitions:

| realizations in the window | partitions | share |
|---|---|---|
| **0** | **18,223** | **96.2%** |
| 1–3 | 494 | 2.6% |
| 4–9 | 134 | 0.7% |
| 10+ | 88 | 0.5% |

The shape is sharper than "mostly small traders". **96% of partitions never realize anything at all** — they are wallets that received a token inside our window and did not sell it, or sold it to a counterparty we cannot price. A leaderboard ranking realized profitability over that population is ranking the 0.5% tail that happens to have traded, and the tail is small because the universe is indiscriminate, not because profitable traders are rare.

A deeper blanket backfill scales this proportionally. Doubling the cache doubles the 96% along with everything else; the top entry moves by a constant factor of nothing in particular. The constraint is not how much data we have but **whose** data it is.

CLAUDE.md §9 has carried a provisional "wallet-universe seed strategy" — bootstrap from large swaps plus public whale lists — since the project started. This ADR commits to it as V1's universe-definition policy.

## Decision

**The wallet universe is a hand-curated seed registry committed to the repo**, not a by-product of whatever the fetchers happened to scan. Four sources:

1. **Nansen Smart Money labels** — top-performing wallets by category (DEX trader, early investor, and similar) from public Nansen Smart Money dashboards, exported as a snapshot.
2. **Arkham Intelligence labels** — entity-labelled addresses from public Arkham dashboards: prop trading firms, known funds, verified MEV searchers.
3. **DeBank whale leaderboards** — top 200 by portfolio value from each chain's public leaderboard, Ethereum and Base.
4. **Large-swap originators** — derived from our own `uniswap_v3_swap` table rather than an external source. **See the subsection below: this source cannot be used as literally stated.**

Target ~500–1,000 deduplicated addresses, in `src/alphawallets/seeds/smart_money.json`, each entry carrying `source`, `snapshot_date` and `label` so a seed's provenance is auditable and a stale snapshot is visible rather than inferred.

Committed, not fetched at runtime. The universe is an input to every PnL figure the project produces, so it must be versioned alongside the code that reads it: a leaderboard that silently changes population between runs cannot be compared with itself.

### Source 4 needs custody resolution first

The obvious implementation — take `tx_from` of every swap above $100k notional — seeds **precisely the population ADR 0016 just excluded.** Measured on the current cache (notional from the WETH leg at $2,700/WETH):

| | wallets |
|---|---|
| originators of ≥$100k swaps | 184 |
| of those, with a partition ADR 0016 flags as contract-mediated | **167 (91%)** |
| of those, with any unflagged partition | 32 |

Across all 1,326 swap originators, 1,149 are flagged somewhere. This is not a coincidence to work around: **large on-chain swaps are overwhelmingly routed through contracts**, so the signer of a big swap is almost by definition a contract-mediated signer whose PnL ADR 0016 declines to rank. Seeding those addresses would add flagged rows, not leaderboard entries.

Source 4 is therefore **blocked on resolving custody** — identifying the address that actually holds the tokens in a large swap, which is the V1.5 attribution work ADR 0016 deferred. Two usable forms exist in the meantime: seed the *custody contract* rather than the signer (available from the transaction's own transfer legs), or defer source 4 entirely and ship with sources 1–3. The implementation PR decides; this ADR records that the naive form is known-wrong before anyone writes it.

### Why the fetchers do not take a seed list

The original shape for this decision was a `--seed-list` flag on AW_01 and AW_02, fetching only events that touch seeded addresses. **Measured, that does not work:** the two fetchers are asymmetric and neither gets cheaper in the way "filter to 1,000 addresses" suggests.

- **AW_01 cannot filter by wallet at all.** `eth_getLogs` filters on contract address and topics, and `tx_from` is neither — it is a property of the transaction, not of the log. A seed list here is a **client-side** filter: it reduces what we store, not what we request, so provider cost is unchanged.
- **AW_02's filter is per-address, not per-list.** `alchemy_getAssetTransfers` takes a single `fromAddress` or `toAddress`, and the fetcher currently passes `contractAddresses: [contract]` — one paginated call set per token, 18 sets for the whole V1 registry. Seeding by address means one paginated set per address per direction: 1,000 addresses becomes ~2,000 sets. That is **two orders of magnitude more provider calls than the blanket scan it replaces**, not less.

So seeding is justified by **signal quality, not by cost**, and the fetcher route would have cost more while delivering the same leaderboard.

### Implementation shape

**The seed registry is an eligibility filter in the PnL calculator, not an argument to a fetcher.** AW_01 and AW_02 stay universe-agnostic: they fetch per pool and per token exactly as they do today, and the cache remains a complete record of everything we have indexed.

The filter applies where eligibility is already decided — alongside `has_pre_window_activity` (ADR 0012 decision 5) and `has_smart_wallet_signal` (ADR 0016), which the CLI's ranking already honours. A non-seeded wallet's row is computed and written; it is simply not ranked.

Four reasons this is the better shape, and they are the reasons it is fixed here rather than left to the implementation:

1. **Identical leaderboard, no fetcher change.** The ranked population is the same either way, so the fetcher argument buys nothing the filter does not.
2. **No provider cost change at all.** Not "less" — none. The subsection above shows the fetcher route is the expensive one.
3. **The full cache stays available for the V1.5 custody work.** Source 4 is blocked on resolving which address holds the tokens in a large swap, and that resolution needs the transfer legs of *non-seeded* contracts. A fetcher that had skipped them would have to re-fetch, and ADR 0016's own fix already depended on legs the pipeline nearly discarded.
4. **Changing the universe stops being destructive.** Re-ranking against a refreshed registry is a recompute over existing data rather than a backfill, so a monthly refresh costs minutes and a bad snapshot is revertible. Under the fetcher shape, a wrong seed list would mean data we never collected.

The cost is that the cache keeps rows for wallets V1 will never rank — which is the same trade ADR 0012 already made with its caveat flags, and for the same reason: a row that exists and is excluded is auditable, while a row that was never fetched is invisible.

## Alternatives Considered

**Blanket 30-day backfill.** Rejected: it scales the 96%-never-realized population proportionally. More data of the wrong kind does not make a thin leaderboard thicker, it makes a thin leaderboard slower to compute.

**Pure factory enumeration — expand pool coverage instead.** This closes ADR 0014's fee-tier gap, which the 2026-10-09 spot-check measured as the *majority* case (2,598 pool-bound sales to unconfigured 0.05% pools against 2,066 to configured ones). Genuinely valuable, and it does not address this problem: wider pool coverage over an indiscriminate universe still ranks the same 0.5% tail. Kept as a parallel track, not a substitute.

**Realtime API integration with Nansen or Arkham.** Rejected on cost. Paid tiers defeat the free-tier design goal that ADR 0003 and ADR 0008 both exist to preserve. Snapshotting the public dashboards is the free equivalent. *Legal note:* snapshot the **labels** — observable facts about addresses — and not the dashboards' proprietary scoring or rankings.

**Twitter and Discord mentions of profitable wallets.** Rejected for V1: social signal with no reliable filter, and no provenance worth committing to a registry.

## Consequences

**What we gain:**
- **The leaderboard gets a population it can actually rank.** Seeded wallets trade frequently and at size, so they clear the ≥10-trade bar that one wallet currently meets. The expected magnitude is far above today's $67.90 — stated as an expectation, not a measurement, because no seeded run has happened yet and the current figure is itself a measurement of the wrong population.
- **Week 4 categorization becomes designable.** The four category rules (Traders, DeFi users, Airdrop Hunters, Yield Farmers) need known-populated examples to set thresholds against, and a labelled seed registry supplies exactly that.
- **Provenance per entry.** A figure traceable to a dated snapshot from a named source is auditable in a way "whatever the fetcher scanned" never was.

**What we lose / take on:**
- **V1 stops claiming to be a universal wallet analyzer.** It is a Smart-Money-seeded profitability tracker over a curated universe, and the homepage, docs and methodology page must say so plainly. A tool that ranks a hand-picked population while implying it ranks everyone is making a claim it cannot support.
- **A new maintenance dependency.** The registry needs refreshing — monthly for V1 — documented in a README rather than automated, because an automated refresh would silently change the population underneath a comparison.
- **Legal diligence before the frontend ships.** Snapshotting public labels is customary for research, but the sources get attributed on the Methodology page when the frontend lands (Week 5+).
- **Source 4 is not ready**, per the subsection above, and its naive form would actively work against ADR 0016.
- **Seeding is not a cost saving.** Under the chosen shape provider cost is unchanged, and the fetcher-argument alternative would have increased it. The cache keeps rows for wallets V1 will never rank.
- **Known-good rows will leave the board.** Candidate B (`0x2c554906…`, $15.38), the ADR 0014 gap wallet currently ranked second, appears in no Smart Money list and will almost certainly drop off. That is this ADR's logic working, not a regression — but it means the board is no longer comparable across the change, and the shrinking-leaderboard measurements from ADR 0016 become a different baseline rather than a continuing series.

## References

- CLAUDE.md §9 — the provisional seed strategy this ADR commits to and de-provisionalises.
- [ADR 0016](0016-contract-mediated-attribution.md) — the attribution fix whose aftermath exposed the universe-scale problem, and whose exclusion source 4 would collide with.
- [ADR 0014](0014-pool-destination-realization.md) — the fee-tier gap kept as a parallel track above.
- [ADR 0007](0007-alchemy-cups-constraint.md) — the CUPS ceiling that made the per-address fetcher route untenable, and which the chosen calculator-level filter does not touch at all.
- **Hand spot-check, 2026-10-09** — the Etherscan validation and the partition-distribution measurements quoted in Context.
