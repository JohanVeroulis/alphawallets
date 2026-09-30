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
