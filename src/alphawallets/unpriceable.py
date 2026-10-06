"""Whether any configured price route can serve a token.

A token no route can serve is not missing a price because a backfill has not
run. It is missing one because no endpoint AW_03 can call carries the token, so
no amount of re-running will produce a row. That is a different fact from "not
fetched yet", and the pipeline classifies it separately — `unpriceable` rather
than `pending` — so an operator is not told to re-run a fetcher that cannot help.

The answer lives in the `token_price_route` table, written by AW_03's discovery
probe (ADR 0010). This module is the single read-side call site, which is what it
was built to be: it previously held a hand-maintained set, seeded with MKR, with
a docstring saying the authoritative answer belonged in the route cache once that
existed. It now does.

A token is unpriceable only when the cache **explicitly says so** — meaning both
routes were tried and neither returned a price. Every other state, including an
unprobed token, is not unpriceable: an unprobed token might be perfectly
servable, and guessing otherwise would suppress a real signal. The failure
direction matters here, because an `unpriceable` verdict silences the
`unavailable` warning that would otherwise tell an operator about a genuine
backfill gap.

MKR was the seed entry and is no longer unpriceable: `/chart` does not carry it,
but `/prices/historical` does, so its cached route is 'historical' and its events
are priced.
"""

from __future__ import annotations

from duckdb import DuckDBPyConnection

from alphawallets.config import Chain
from alphawallets.fetchers.prices import route_cache


def is_unpriceable(
    conn: DuckDBPyConnection,
    chain: Chain,
    token_address: str,
) -> bool:
    """Return True when no configured price route can serve this token.

    Args:
        conn: Open DuckDB connection to the cache.
        chain: Chain the token lives on.
        token_address: Token contract, any case.

    Returns:
        True only when the route cache records 'unpriceable' for this
        (chain, token). False for a servable token, and False for an unprobed
        one — absence of a verdict is not a verdict.
    """
    return route_cache.get_route(conn, chain, token_address) == "unpriceable"
