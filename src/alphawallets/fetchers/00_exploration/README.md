# 00 — Exploration

Ad-hoc queries written to understand the data: what Dune tables contain, how well Base is covered, how values are distributed. Nothing here runs in the pipeline.

## What belongs here

- Throwaway queries worth keeping because of what they revealed
- Data-quality checks: coverage, nulls, duplicates, suspicious outliers
- Sanity checks against block explorers

## Conventions

- Same `AW_XX_description.sql` naming and header comment as the rest of `queries/` (CLAUDE.md Section 6)
- Add a `-- Finding:` line to the header when a query settled a question — that is the reason to keep it
- A query that graduates into the pipeline moves to the folder for its stage and keeps its number

If a finding changes a project decision, write it up in [../../docs/decisions/](../../docs/decisions/) rather than leaving it in a comment here.
