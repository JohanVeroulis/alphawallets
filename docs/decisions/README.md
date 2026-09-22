# Architecture Decision Records

This folder records significant decisions: what we chose, why, and what we gave up. The goal is that a future reader (or a future Claude Code session) doesn't have to re-argue a settled question.

## Conventions

- **File name:** `NNNN-kebab-case-title.md`, where `NNNN` is a zero-padded sequential number (`0001`, `0002`, …). Numbers are never reused.
- **Sections:** Title, Status, Date, Context, Decision, Alternatives Considered, Consequences.
- **Status:** `Proposed` → `Accepted`. Later it can become `Superseded by NNNN` or `Deprecated`.
- **ADRs don't change.** To change a decision, write a new ADR and mark the old one `Superseded by NNNN`. Fixing typos and adding links is fine.
- **Keep them short.** One page is enough. Link to queries or data that back the decision.

## When to write one

- Choosing between tools, data sources, or storage (e.g. DuckDB vs. SQLite)
- Methodology decisions: PnL calculation, category rules, wallet filters
- Scope changes to V1

## Index

| # | Title | Status |
|---|---|---|
| [0001](0001-airdrop-selection.md) | V1 Airdrop Selection | Accepted |
