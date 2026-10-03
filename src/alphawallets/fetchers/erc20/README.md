# ERC-20 fetchers

Generic ERC-20 activity — Transfer events and Transfers API queries — for tokens in V1 scope and for tracking airdrop distributions.

## Scope

- Tracked DeFi tokens (Section 2 of CLAUDE.md): UNI, AAVE, LDO, PENDLE, CRV, ENA, MKR, MORPHO, LINK, ARB
- Tracked airdrops (Section 2 of CLAUDE.md): UNI, ARB, ENA, EIGEN, MORPHO, ETHFI

Airdrop-sourced balances must be traceable back to the original distribution transactions so PnL can separate them from purchased balances.

## Alchemy method choice

**Decided (AW_02, PR #20): the Transfers API (`alchemy_getAssetTransfers`) for bulk ERC-20 backfill.** It has no `eth_getLogs` 10-block window cap (see [ADR 0006](../../../../docs/decisions/0006-alchemy-eth-getlogs-block-window.md)), returns pre-decoded entries, and paginates by page key rather than block range — up to 1000 entries per call.

`eth_getLogs` stays the route for protocol-specific event decoding, where the raw log and its ABI are the point (AW_01's Uniswap V3 Swap).

**Empirical note:** for ERC-20 category, Alchemy's Transfers API returns `logIndex: null` for every result in our verification runs. This makes `unique_id` the mandatory dedup key — a raw-log-style `(chain, tx_hash, log_index)` PK would collapse legitimate distinct transfers. Recorded here so future fetchers targeting the same API don't re-discover it.

## Modules

| Module | Role |
|---|---|
| `models.py` | `RawAssetTransfer` (verbatim-shaped) and `ERC20Transfer` (normalized) |
| `client.py` | One `alchemy_getAssetTransfers` call per page; returns `(transfers, next_page_key)` |
| `mapper.py` | Pure entry → model mapping; absorbs hex fields, ISO timestamps, missing values |
| `writer.py` | DuckDB DDL and idempotent writes to `raw_erc20_transfer` / `erc20_transfer` |
| `aw_02_erc20_transfers.py` | Orchestrator (`fetch_and_persist_erc20_transfers`) and CLI |

## Tables

- `raw_erc20_transfer` — audit trail, PK `(chain, unique_id)`
- `erc20_transfer` — normalized pipeline input, PK `(chain, unique_id)`

Entries that map but lack `metadata.blockTimestamp` are written to the raw table only; the decoded table requires a timestamp because every downstream consumer keys on time. `FetchResult.decoded_skipped_missing_timestamp` reports the count.

## Resume mode

```bash
uv run python -m alphawallets.fetchers.erc20.aw_02_erc20_transfers \
    --chain ethereum \
    --contract 0x1f9840a85d5af5bf1d1762f925bdaddc4201f984 \
    --resume
```

Continues from `MAX(block_number)` in `erc20_transfer` for that `(chain, token_address)`, up to the current head. Reads the **decoded** table, not the raw one — raw rows can include transfers the mapper dropped, so decoded is the pipeline's source of truth (CLAUDE.md §6).

Scoped per token, so one token's backfill does not make another look current. When nothing is stored it logs at INFO and falls back to the default 1000-block window. Mutually exclusive with `--blocks` and `--from-block`/`--to-block`.

## Multi-token

Name a token by symbol and let the V1 registry resolve it against `--chain`:

```bash
uv run python -m alphawallets.fetchers.erc20.aw_02_erc20_transfers \
    --chain base \
    --token-symbol LINK \
    --blocks 1000
```

`--token-symbol` accepts any of the twelve V1 symbols (`UNI`, `AAVE`, `LDO`, `PENDLE`, `CRV`, `ENA`, `MKR`, `MORPHO`, `LINK`, `ARB`, `EIGEN`, `ETHFI`) and resolves through [`tokens.py`](../../tokens.py), where every address was verified on-chain. Unknown symbols are rejected at parse time, so the error lists the valid set.

`--contract 0x...` still works unchanged and is still the way to fetch a token outside the registry. The two are mutually exclusive, and passing neither keeps the historical default of UNI on Ethereum.

### Measured backfill, 2026-10-03

A 1-day window across all 18 verified `(token, chain)` pairs: **18/18 OK, 142,210 transfers seen, 140,322 written, 151 pages, 1,149s total.**

Per-page latency splits sharply by chain — Base averages ~9.4s/page against Ethereum's ~3.4s/page at comparable page counts. Two Base pairs (MORPHO 375.8s / 34 pages, AAVE 280.7s / 30 pages) account for 57% of total runtime between them. Pagination is serial because each request needs the previous response's `pageKey`, so wall-clock scales with transfer volume rather than block count. Relevant when planning a 30-day backfill.

Not every token has a Base address. Six — LDO, ENA, MKR, ARB, EIGEN, ETHFI — are Ethereum-only in the registry, because no source was found whose Base address could be verified. Asking for one on Base fails with a message naming the chains that are available; it never falls back to the Ethereum address, which would quietly fetch the wrong chain's transfers. Absence means unverified, not nonexistent.
