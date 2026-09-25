# Fetchers

Raw data collection from Alchemy. Every fetcher module pulls events, transactions, or state from Alchemy JSON-RPC or the Transfers/Token APIs, decodes what it needs, and writes to DuckDB.

Fetchers are organized by **protocol or generic data type**, not by pipeline stage. A Uniswap V3 fetcher writes swap events; downstream `pipeline/` code reads them.

## Layout

Each subfolder holds fetchers for one protocol or data domain:

- `uniswap_v3/` — Swap events from Uniswap V3 pools on Ethereum and Base
- `erc20/` — Transfers API + `Transfer` events for tracked tokens

New protocols get their own subfolder as they are added (e.g. `aave/`, `compound/`, `morpho/`, `pendle/`, `uniswap_v2/`).

## Module naming

`AW_XX_description.py` — zero-padded sequential number, never reused. See CLAUDE.md Section 6 for the full convention, including the docstring header shape.
