# Exploration

Ad-hoc notebooks and scripts for understanding raw data before formalizing it into pipeline stages. Everything here is throwaway — nothing writes to production tables, nothing is imported by other pipeline stages.

Typical use: open DuckDB, poke at newly-fetched data, sketch analysis in pandas, decide whether it belongs in `pnl/`, `categorization/`, or `ranking/`.
