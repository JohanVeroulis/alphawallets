"""Which price route serves each (chain, token), discovered and remembered.

DefiLlama's coverage is endpoint-specific: a coin served by `/prices/historical`
may be absent from `/chart` entirely (the ADR 0008 amendment, measured on MKR).
AW_03 fetches with `/chart` because V1's access pattern is bulk, so a coin
missing from that route needs the per-timestamp fallback — but probing for that
on every run would waste a request per token per run.

This table remembers the verdict. ADR 0010 is the design: discovery lives in the
fetcher rather than a config file, because the gap is a property of the provider
at a point in time, not of our token set. A new token, or a token that migrates
the way MKR did, is classified by code rather than by someone remembering to
edit a list.

Three verdicts:

- `chart`        — the bulk route serves it. The fast path, 2 requests for 30 days.
- `historical`   — absent from `/chart`, served per timestamp. ~720 requests for
                   the same span, so it is used only where it must be.
- `unpriceable`  — neither route returns a price. Nothing to fetch; the pipeline
                   classifies the token's events as `unpriceable` (PR #27).

A verdict is a snapshot, not a fact about the token forever, which is why
`last_verified` is stored and `clear_route` exists. A token that gains `/chart`
coverage keeps using the slow path until something re-probes it, and nothing here
notices that on its own.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

from duckdb import CatalogException, DuckDBPyConnection

from alphawallets.config import Chain
from alphawallets.db import assert_table_matches_ddl

logger = logging.getLogger(__name__)

PriceRoute = Literal["chart", "historical", "unpriceable"]

VALID_ROUTES: frozenset[str] = frozenset({"chart", "historical", "unpriceable"})


# ---------- Schema ----------


TOKEN_PRICE_ROUTE_DDL = """
CREATE TABLE IF NOT EXISTS token_price_route (
    chain           VARCHAR     NOT NULL,
    token_address   VARCHAR     NOT NULL,
    preferred_route VARCHAR     NOT NULL,
    last_verified   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (chain, token_address)
);
"""

# Expected shape of the table above, for the schema-drift guard in
# create_tables(). Hand-written next to the DDL rather than parsed out of it: a
# parser would happily agree with a typo in both, whereas a reviewer changing one
# and not the other is exactly the drift this catches (ADR 0009).
TOKEN_PRICE_ROUTE_COLUMNS: list[tuple[str, str]] = [
    ("chain", "VARCHAR"),
    ("token_address", "VARCHAR"),
    ("preferred_route", "VARCHAR"),
    ("last_verified", "TIMESTAMPTZ"),
]


def create_tables(conn: DuckDBPyConnection) -> None:
    """Create token_price_route if absent, then check it against the DDL.

    Safe to call every run. The drift check is the ADR 0009 pattern: CREATE TABLE
    IF NOT EXISTS does nothing to an existing table, so a cache file written
    before a column was added keeps the old shape and fails later inside a write.
    """
    conn.execute(TOKEN_PRICE_ROUTE_DDL)
    assert_table_matches_ddl(conn, "token_price_route", TOKEN_PRICE_ROUTE_COLUMNS)


# ---------- Reads ----------


def get_route(
    conn: DuckDBPyConnection,
    chain: Chain,
    token_address: str,
) -> PriceRoute | None:
    """Return the cached route for a (chain, token), or None when unprobed.

    None means "no verdict yet", which is what triggers discovery. It is
    deliberately distinct from 'unpriceable', which means "probed, and neither
    route works" — conflating them would re-probe a known-dead token every run.

    Args:
        conn: Open DuckDB connection.
        chain: Chain name.
        token_address: Token contract, any case.

    Returns:
        'chart', 'historical', 'unpriceable', or None.
    """
    try:
        row = conn.execute(
            "SELECT preferred_route FROM token_price_route "
            "WHERE chain = ? AND lower(token_address) = ?",
            [chain, token_address.lower()],
        ).fetchone()
    except CatalogException:
        # The table does not exist yet, which happens on a cache where AW_03 has
        # not run since the route cache was introduced. "No verdict" is the
        # correct answer and matches the contract below — a pipeline stage should
        # not fail because a fetcher has not run.
        logger.debug(
            "token_price_route does not exist; treating %s:%s as unprobed", chain, token_address
        )
        return None
    if row is None:
        return None

    route = row[0]
    if route not in VALID_ROUTES:
        # A value outside the known set means the table was written by something
        # that does not share this module's vocabulary. Treating it as unprobed
        # re-discovers the truth rather than acting on a verdict we cannot read.
        logger.warning(
            "Ignoring unrecognised route %r for %s:%s; treating as unprobed",
            route,
            chain,
            token_address,
        )
        return None
    return route  # type: ignore[return-value]


def get_last_verified(
    conn: DuckDBPyConnection,
    chain: Chain,
    token_address: str,
) -> datetime | None:
    """Return when a (chain, token)'s route was last established, or None.

    Exposed so an operator or a future re-probe policy can find stale verdicts
    without reading the table directly.
    """
    row = conn.execute(
        "SELECT last_verified FROM token_price_route WHERE chain = ? AND lower(token_address) = ?",
        [chain, token_address.lower()],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return row[0].astimezone(UTC)


def all_routes(conn: DuckDBPyConnection) -> list[tuple[str, str, str, datetime]]:
    """Return every cached verdict, sorted. The operator's view of the cache.

    Returns:
        (chain, token_address, preferred_route, last_verified) tuples.
    """
    rows = conn.execute(
        "SELECT chain, token_address, preferred_route, last_verified "
        "FROM token_price_route ORDER BY chain, token_address"
    ).fetchall()
    return [(c, t, r, ts.astimezone(UTC)) for c, t, r, ts in rows]


# ---------- Writes ----------


def set_route(
    conn: DuckDBPyConnection,
    chain: Chain,
    token_address: str,
    route: PriceRoute,
    verified_at: datetime | None = None,
) -> None:
    """Record a route verdict, replacing any previous one.

    An upsert rather than INSERT OR IGNORE: unlike price rows, a verdict is
    mutable by design — a re-probe must be able to overwrite a stale answer, and
    silently keeping the old one is the failure mode this table exists to avoid.

    Args:
        conn: Open DuckDB connection.
        chain: Chain name.
        token_address: Token contract, any case. Stored lowercase.
        route: The verdict.
        verified_at: When it was established. Defaults to now.

    Raises:
        ValueError: On a route outside the known set, so a typo fails at the
            write rather than becoming an unreadable verdict in the table.
    """
    if route not in VALID_ROUTES:
        raise ValueError(f"Unknown route {route!r}. Valid routes: {sorted(VALID_ROUTES)}")

    timestamp = verified_at if verified_at is not None else datetime.now(tz=UTC)
    conn.execute(
        "INSERT OR REPLACE INTO token_price_route VALUES (?, ?, ?, ?)",
        [chain, token_address.lower(), route, timestamp],
    )
    logger.info("Route for %s:%s set to %s", chain, token_address.lower(), route)


def clear_route(conn: DuckDBPyConnection, chain: Chain, token_address: str) -> bool:
    """Forget a (chain, token)'s verdict so the next run re-probes it.

    The escape hatch for a stale verdict — a token that has gained or lost
    /chart coverage since it was last checked.

    Returns:
        True when a row was removed, False when there was nothing cached.
    """
    before = _row_count(conn)
    conn.execute(
        "DELETE FROM token_price_route WHERE chain = ? AND lower(token_address) = ?",
        [chain, token_address.lower()],
    )
    removed = before - _row_count(conn)
    if removed:
        logger.info("Cleared cached route for %s:%s", chain, token_address.lower())
    return removed > 0


def _row_count(conn: DuckDBPyConnection) -> int:
    """Return the current row count of token_price_route."""
    result = conn.execute("SELECT COUNT(*) FROM token_price_route").fetchone()
    assert result is not None, "COUNT(*) on token_price_route returned no rows"
    return int(result[0])
