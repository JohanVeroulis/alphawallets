# PnL

FIFO cost-basis PnL calculation, airdrop-aware.

## Inputs

- Decoded swap tables from `fetchers/uniswap_v3/` (and other DEX fetchers as they land)
- ERC-20 Transfer tables from `fetchers/erc20/`
- Historical USD prices (source under investigation in Week 1 — DefiLlama primary candidate per ADR 0003)

## Outputs

Per-wallet PnL tables with airdrop-sourced balances tagged and separated from purchased balances.

## Methodology

- Realized PnL only for V1 (unrealized in V1.5)
- FIFO cost basis
- Airdrop cost basis = 0; distribution transactions are traced from `fetchers/erc20/` output
