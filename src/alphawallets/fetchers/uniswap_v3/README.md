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

## V1 pools

One primary pool per tracked token, configured in `DEFAULT_ETHEREUM_POOLS` and `DEFAULT_BASE_POOLS` (indexed together by `DEFAULT_POOLS_BY_CHAIN`). The matching token-slot entries live in `pipeline/exploration/queries.py` `POOL_TOKEN_LAYOUT`; both are hand-written so a reviewer sees either side change.

| Token | Chain | Fee | Pool |
|---|---|---|---|
| UNI | ethereum | 0.3% | `0x1d42064fc4beb5f8aaf85f4617ae8b3b5b8bd801` |
| AAVE | ethereum | 0.3% | `0x5ab53ee1d50eef2c1dd3d5402789cd27bb52c1bb` |
| LDO | ethereum | 0.3% | `0xa3f558aebaecaf0e11ca4b2199cc5ed341edfd74` |
| PENDLE | ethereum | 0.3% | `0x57af956d3e2cca3b86f3d8c6772c03ddca3eaacb` |
| CRV | ethereum | 0.3% | `0x919fa96e88d67499339577fa202345436bcdaf79` |
| ENA | ethereum | 0.3% | `0xc3db44adc1fcdfd5671f555236eae49f4a8eea18` |
| MKR | ethereum | 0.3% | `0xe8c6c9227491c0a8156a0106a0204d881bb7e531` |
| MORPHO | ethereum | **1%** | `0x25b96761e765b9ac20db18fa57fa91e3b617ec6f` |
| LINK | ethereum | 0.3% | `0xa6cc3c2531fdaa6ae1a3ca84c2855806728693e8` |
| ARB | ethereum | 0.3% | `0x59354356ec5d56306791873f567d61ebf11dfbd5` |
| EIGEN | ethereum | 0.3% | `0xc2c390c6cd3c4e6c2b70727d35a45e8a072f18ca` |
| ETHFI | ethereum | 0.3% | `0x06f00544c0bc62e6db10f46d370dfccdc23d8189` |
| USDC | ethereum | 0.05% | `0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640` (reference pool) |
| UNI | base | **1%** | `0xab365f161dd501473a1ff0d2ef0dce94e7398839` |
| AAVE | base | 0.3% | `0x2e86514cfd61fb19c5cf2b879d536d273d6e693d` |
| CRV | base | **1%** | `0x330e535c40eb49cc186496f061052fcf814d68cb` |
| PENDLE | base | 0.3% | `0xd7042869277c75ca56f1f6cc7e18ff0d83410dee` |
| MORPHO | base | 0.3% | `0x2f42df4af5312b492e9d7f7b2110d9c7bf2d9e4f` |
| LINK | base | 0.3% | `0x224a5d3f2155f2f85af70b6d72aea61a15273ff4` |

Base coverage follows the token registry: only the six V1 tokens with a verified Base address have pools.

### Pair policy

**TOKEN/WETH throughout.** The USDC fallback the selection rules allow was never needed — every V1 token has a WETH pair on every chain it exists on. All sides are 18 decimals except the USDC reference pool, so there is no scaling asymmetry to reason about.

### Fee tier policy

**0.3% by default**, overridden only where another tier measured deeper. Three exceptions:

```
MORPHO/ethereum   1%  ~2x the 0.3% pool
UNI/base          1%  the 0.3% pool exists but is ~25,000x shallower
CRV/base          1%  ~60x the 0.3% pool
```

Selection used the pool's `liquidity()`, which is valid **only** for comparing fee tiers of the same pair — see the methodology caveat above `POOL_TOKEN_LAYOUT` for why cross-pair comparisons are meaningless.

### Verifying a new pool

1. `getPool(tokenA, tokenB, fee)` on the factory — Ethereum `0x1F98431c8aD98523631AE4a59f267346ea31F984`, Base `0x33128a8fC17869897dcE68Ed026d694621f6FDfD`.
2. On the returned pool: `token0()`, `token1()`, `fee()`.
3. Re-verify after transcribing into both dicts. That is where a typo enters, and it is the one step easy to skip.
4. Smoke-test the pool and confirm swaps decode.

**Specify smoke-test windows in time, not blocks, when comparing pool activity across chains.** 100 blocks is ~20 min on Ethereum but only ~3.3 min on Base, so a direct block-count comparison produces false negatives. This is not hypothetical: EIGEN/ethereum reported **0 swaps** at 100 blocks and **32 swaps** over 600 blocks (~2h). The pool was active all along; the window was too short to see it.

### Known quiet pools

**PENDLE/base** — pool configured and verified, but the market is currently quiet: zero swaps over a 1,200-block (~40 min) probe, consistent with it being the shallowest pool in the set by roughly five orders of magnitude. Zero rows cost nothing, so it stays configured; expect activity to grow with Base TVL.

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
