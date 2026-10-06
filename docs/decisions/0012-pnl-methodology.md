# 0012 — PnL calculation methodology

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

Week 3 of the ROADMAP commits to realized PnL with FIFO cost basis, airdrop-aware separation, and per-wallet PnL tables. It flags three decisions as expected during that week: transfer-in/out treatment, the trading-versus-airdrop split, and pre-window activity handling.

This ADR records ten methodology decisions in one document because they are architecturally interdependent. Transfer treatment constrains airdrop handling; granularity constrains window semantics; the classification flags only make sense against the scope decisions. Splitting them into separate records would hide the connections that make them coherent.

The PnL calculator is the foundation for Week 4 categorization and the Week 5 leaderboard. A methodology error here does not surface as a crash — it surfaces as a plausible number that ranks the wrong wallets, and every layer above inherits it silently. That is the reason to write the methodology down before writing the code.

Three recent decisions shape this one directly:

- **PR #27** introduced the fourth classification state, `unpriceable`. PnL must handle events on tokens that cannot be priced at all, rather than assuming every event has a USD value.
- **ADR 0010** designed the DefiLlama `/prices/historical` fallback. Until that implementation lands, MKR events remain `unpriceable`.
- **ADR 0011** splits the cache per chain. PnL therefore runs per chain file and aggregates across chains at report time via `ATTACH`.

## Decision

Ten sub-decisions, in three groups.

### Cost basis mechanics

**1. FIFO cost basis.** First in, first out. Deterministic, spot-checkable against a block explorer by hand, and the default most tax regimes assume, which makes the output legible to anyone who has reconciled a portfolio before.

**2. Transfer-IN uses price-at-receipt as cost basis, not zero.** A wallet withdrawing from Coinbase paid for those tokens somewhere we cannot see. Treating the receipt as free would inflate PnL for every CEX-active wallet, and CEX-active wallets are a large share of any realistic universe — this is not an edge case, it is the common case. Two exceptions:

- **Identified airdrop receipts are zero-cost**, because they genuinely were free. See decision 4.
- **A wallet's own self-transfers should preserve cost basis** rather than re-basing at receipt. V1 cannot detect that two addresses belong to one person, so this is deferred to V1.5 and remains a known source of error: moving tokens between your own wallets will re-base the cost at the transfer price.

**3. Transfer-OUT ends tracking and is not a realization.** It reduces the balance in the FIFO stack, but we do not know whether the wallet sold on a CEX, paid a counterparty, bridged, or moved to its own cold storage. Recording it as a realization would invent a price; recording it as nothing would leave a phantom balance. Reducing the stack without realizing PnL is the honest middle, and it means the cost basis of the transferred amount is **lost**. This is a documented limitation, not a rounding detail — a wallet that accumulates on-chain and sells on a CEX will show understated PnL.

### Classification

**4. Airdrop detection by known distribution contracts.** For each tracked airdrop token, the distribution contract addresses are hardcoded, in the same hand-verified style as the token and pool registries. A transfer **from** a known distribution contract is flagged as airdrop income, tracked as a separate zero-cost sub-stack in the FIFO engine, so selling it realizes the full proceeds as PnL — which is correct, because the tokens were free.

Two scope notes carried over from [ADR 0001](0001-airdrop-selection.md), which must be reconciled during implementation rather than discovered then:

- **ARB cannot be detected this way.** ADR 0001 records that the ARB claim happened on Arbitrum and that V1 tracks ARB only after it was bridged to Ethereum, with the claim itself explicitly not attributed. There is no Ethereum distribution contract to match, so bridged-in ARB will be treated as an ordinary transfer-IN and receive a price-at-receipt cost basis. Airdrop-vs-trading separation is therefore unavailable for ARB in V1, and the registry should say so in place of an address rather than leave a reader to wonder.
- **MORPHO rewards may have been distributed on Base.** ADR 0001 flags this as a Week 3 check. If so, the registry needs a per-chain distribution address rather than one per token.

**5. Pre-window activity: flag and exclude from the leaderboard.** A wallet holding a balance before the indexed data begins gets `has_pre_window_activity = true`. PnL is still computed for the visible events, but the wallet is excluded from leaderboard rankings, because its cost basis is unknowable and its apparent profit is an artifact of where the data starts. Deeper backfill in V1.5+ resolves most cases.

**6. Unpriceable events: flag, do not exclude.** Per PR #27, some events cannot be priced at all — MKR today. The wallet gets `has_unpriceable_events = true` and **stays** in the leaderboard, with PnL covering its priced events only. Excluding it would silently shrink the universe for a provider's gap; flagging it makes the incompleteness visible to whoever reads the number.

