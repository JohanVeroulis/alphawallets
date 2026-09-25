# Uniswap V3 fetchers

Swap events from Uniswap V3 pools on Ethereum and Base.

## Scope

V1 targets the highest-volume pools for tracked pairs:
- ETH/USDC, ETH/USDT
- WBTC/USDC, WBTC/ETH
- Pools involving V1 DeFi tokens (UNI, AAVE, LDO, PENDLE, CRV, ENA, MKR, MORPHO, LINK, ARB)

## Output tables

Output table naming is finalized with the first fetcher (Week 1). Provisional:
- `raw_uniswap_v3_swap` — raw log rows
- `uniswap_v3_swap` — decoded rows joined with pool metadata
