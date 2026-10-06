"""Tests for the route-less token check, now backed by the route cache."""

import pytest

from alphawallets.db import connect
from alphawallets.fetchers.prices import route_cache
from alphawallets.unpriceable import is_unpriceable

MKR = "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2"
UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"


@pytest.fixture
def conn():
    with connect(":memory:") as c:
        route_cache.create_tables(c)
        yield c


class TestIsUnpriceable:
    def test_explicit_unpriceable_verdict_is_true(self):
        """The only state that counts: both routes tried, neither worked."""
        with connect(":memory:") as c:
            route_cache.create_tables(c)
            route_cache.set_route(c, "ethereum", MKR, "unpriceable")
            assert is_unpriceable(c, "ethereum", MKR) is True

    def test_unprobed_token_is_not_unpriceable(self, conn):
        """Absence of a verdict is not a verdict.

        An unprobed token might be perfectly servable, and guessing otherwise
        would suppress the `unavailable` warning that reports a real backfill gap.
        """
        assert is_unpriceable(conn, "ethereum", UNI) is False

    @pytest.mark.parametrize("route", ["chart", "historical"])
    def test_servable_routes_are_not_unpriceable(self, conn, route):
        route_cache.set_route(conn, "ethereum", MKR, route)
        assert is_unpriceable(conn, "ethereum", MKR) is False

    def test_mkr_on_the_historical_route_is_priceable(self, conn):
        """The ADR 0010 outcome: MKR is absent from /chart but served per
        timestamp, so it is no longer unpriceable."""
        route_cache.set_route(conn, "ethereum", MKR, "historical")
        assert is_unpriceable(conn, "ethereum", MKR) is False

    def test_a_cleared_verdict_stops_being_unpriceable(self, conn):
        """clear_route is the re-probe escape hatch; the read side must follow."""
        route_cache.set_route(conn, "ethereum", MKR, "unpriceable")
        route_cache.clear_route(conn, "ethereum", MKR)
        assert is_unpriceable(conn, "ethereum", MKR) is False

    def test_scoped_per_chain(self, conn):
        """Coin ids are chain-prefixed, so coverage can differ per chain."""
        route_cache.set_route(conn, "ethereum", MKR, "unpriceable")
        assert is_unpriceable(conn, "base", MKR) is False

    def test_scoped_per_token(self, conn):
        route_cache.set_route(conn, "ethereum", MKR, "unpriceable")
        assert is_unpriceable(conn, "ethereum", UNI) is False

    def test_address_is_case_insensitive(self, conn):
        route_cache.set_route(conn, "ethereum", MKR, "unpriceable")
        assert is_unpriceable(conn, "ethereum", MKR.upper()) is True

    def test_returns_a_bool_not_a_truthy_value(self, conn):
        """Callers pass this straight into a Pydantic bool field."""
        route_cache.set_route(conn, "ethereum", MKR, "unpriceable")
        assert isinstance(is_unpriceable(conn, "ethereum", MKR), bool)
        assert isinstance(is_unpriceable(conn, "ethereum", UNI), bool)

    def test_empty_cache_means_nothing_is_unpriceable(self, conn):
        """A fresh cache must not classify every token as route-less."""
        for token in (MKR, UNI):
            for chain in ("ethereum", "base"):
                assert is_unpriceable(conn, chain, token) is False
