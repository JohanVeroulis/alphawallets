# 01 — Wallet Universe

Queries that decide which wallets AlphaWallets looks at. Everything downstream inherits this selection, so the filters here matter more than any other step.

## What belongs here

- Candidate wallet identification on Ethereum and Base from in-scope pairs and tokens
- Activity aggregates per wallet: trade counts, volumes, active days over 30d/90d
- Exclusion lists: CEX hot wallets, bridge addresses, contracts
- MEV bot flagging (flagged, not excluded)

## Conventions

- `AW_XX_description.sql` naming and the standard header comment (CLAUDE.md Section 6)
- State the grain explicitly — usually one row per `(blockchain, wallet)`
- Keep exclusions as their own CTE with a comment saying why each rule exists
- Smart-wallet types (Safe, ERC-4337, Argent, Ambire) are kept, not excluded — they are often the wallets we want

Filter rules are provisional until validated in Week 1 with real data. See CLAUDE.md Section 9 and record the outcome in [../../docs/decisions/](../../docs/decisions/).