**7. Smart-contract-wallet mediation: per-EOA with a flag.** `tx_from` identifies the signing EOA, not the Safe or ERC-4337 account that owns the position. V1 computes PnL per EOA. Wallets whose activity patterns suggest Safe mediation get `has_smart_wallet_signal = true`. Detection and merging of Safe identities is V1.5+.

### Scope and semantics

**8. Granularity: per `(wallet, token)` internally, aggregated externally.** The engine computes per `(wallet, token)` because FIFO is only meaningful within one asset. The leaderboard aggregates to per-wallet; the API and UI expose the per-token breakdown, which is also the input signal Week 4 categorization needs.

**9. Window semantics: running cost basis, PnL sliced by realization time.** Cost basis accumulates from the start of indexed data and is **not** reset at a window boundary. PnL for a window is the sum of realizations whose timestamps fall inside it. The 30-day and 90-day windows share one cost-basis engine state and differ only in which realizations they count.

**10. Module placement: `src/alphawallets/pipeline/pnl/`, not a new `AW_04`.** Per CLAUDE.md §6, `AW_*` names are reserved for ingestion from external sources, and derived analysis lives in `pipeline/`. PnL reads the local DuckDB only — no provider, no credential, no rate limit.

### Schema

> **Note:** the brief for this ADR referenced a SQL block that was not included in the message. The table below is written from the field list that was described — PK, split PnL, volume context, position state, and three caveat flags — and should be checked against the intended version before implementation.

```sql
CREATE TABLE IF NOT EXISTS wallet_pnl (
    -- Identity and window
    chain                     VARCHAR     NOT NULL,
    wallet                    VARCHAR     NOT NULL,
    token_address             VARCHAR     NOT NULL,
    window_start              TIMESTAMPTZ NOT NULL,
    window_end                TIMESTAMPTZ NOT NULL,

    -- Split PnL: trading is the skill signal, airdrop is the luck signal
    realized_pnl_usd          DOUBLE      NOT NULL,
    realized_pnl_trading_usd  DOUBLE      NOT NULL,
    realized_pnl_airdrop_usd  DOUBLE      NOT NULL,

    -- Reserved for V1.5; computed as NULL in V1 (decision: realized only)
    unrealized_pnl_usd        DOUBLE,

    -- Volume context, so a PnL number can be read against the activity producing it
    bought_usd                DOUBLE      NOT NULL,
    sold_usd                  DOUBLE      NOT NULL,
    realization_count         INTEGER     NOT NULL,

    -- Current position state at window_end
    balance_token             VARCHAR     NOT NULL,  -- uint256 scale, VARCHAR per ADR 0004
    avg_cost_basis_usd        DOUBLE,                -- NULL when the balance is zero

    -- Caveat flags: decisions 5, 6 and 7. Surfaced, never silently applied.
    has_pre_window_activity   BOOLEAN     NOT NULL DEFAULT FALSE,
    has_unpriceable_events    BOOLEAN     NOT NULL DEFAULT FALSE,
    has_smart_wallet_signal   BOOLEAN     NOT NULL DEFAULT FALSE,

    computed_at               TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (chain, wallet, token_address, window_start, window_end)
);
```

`realized_pnl_usd` is the sum of the trading and airdrop columns, stored rather than derived so the leaderboard can sort without recomputing. `balance_token` is VARCHAR for the same reason every raw amount in this project is: uint256 overflows `HUGEINT`.

## Alternatives Considered

**HIFO cost basis.** Rejected. It optimises for tax outcome but obscures economic reality, and the leaderboard needs PnL that is *comparable across wallets*, not minimised per wallet. Two wallets with identical trades would rank differently under HIFO depending on lot composition.

**Transfer-IN at zero cost.** Rejected. It would systematically inflate PnL for every CEX-active wallet, producing a false profitability signal exactly where the universe is densest — the failure would look like success, which is the worst shape for a ranking system.

**Window-isolated cost basis**, reset at the window boundary. Rejected, and the counterexample is concrete: a wallet that bought UNI 60 days ago and sold 20 days ago would show zero cost in the 30-day window, booking the entire sale price as profit. It also breaks comparability between the 30- and 90-day views of the same wallet.

**Compute PnL only for identified Safe wallets**, excluding EOAs with a mediation signal. Rejected as too restrictive for V1: most trading EOAs do not trigger a strong Safe signal, so this would discard real data to avoid a minority error. Flag and include is more honest and keeps the universe intact.

**Mark-to-market unrealized PnL in V1.** Rejected, deferred to V1.5. V1 ships realized PnL only. The schema reserves `unrealized_pnl_usd` and computes NULL, so adding it later is not a migration of the primary key.

**Separate ADRs for each sub-decision.** Considered and rejected. The decisions constrain each other — transfer treatment determines what airdrop handling has to special-case, granularity determines what window semantics can mean — and ten records would document the parts while losing the reasoning that connects them.

## Consequences

