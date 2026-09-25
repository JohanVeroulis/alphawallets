# ERC-20 fetchers

Generic ERC-20 activity — Transfer events and Transfers API queries — for tokens in V1 scope and for tracking airdrop distributions.

## Scope

- Tracked DeFi tokens (Section 2 of CLAUDE.md): UNI, AAVE, LDO, PENDLE, CRV, ENA, MKR, MORPHO, LINK, ARB
- Tracked airdrops (Section 2 of CLAUDE.md): UNI, ARB, ENA, EIGEN, MORPHO, ETHFI

Airdrop-sourced balances must be traceable back to the original distribution transactions so PnL can separate them from purchased balances.

## Alchemy method choice

Two options, decided per fetcher module:
- **Transfers API** — pre-decoded, simpler, cheaper on CUs
- **`eth_getLogs`** — raw Transfer events, needed when we care about protocol-specific context around the transfer

First fetcher (Week 1) establishes the trade-off.
