# Tests

The pytest suite for the Python pipeline. PnL correctness is the thing most worth testing: a silent rounding or cost-basis bug would quietly rank the wrong wallets.

## What belongs here

- Unit tests per module, mirroring `src/alphawallets/` (`test_pnl.py` for `pnl.py`, and so on)
- Hand-worked PnL fixtures: known trade sequences with the expected result computed by hand
- Edge cases: partial sells, re-buys, transfers in and out, zero-cost-basis (airdropped) tokens
- A smoke test for the end-to-end pipeline run

## Conventions

- `test_<module>.py` naming; test functions read as sentences: `test_fifo_handles_partial_sell`
- No network calls — Dune responses are fixtures. Anything hitting the live API is marked `@pytest.mark.integration` and skipped by default
- Fixtures use small, readable numbers; a test whose expected value can't be checked by hand isn't a test
- Run with `uv run pytest` before considering any change done (CLAUDE.md Section 7, principle 6)