**What we gain:**
- A PnL number that survives hand spot-checking against a block explorer, for priced events under FIFO. That is the property that makes the whole leaderboard credible.
- A clean separation of trading PnL from airdrop PnL — skill signal from luck signal — which is the distinction the product exists to make.
- Transparent caveats. Three flags surface the known-limitation cases instead of silently excluding wallets, so a consumer can decide what to trust rather than inheriting our judgement invisibly.
- A V1-scoped surface with its edges documented and visible: transfers out, Safe mediation, pre-window activity.
- The input signal for Week 4 categorization, since a wallet's per-token PnL profile is what distinguishes a Trader from a Yield Farmer from an Airdrop Hunter.

**What we lose / take on:**
- **The transfer-out blind spot.** Tokens leaving to a CEX or an external wallet lose their cost basis, so a wallet that accumulates on-chain and exits off-chain shows understated PnL. Reports should name this as "sold outside the tracked universe" rather than let it read as a loss.
- **Self-transfers re-base cost.** Until V1.5 address clustering, moving tokens between one's own wallets looks like a purchase at the transfer price. This can cut either way and is the most likely source of an individually wrong number.
- **No Safe-mediated attribution.** Per-EOA PnL misses positions owned by a smart wallet, captured only as `has_smart_wallet_signal`.
- **Pre-window exclusion shrinks the leaderboard universe.** Expected to shrink as backfill depth grows; worth measuring how many wallets it removes, because if it is most of them the window needs extending before the leaderboard means anything.
- **MKR shows incomplete PnL** on wallets that trade it, until ADR 0010's fallback lands. AW_01 now has a verified MKR pool (PR #28), so MKR swap data exists while its prices do not — this is the first token where that asymmetry is live.
- **PnL for a fixed window can change between runs.** A running cost basis means re-fetching earlier events — reorg handling, or extending the backfill further back — legitimately alters a window's result. This is expected behaviour, not a bug, and `computed_at` is in the schema so a changed number can be explained rather than disputed.

## Implementation plan

A follow-up PR. Module structure:

```
src/alphawallets/pipeline/pnl/
  __init__.py
  models.py                 # CostBasisLot, Realization, WalletPnL (Pydantic, frozen)
  cost_basis.py             # FIFO engine, pure functions
  transfer_treatment.py     # Classifier for transfer-in semantics
  airdrop_registry.py       # Hand-verified distribution contract addresses
  calculator.py             # Orchestrator
  writer.py                 # DuckDB writer for wallet_pnl
  __main__.py               # CLI: python -m alphawallets.pipeline.pnl
```

Sequence:

1. Models, airdrop registry and FIFO engine — all unit-testable in isolation, with no DB.
2. Transfer treatment classifier, handing transfers to the engine with the correct cost-basis signal.
3. Calculator orchestrator, reading swaps, transfers and prices and applying the ten decisions above.
4. Writer with the ADR 0009 schema-drift guard and `EXPECTED_COLUMNS` beside the DDL.
5. CLI plus a **hand spot-check**: pick three wallets, work their PnL by hand from a block explorer, and assert the computed figure matches within 1%. This is the step that validates methodology rather than code, and it should be treated as a gate rather than a formality — a passing test suite proves the engine does what it was told, while only the spot-check proves it was told the right thing.
6. Live run on the current cache, reporting flag distribution as well as PnL: how many wallets carry each caveat flag is itself a finding about how usable the leaderboard will be.

## References

- **ROADMAP Week 3** — the deliverables and the three decisions it expected during this week.
- **PR #22** (`feat/exploration-wallet-activity-proof`) — the three-way swaps × transfers × prices join this layer consumes.
- **PR #27** (fourth classification state) — why PnL must handle unpriceable events rather than assume a price.
- **PR #28** (AW_01 pool expansion) — gives MKR a verified pool, creating the first token with swap data and no price data.
- [ADR 0001](0001-airdrop-selection.md) — the tracked airdrops whose distribution contracts the registry needs, and the ARB and MORPHO scope notes in decision 4.
- [ADR 0004](0004-duckdb-cache.md) — DuckDB single-writer, which the calculator holds on the output table during materialization; also why amounts are VARCHAR.
- [ADR 0009](0009-duckdb-connection-and-schema-conventions.md) — the schema-drift guard applied to `wallet_pnl`, and the UTC session pin the window boundaries depend on.
- [ADR 0010](0010-defillama-historical-fallback.md) — when implemented, MKR becomes priceable and `has_unpriceable_events` clears for its traders.
- [ADR 0011](0011-chain-aware-backfill-parallelization.md) — per-chain execution and the `ATTACH` pattern for cross-chain aggregation.
- **CLAUDE.md §6** — pipeline-stage conventions, and why this is `pipeline/pnl/` rather than `AW_04`.
