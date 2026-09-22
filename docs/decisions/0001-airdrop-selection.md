# 0001 — V1 Airdrop Selection

- **Status:** Accepted
- **Date:** 2026-09-22

## Context

AlphaWallets ranks wallets by PnL. Airdrops distort that ranking: a wallet that received a large airdrop and sold it shows a big "profit" that came from eligibility farming, not from trading skill. Without special handling:

- Airdrop Hunters would crowd the Trader leaderboard.
- Tokens received for free would have a cost basis of zero, which inflates realized PnL under FIFO.
- Tokens that are both airdrops and tracked DeFi tokens (UNI, MORPHO) would mix airdropped and purchased balances.

So airdrops are tracked separately. Airdrop income is split out of trading/yield PnL and also feeds the Airdrop Hunter category. Every airdrop we include needs reliable claim data on V1 chains (Ethereum, Base). Otherwise its PnL attribution is wrong, and a wrong attribution is worse than none.

## Decision

V1 tracks six airdrops: **UNI, ARB, ENA, EIGEN, MORPHO, ETHFI**.

Selection criteria:
1. The claim (or, for ARB, the relevant post-claim activity) is observable on Ethereum.
2. The distribution happened on a single chain, so we don't need cross-chain wallet linking.
3. It moves wallet PnL significantly within the V1 universe.

| Token | Claim chain | Notes |
|---|---|---|
| UNI | Ethereum | Also a tracked DeFi token; airdropped and purchased balances must be kept apart |
| ARB | Arbitrum | **Exception:** the claim happened on Arbitrum. We track ARB only after it was bridged to Ethereum, and only that activity. The claim itself is not attributed. |
| ENA | Ethereum | |
| EIGEN | Ethereum | |
| MORPHO | Ethereum | Also a tracked DeFi token. Check in Week 3 whether any rewards were distributed on Base. |
| ETHFI | Ethereum | |

## Alternatives Considered

| Token(s) | Reason excluded |
|---|---|
| OP, STRK, ZK | Distributed on non-V1 chains (Optimism, Starknet, zkSync) |
| JUP, JITO, W | Solana-native; Solana support is planned for V1.5 |
| LAYER (Solayer) | Solana-native |
| ZRO (LayerZero) | Distributed across many EVM chains; attributing it requires cross-chain wallet linking (V1.5+) |

## Consequences

**What we gain**
- Airdrop-aware PnL is reliable for every token on the list, because each claim is either directly observable or explicitly scoped (ARB).
- No dependency on cross-chain linking or non-EVM data in V1.
- Airdrop Hunter detection rests on a small, well-defined set of events.

**What we lose**
- Wallets that farmed OP, STRK, ZK or the Solana airdrops look less like Airdrop Hunters than they are. When they sell those tokens on Ethereum, the proceeds may show up as ordinary transfers or trades.
- For ARB, the airdrop part of a wallet's PnL is incomplete: we see what happens to ARB after it's bridged, not the claim itself.

**What we revisit in V1.5**
- Add the Solana airdrops (JUP, JITO, W, LAYER) together with Solana support.
- Reconsider ZRO, OP, STRK and ZK once cross-chain wallet linking exists.
- Re-check the criteria when new major Ethereum/Base airdrops appear. Adding one requires a new ADR that supersedes this one.
