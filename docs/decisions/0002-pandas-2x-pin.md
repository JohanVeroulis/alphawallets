# 0002 — Pin pandas to 2.x for V1

- **Status:** Accepted
- **Date:** 2026-09-23

## Context

The first `uv sync` resolved `pandas>=2.2` to **pandas 3.0.6**, because the dependency had no upper bound. That was not a deliberate choice, and pandas 3.0 is a major release with breaking changes:

- Copy-on-write is the default, so chained assignment and in-place mutation behave differently
- Stricter dtype behaviour, including string dtype changes
- Legacy API methods removed

Three things make 3.x the wrong default for this project right now:

1. Practically every tutorial, Stack Overflow answer and community example currently assumes 2.x. Debugging against 3.0 means debugging without a map.
2. The project owner has extensive pandas 2.x experience from Databricks work. That experience transfers directly to 2.x and only partly to 3.x.
3. The V1 timeline is 5–6 weeks. It has no budget for pandas migration issues, which would compete with the actual problem — getting PnL right.

## Decision

Pin pandas to `>=2.2,<3` in `pyproject.toml`.

This applies to V1 only. It is a schedule decision, not a judgement about pandas 3.x.

Resolved version after the pin: **pandas 2.3.3**.

## Alternatives Considered

| Alternative | Why not |
|---|---|
| Allow pandas 3.x | Timeline pressure, plus a learning cost that buys nothing for V1. Any hour spent on copy-on-write semantics is an hour not spent on PnL correctness. |
| Pin an exact version (`pandas==2.2.3`) | Too restrictive. A range still admits security and bug-fix releases within 2.x, and `uv.lock` already pins the exact version for reproducibility. |

## Consequences

**What we gain**
- V1 development uses familiar, well-documented patterns
- `uv.lock` pins one exact 2.x version, so every environment matches
- Migrating to 3.x becomes a deliberate V2 task rather than an accident of dependency resolution

**What we lose / take on**
- We forgo pandas 3.x improvements, including its copy-on-write performance gains
- When we do migrate, every use of pandas needs review for copy-on-write compatibility. The longer 2.x-era code accumulates, the larger that review gets.
- A future dependency could require pandas 3.x and force the question early

**Revisit when**
- V2 planning starts, or
- A critical dependency requires pandas 3.x

Writing new code in a copy-on-write-safe style now (no chained assignment, explicit `.copy()`) keeps that future migration small at no cost today.
