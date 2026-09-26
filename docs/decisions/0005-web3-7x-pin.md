# 0005 — Pin web3.py to 7.x and eth-abi to 5.x for V1

- **Status:** Accepted
- **Date:** 2026-09-26

## Context

The first Uniswap V3 fetcher needs an Ethereum RPC client and an ABI decoder. The two natural choices are `web3.py` (the reference Python RPC client, since 2015) and its transitive dependency `eth-abi` (Ethereum ABI codec).

When adding the dependencies with `uv add web3 eth-abi`, uv resolved to the latest releases:
- **web3.py 8.0.0** — released mid-2026, a major release with breaking API changes vs 7.x
- **eth-abi 6.0.0** — matching major release aligned with web3.py 8.x

The owner has never worked with web3.py before. Learning material — tutorials, blog posts, Stack Overflow answers, ChatGPT training data through 2025 — nearly all targets web3.py 6.x or 7.x. Documentation coverage for 8.x is thin and community answers are sparse.

## Decision

Pin web3.py to `>=7.0,<8` and eth-abi to `>=5.0,<6` for V1. Also cap pydantic at `<3` as a safety net (same rationale, though pydantic 2.x is mature and 3.x is not yet visible on the horizon).

## Alternatives Considered

**Ship on web3.py 8.x with a `<9` cap.** The safety cap is correct, but the dev cost of building the first fetcher against a library with almost no external material is real, and adventurous version choices belong outside the initial build cycle.

**Ship on 8.x without a cap.** Nothing gained over the above, and a future web3.py 9.x could break V1 unannounced.

## Consequences

**What we gain:**
- Debugging the first fetcher works against the tutorial and reference material the owner will actually reach for.
- No mystery breakage from an 8.x API change nobody has written about yet.
- Consistent with ADR 0002 (pandas 2.x pin): during the initial build cycle, well-trodden versions beat freshness.

**What we lose / take on:**
- V1 ships on web3.py 7.x, one major behind the current release.
- An eventual upgrade to 8.x is deferred, not avoided. This will be a separate ADR when 8.x has more community coverage or when the fetchers hit an 8.x-only feature.
- The `<8` and `<6` caps live in `pyproject.toml`. Removing them is a one-line change plus a new ADR.
