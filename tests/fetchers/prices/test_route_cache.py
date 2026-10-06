"""Tests for the price route cache (ADR 0010)."""

from datetime import UTC, datetime, timedelta

import pytest

from alphawallets.db import SchemaDriftError, _canonical_type, connect, live_columns
from alphawallets.fetchers.prices.route_cache import (
    TOKEN_PRICE_ROUTE_COLUMNS,
    VALID_ROUTES,
    all_routes,
    clear_route,
    create_tables,
    get_last_verified,
    get_route,
    set_route,
)

MKR = "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2"
UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


@pytest.fixture
def conn():
    with connect(":memory:") as c:
        create_tables(c)
        yield c


class TestSchema:
    def test_table_created(self, conn):
        names = [
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        ]
        assert "token_price_route" in names

    def test_idempotent(self, conn):
        create_tables(conn)
        create_tables(conn)

    def test_expected_columns_agree_with_the_ddl(self, conn):
        """Both are hand-written; this is what keeps them in step."""
        assert live_columns(conn, "token_price_route") == [
            (name, _canonical_type(type_name)) for name, type_name in TOKEN_PRICE_ROUTE_COLUMNS
        ]

    def test_drifted_table_raises(self):
        """ADR 0009's guard: an older-shaped table must fail loudly at startup."""
        with connect(":memory:") as c:
            c.execute(
                "CREATE TABLE token_price_route ("
                "chain VARCHAR, token_address VARCHAR, preferred_route VARCHAR)"
            )
            with pytest.raises(SchemaDriftError, match="last_verified"):
                create_tables(c)


class TestGetAndSet:
    def test_unprobed_token_is_none(self, conn):
        assert get_route(conn, "ethereum", MKR) is None

    @pytest.mark.parametrize("route", sorted(VALID_ROUTES))
    def test_roundtrip_every_route(self, conn, route):
        set_route(conn, "ethereum", MKR, route)
        assert get_route(conn, "ethereum", MKR) == route

    def test_address_stored_lowercase(self, conn):
        set_route(conn, "ethereum", MKR.upper(), "historical")
        stored = conn.execute("SELECT token_address FROM token_price_route").fetchone()[0]
        assert stored == MKR

    def test_lookup_is_case_insensitive(self, conn):
        set_route(conn, "ethereum", MKR, "historical")
        assert get_route(conn, "ethereum", MKR.upper()) == "historical"

    def test_set_overwrites_a_previous_verdict(self, conn):
        """A verdict is mutable by design — a re-probe must be able to correct it."""
        set_route(conn, "ethereum", MKR, "historical")
        set_route(conn, "ethereum", MKR, "chart")
        assert get_route(conn, "ethereum", MKR) == "chart"
        assert conn.execute("SELECT COUNT(*) FROM token_price_route").fetchone()[0] == 1

    def test_scoped_per_chain(self, conn):
        """Coin ids are chain-prefixed, so coverage can differ per chain."""
        set_route(conn, "ethereum", MKR, "historical")
        assert get_route(conn, "base", MKR) is None

    def test_scoped_per_token(self, conn):
        set_route(conn, "ethereum", MKR, "historical")
        assert get_route(conn, "ethereum", UNI) is None

    def test_unknown_route_rejected_at_write(self, conn):
        """A typo must fail here rather than become an unreadable verdict."""
        with pytest.raises(ValueError, match="Unknown route"):
            set_route(conn, "ethereum", MKR, "sometimes")

    def test_unrecognised_stored_route_reads_as_unprobed(self, conn):
        """Written by something with a different vocabulary: re-discover rather
        than act on a verdict we cannot interpret."""
        conn.execute(
            "INSERT INTO token_price_route VALUES (?, ?, ?, ?)",
            ["ethereum", MKR, "legacy-route", NOW],
        )
        assert get_route(conn, "ethereum", MKR) is None

    def test_unprobed_is_distinct_from_unpriceable(self, conn):
        """Conflating them would re-probe a known-dead token on every run."""
        assert get_route(conn, "ethereum", MKR) is None
        set_route(conn, "ethereum", MKR, "unpriceable")
        assert get_route(conn, "ethereum", MKR) == "unpriceable"


