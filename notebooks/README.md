# Notebooks

Jupyter notebooks for exploration: looking at distributions, checking PnL by hand for a few wallets, and prototyping logic before it becomes a module.

Notebooks are a scratchpad, not a deliverable. Anything that matters ends up as a query in [../queries/](../queries/), a module in [../src/alphawallets/](../src/alphawallets/), or a decision in [../docs/decisions/](../docs/decisions/).

## What belongs here

- Data exploration and distribution analysis that informs thresholds
- Manual verification of PnL against block explorers
- Prototypes of categorization rules before they harden

## Conventions

- `kebab-case.ipynb` names that say what the notebook explores, e.g. `pnl-fifo-sanity-check.ipynb`
- Clear all outputs before committing — they bloat diffs and can leak wallet data
- No secrets in cells; read `DUNE_API_KEY` from the environment
- A notebook that produces a finding gets a short summary at the top, so it can be read without running it
