# 0008 — DefiLlama as V1 historical price source

- **Status:** Accepted
- **Date:** 2026-09-28

## Context

FIFO PnL (planned for Week 3 per ROADMAP) needs a historical USD price per trade timestamp for every token a wallet buys or sells. ADR 0003 named DefiLlama as the primary candidate but didn't verify coverage; that verification was tracked as the final "TBD Week 1" marker in CLAUDE.md Section 9.

The verification: `src/alphawallets/pipeline/exploration/defillama_coverage.py` probes DefiLlama's free `/prices/historical` endpoint for the union of V1 tracked tokens (6 airdrops + 10 DeFi, deduped to 12 unique symbols, with ARB probed on both its Arbitrum-native and Ethereum-bridged forms) at 4 dates spanning a full year:

- Yesterday
- 30 days ago
- 90 days ago (V1's outer PnL window)
- 1 year ago

Result (2026-09-28): 52 / 52 probes returned prices. 100% coverage across all tested tokens and dates. Median latency 301 ms, p95 371 ms.

Two findings that shape follow-on work:

1. ARB prices are identical on both chains — DefiLlama treats the bridged Ethereum token (0xb50721bcf8d664c30412cfbc6cf7a15145234ad1) and the Arbitrum-native token (0x912ce59144191c1204e64559fe8253a0e49e6548) as the same asset, to the cent, at every date. The V1 scope choice to track ARB post-bridge (per ADR 0001) costs nothing in price accuracy.
2. MORPHO required the correct address to resolve. The original non-transferable MORPHO token (0x9994e35db50125e0df82e4c2dde62496ce330999) has no market price. The transferable MORPHO deployed in late 2024 (0x58d97b57bb95320f9a05dc918aef65434969c2b2) is the one with a price feed. Any code that stores or queries MORPHO prices must use the transferable address.

## Decision

Adopt DefiLlama as the sole historical price source for V1 tracked tokens. No CoinGecko fallback is provisioned in V1 because the coverage measurement shows no gap to fill.

Concrete rules:

1. Store the transferable MORPHO contract (0x58d97b57...) in the token address registry — see `data/known_airdrops.json` and any future DeFi-token registry.
2. Query ARB prices with the Ethereum-side coin_id (`ethereum:0xb50721bcf8d664c30412cfbc6cf7a15145234ad1`) to keep the pricing pipeline chain-consistent with the rest of the V1 fetchers.
3. Historical price integration lands in the Week 3 PnL work as a DuckDB table joinable to swap timestamps.

## Alternatives Considered

**CoinGecko as primary or co-primary.** Rejected. No missing token, higher latency in practice, an API key needed for anything useful, and rate limits that are tighter than DefiLlama's on the free tier. Kept in reserve for tokens outside the V1 tracked set if they surface.

**Derive USD prices from DEX pool swap rates at trade time.** Rejected. Requires per-swap on-chain math (sqrt price × 2^96, token decimals, cross-pool triangulation for non-USD-paired tokens), and only works for tokens with active DEX liquidity at the exact timestamp. Higher accuracy at a much higher engineering cost; deferred until a specific use case demands intra-day precision.

## Consequences

**What we gain:**
- Zero cost, zero credential, one HTTP endpoint. The simplest possible integration for a hard prerequisite.
- Verified coverage: 100% across a full year for every token V1 tracks.
- Consistent pricing across chains via the ARB parity, which means the V1 scope decision doesn't leak into pricing complexity.

**What we lose / take on:**
- Coverage is verified only for V1 tracked tokens. Wallets in the discovered universe will hold tokens outside this list, and those are exactly where DefiLlama coverage is likelier to thin. When AW_02 (ERC-20 transfers) starts surfacing arbitrary tokens, a coverage check per token before PnL attribution is prudent, with CoinGecko as the fallback for the tail.
- Latency at ~300 ms per lookup. Per-trade PnL over 1,000+ swaps per pool needs the batch endpoint (DefiLlama accepts comma-separated coin ids on `/prices/historical`). Batching strategy needs to check DefiLlama's rate limits before concentrating calls — the same CUPS lesson as ADR 0007, applied to a different provider.
- The MORPHO address gotcha is a footgun. Any future code or registry that carries a MORPHO address must use the transferable one; a check in the price fetcher for the legacy address would catch a common mistake.