class TestLastVerified:
    def test_defaults_to_now(self, conn):
        before = datetime.now(tz=UTC)
        set_route(conn, "ethereum", MKR, "chart")
        stored = get_last_verified(conn, "ethereum", MKR)
        assert before <= stored <= datetime.now(tz=UTC)

    def test_explicit_timestamp_preserved(self, conn):
        set_route(conn, "ethereum", MKR, "chart", verified_at=NOW)
        assert get_last_verified(conn, "ethereum", MKR) == NOW

    def test_none_for_unprobed(self, conn):
        assert get_last_verified(conn, "ethereum", MKR) is None

    def test_result_is_utc(self, conn):
        set_route(conn, "ethereum", MKR, "chart", verified_at=NOW)
        assert get_last_verified(conn, "ethereum", MKR).utcoffset().total_seconds() == 0

    def test_overwrite_refreshes_the_timestamp(self, conn):
        old = NOW - timedelta(days=30)
        set_route(conn, "ethereum", MKR, "historical", verified_at=old)
        set_route(conn, "ethereum", MKR, "chart", verified_at=NOW)
        assert get_last_verified(conn, "ethereum", MKR) == NOW


class TestClearRoute:
    def test_removes_the_verdict(self, conn):
        set_route(conn, "ethereum", MKR, "historical")
        assert clear_route(conn, "ethereum", MKR) is True
        assert get_route(conn, "ethereum", MKR) is None

    def test_returns_false_when_nothing_cached(self, conn):
        assert clear_route(conn, "ethereum", MKR) is False

    def test_case_insensitive(self, conn):
        set_route(conn, "ethereum", MKR, "historical")
        assert clear_route(conn, "ethereum", MKR.upper()) is True

    def test_leaves_other_entries_alone(self, conn):
        set_route(conn, "ethereum", MKR, "historical")
        set_route(conn, "ethereum", UNI, "chart")
        clear_route(conn, "ethereum", MKR)
        assert get_route(conn, "ethereum", UNI) == "chart"

    def test_scoped_per_chain(self, conn):
        set_route(conn, "ethereum", MKR, "historical")
        assert clear_route(conn, "base", MKR) is False
        assert get_route(conn, "ethereum", MKR) == "historical"


class TestAllRoutes:
    def test_empty_cache(self, conn):
        assert all_routes(conn) == []

    def test_sorted_by_chain_then_token(self, conn):
        set_route(conn, "ethereum", UNI, "chart", verified_at=NOW)
        set_route(conn, "base", UNI, "chart", verified_at=NOW)
        set_route(conn, "ethereum", MKR, "historical", verified_at=NOW)
        rows = all_routes(conn)
        assert [(c, t) for c, t, _r, _ts in rows] == sorted((c, t) for c, t, _r, _ts in rows)
        assert rows[0][0] == "base"

    def test_carries_route_and_timestamp(self, conn):
        set_route(conn, "ethereum", MKR, "historical", verified_at=NOW)
        assert all_routes(conn) == [("ethereum", MKR, "historical", NOW)]


class TestMissingTable:
    """A pipeline stage must not fail because a fetcher has not run yet."""

    def test_get_route_on_a_cache_without_the_table_is_none(self):
        with connect(":memory:") as c:
            assert get_route(c, "ethereum", MKR) is None

    def test_that_is_the_same_answer_as_unprobed(self):
        """So downstream code needs no special case for an old cache file."""
        with connect(":memory:") as missing:
            without_table = get_route(missing, "ethereum", MKR)
        with connect(":memory:") as present:
            create_tables(present)
            with_table = get_route(present, "ethereum", MKR)
        assert without_table == with_table is None
