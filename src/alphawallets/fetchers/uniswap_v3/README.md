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

## Resume mode

```bash
uv run python -m alphawallets.fetchers.uniswap_v3.aw_01_uniswap_v3_swaps \
    --chain ethereum \
    --pool 0x1d42064Fc4Beb5F8aAF85F4617AE8b3b5B8Bd801 \
    --resume
```

Continues from `MAX(block_number)` in `uniswap_v3_swap` for that `(chain, pool_address)`, up to the current head. The resume point comes from the **decoded** table, not the raw one: raw rows can include logs the pipeline later excluded as reorged, so decoded is the pipeline's source of truth (CLAUDE.md §6).

Scoped per pool, so backfilling one pool does not make another appear up to date. When nothing is stored for that `(chain, pool)` it logs at INFO and falls back to the default 1000-block window. Mutually exclusive with `--blocks` and `--from-block`/`--to-block`.

The range starts at `MAX + 1` so the stored block is not re-fetched. An off-by-one here would waste one block of work at most — `INSERT OR IGNORE` makes overlap harmless — so it is not a correctness concern.
