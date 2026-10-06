"""Tests for the AW_03 orchestrator.

The HTTP client is mocked; DuckDB is real but in-memory-per-file (tmp_path), so
the writer's PK behaviour is exercised for real rather than stubbed.

Expected counts are computed from the fixtures rather than hardcoded, so a test
can't hold two mutually inconsistent expectations.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import pytest

from alphawallets.db import connect
from alphawallets.fetchers.prices import route_cache
from alphawallets.fetchers.prices.aw_03_defillama_historical_prices import (
    FetchResult,
    fetch_and_persist_prices,
)
from alphawallets.fetchers.prices.mapper import align_to_hour

UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
CID = f"ethereum:{UNI}"


# ---------- Helpers ----------


def _response(entries: list[dict]) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.headers = {}
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "coins": {
            CID: {
                "symbol": "UNI",
                "decimals": 18,
                "confidence": 0.99,
                "prices": entries,
            }
        }
    }
    return resp


def _client(*pages: list[dict]) -> MagicMock:
    """A client that serves the given pages, then repeats the last one.

    The orchestrator now probes /chart for route coverage before fetching
    (ADR 0010), so the call count is one higher than the page count. Repeating
    the final page rather than exhausting keeps these tests about mapping,
    collisions and idempotency instead of about the probe — the probe has its own
    tests in TestRouteSelection.
    """
    client = MagicMock(spec=httpx.Client)
    responses = [_response(p) for p in pages]

    def _get(*_args, **kwargs):
        # The probe asks for span=2; serve it the first page so it verdicts
        # 'chart', then hand out the pages in order for the real fetch.
        if kwargs.get("params", {}).get("span") == 2:
            return responses[0]
        if _get.index < len(responses):
            response = responses[_get.index]
            _get.index += 1
            return response
        return responses[-1]

    _get.index = 0
    client.get.side_effect = _get
    return client


def _points(*specs: tuple[str, float]) -> list[dict]:
    """Build API entries from (iso timestamp, price) pairs."""
    return [
        {"timestamp": int(datetime.fromisoformat(iso).timestamp()), "price": price}
        for iso, price in specs
    ]


def _expected_written(entries: list[dict]) -> int:
    """Distinct hour buckets across the entries — what the writer should insert."""
    return len({align_to_hour(e["timestamp"]) for e in entries})


# ---------- Happy path ----------


class TestFetchAndPersistPrices:
    def test_returns_fetch_result(self, tmp_path):
        entries = _points(
            ("2026-09-30T05:10:00+00:00", 8.80),
            ("2026-09-30T06:10:00+00:00", 8.85),
        )
        result = fetch_and_persist_prices(
            chain="ethereum",
            token_address=UNI,
            span_days=1,
            db_path=tmp_path / "p.duckdb",
            client=_client(entries),
        )
        assert isinstance(result, FetchResult)
        assert result.chain == "ethereum"
        assert result.token_address == UNI
        assert result.span_days == 1
        assert result.period == "1h"

    def test_counters_match_fixture(self, tmp_path):
        entries = _points(
            ("2026-09-30T05:10:00+00:00", 8.80),
            ("2026-09-30T06:10:00+00:00", 8.85),
            ("2026-09-30T07:10:00+00:00", 8.90),
        )
        expected = _expected_written(entries)

        result = fetch_and_persist_prices(
            chain="ethereum",
            token_address=UNI,
            span_days=1,
            db_path=tmp_path / "p.duckdb",
            client=_client(entries),
        )

        assert result.raw_points_seen == len(entries)
        assert result.mapped == len(entries)
        assert result.written == expected
        assert result.duplicate_after_alignment == len(entries) - expected

    def test_rows_land_in_db(self, tmp_path):
        db = tmp_path / "p.duckdb"
        entries = _points(
            ("2026-09-30T05:10:00+00:00", 8.80),
            ("2026-09-30T06:10:00+00:00", 8.85),
        )
        fetch_and_persist_prices(
            chain="ethereum", token_address=UNI, span_days=1, db_path=db, client=_client(entries)
        )
        with connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM token_price").fetchone()[0] == 2
            assert (
                conn.execute("SELECT DISTINCT source FROM token_price").fetchone()[0] == "defillama"
            )

    def test_stored_timestamps_are_hour_aligned(self, tmp_path):
        db = tmp_path / "p.duckdb"
        entries = _points(("2026-09-30T05:49:50+00:00", 8.86))
        fetch_and_persist_prices(
            chain="ethereum", token_address=UNI, span_days=1, db_path=db, client=_client(entries)
        )
        with connect(db) as conn:
            stored = conn.execute("SELECT ts FROM token_price").fetchone()[0]
        assert stored == datetime(2026, 9, 30, 5, 0, tzinfo=UTC)

    def test_chunks_fetched_reflects_plan(self, tmp_path):
        """30 days hourly needs 2 chunks, so the client is called twice."""
        first = _points(("2026-09-01T00:10:00+00:00", 8.0))
        second = _points(("2026-09-16T00:10:00+00:00", 9.0))
        client = _client(first, second)

        result = fetch_and_persist_prices(
            chain="ethereum",
            token_address=UNI,
            span_days=30,
            db_path=tmp_path / "p.duckdb",
            client=client,
        )
        assert result.chunks_fetched == 2
        # 3 calls, not 2: the route-coverage probe (ADR 0010) precedes the two
        # chunk fetches. chunks_fetched counts chunks, not HTTP calls.
        assert client.get.call_count == 3
        assert result.raw_points_seen == len(first) + len(second)

    def test_address_lowercased_in_result(self, tmp_path):
        entries = _points(("2026-09-30T05:10:00+00:00", 8.80))
        result = fetch_and_persist_prices(
            chain="ethereum",
            token_address=UNI.upper().replace("0X", "0x"),
            span_days=1,
            db_path=tmp_path / "p.duckdb",
            client=_client(entries),
        )
        assert result.token_address == UNI

    def test_empty_response_writes_nothing(self, tmp_path):
        result = fetch_and_persist_prices(
            chain="ethereum",
            token_address=UNI,
            span_days=1,
            db_path=tmp_path / "p.duckdb",
            client=_client([]),
        )
        assert (result.raw_points_seen, result.mapped, result.written) == (0, 0, 0)
        assert result.duplicate_after_alignment == 0


# ---------- The same-hour collision, end to end ----------


class TestSameHourCollision:
    def test_two_points_one_hour_yield_one_row(self, tmp_path):
        """The scenario the brief calls out: 2 raw points → 1 written, 1 duplicate."""
        entries = _points(
            ("2026-09-30T10:02:11+00:00", 8.80),
            ("2026-09-30T10:58:44+00:00", 8.95),
        )
        expected = _expected_written(entries)
        assert expected == 1, "fixture must put both points in one hour"

        result = fetch_and_persist_prices(
            chain="ethereum",
            token_address=UNI,
            span_days=1,
            db_path=tmp_path / "p.duckdb",
            client=_client(entries),
        )

        assert result.raw_points_seen == 2
        assert result.mapped == 2, "mapper must not deduplicate"
        assert result.written == 1
        assert result.duplicate_after_alignment == 1

    def test_first_observation_wins(self, tmp_path):
        db = tmp_path / "p.duckdb"
        entries = _points(
            ("2026-09-30T10:02:11+00:00", 8.80),
            ("2026-09-30T10:58:44+00:00", 8.95),
        )
        fetch_and_persist_prices(
            chain="ethereum", token_address=UNI, span_days=1, db_path=db, client=_client(entries)
        )
        with connect(db) as conn:
            stored = conn.execute("SELECT price_usd FROM token_price").fetchone()[0]
        assert stored == pytest.approx(8.80)

    def test_mixed_collision_and_distinct_hours(self, tmp_path):
        entries = _points(
            ("2026-09-30T10:02:00+00:00", 8.80),
            ("2026-09-30T10:58:00+00:00", 8.95),
            ("2026-09-30T11:05:00+00:00", 9.05),
        )
        expected = _expected_written(entries)

        result = fetch_and_persist_prices(
            chain="ethereum",
            token_address=UNI,
            span_days=1,
            db_path=tmp_path / "p.duckdb",
            client=_client(entries),
        )
        assert result.written == expected == 2
        assert result.duplicate_after_alignment == len(entries) - expected == 1


# ---------- Idempotency ----------


class TestIdempotency:
    def test_rerun_writes_nothing(self, tmp_path):
        db = tmp_path / "p.duckdb"
        entries = _points(
            ("2026-09-30T05:10:00+00:00", 8.80),
            ("2026-09-30T06:10:00+00:00", 8.85),
        )
        expected = _expected_written(entries)

        first = fetch_and_persist_prices(
            chain="ethereum", token_address=UNI, span_days=1, db_path=db, client=_client(entries)
        )
        second = fetch_and_persist_prices(
            chain="ethereum", token_address=UNI, span_days=1, db_path=db, client=_client(entries)
        )

        assert first.written == expected
        assert second.written == 0
        assert second.duplicate_after_alignment == expected
        with connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM token_price").fetchone()[0] == expected

    def test_rerun_does_not_overwrite_prices(self, tmp_path):
        db = tmp_path / "p.duckdb"
        original = _points(("2026-09-30T05:10:00+00:00", 8.80))
        changed = _points(("2026-09-30T05:40:00+00:00", 9.99))

        fetch_and_persist_prices(
            chain="ethereum", token_address=UNI, span_days=1, db_path=db, client=_client(original)
        )
        fetch_and_persist_prices(
            chain="ethereum", token_address=UNI, span_days=1, db_path=db, client=_client(changed)
        )

        with connect(db) as conn:
            stored = conn.execute("SELECT price_usd FROM token_price").fetchone()[0]
        assert stored == pytest.approx(8.80), "an existing hour must not be revised by a re-run"


# ---------- Validation and mapping failures ----------


class TestValidation:
    def test_zero_span_days_rejected_before_request(self, tmp_path):
        client = MagicMock(spec=httpx.Client)
        with pytest.raises(ValueError, match="span_days must be positive"):
            fetch_and_persist_prices(
                chain="ethereum",
                token_address=UNI,
                span_days=0,
                db_path=tmp_path / "p.duckdb",
                client=client,
            )
        client.get.assert_not_called()

    def test_unsupported_period_rejected_before_request(self, tmp_path):
        client = MagicMock(spec=httpx.Client)
        with pytest.raises(ValueError, match="Unsupported period"):
            fetch_and_persist_prices(
                chain="ethereum",
                token_address=UNI,
                span_days=1,
                period="7m",
                db_path=tmp_path / "p.duckdb",
                client=client,
            )
        client.get.assert_not_called()

    def test_unmappable_point_skipped_not_fatal(self, tmp_path):
        """A malformed entry is dropped; the rest of the span still lands."""
        good = _points(("2026-09-30T05:10:00+00:00", 8.80))
        bad = [{"timestamp": None, "price": 8.9}, {"timestamp": 1, "price": -5.0}]
        entries = good + bad

        result = fetch_and_persist_prices(
            chain="ethereum",
            token_address=UNI,
            span_days=1,
            db_path=tmp_path / "p.duckdb",
            client=_client(entries),
        )

        # The null-timestamp entry is dropped by the client (it sorts and
        # dedupes on that field); the negative price is dropped by the mapper.
        assert result.raw_points_seen == 2
        assert result.mapped == 1
        assert result.written == 1


class TestRouteSelection:
    """The ADR 0010 discovery flow: probe once, remember, then use the verdict."""

    @staticmethod
    def _empty_chart_client() -> MagicMock:
        """A client whose /chart responses never contain the coin."""
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.headers = {}
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"coins": {}}
        client = MagicMock(spec=httpx.Client)
        client.get.return_value = resp
        return client

    @staticmethod
    def _historical_client(price: float = 2014.54) -> MagicMock:
        """Empty on /chart, priced on /prices/historical — the MKR shape."""
        client = MagicMock(spec=httpx.Client)

        def _get(url, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.headers = {}
            resp.raise_for_status.return_value = None
            if "/prices/historical/" in url:
                ts = int(url.split("/historical/")[1].split("/")[0])
                resp.json.return_value = {
                    "coins": {
                        CID: {
                            "price": price,
                            "timestamp": ts,
                            "symbol": "UNI",
                            "decimals": 18,
                            "confidence": 0.99,
                        }
                    }
                }
            else:
                resp.json.return_value = {"coins": {}}
            return resp

        client.get.side_effect = _get
        return client

    def test_covered_token_caches_chart_and_uses_it(self, tmp_path):
        db = tmp_path / "p.duckdb"
        result = fetch_and_persist_prices(
            chain="ethereum",
            token_address=UNI,
            span_days=1,
            db_path=db,
            client=_client(_points(("2026-09-30T05:10:00+00:00", 8.80))),
        )
        assert result.route == "chart"
        assert result.route_probed is True
        with connect(db) as conn:
            assert route_cache.get_route(conn, "ethereum", UNI) == "chart"

    def test_cached_verdict_skips_the_probe(self, tmp_path):
        """The probe costs one request per token ever, not per run."""
        db = tmp_path / "p.duckdb"
        with connect(db) as conn:
            route_cache.create_tables(conn)
            route_cache.set_route(conn, "ethereum", UNI, "chart")

        client = _client(_points(("2026-09-30T05:10:00+00:00", 8.80)))
        result = fetch_and_persist_prices(
            chain="ethereum", token_address=UNI, span_days=1, db_path=db, client=client
        )
        assert result.route_probed is False
        # 1 call: the chunk fetch only, no probe.
        assert client.get.call_count == 1

    def test_uncovered_token_falls_back_to_historical(self, tmp_path, monkeypatch):
        """The MKR case end to end: absent from /chart, priced per timestamp."""
        monkeypatch.setattr("time.sleep", lambda _s: None)
        db = tmp_path / "p.duckdb"
        result = fetch_and_persist_prices(
            chain="ethereum",
            token_address=UNI,
            span_days=1,
            db_path=db,
            client=self._historical_client(),
        )
        assert result.route == "historical"
        assert result.written == 24  # one per hour of the span
        with connect(db) as conn:
            assert route_cache.get_route(conn, "ethereum", UNI) == "historical"

    def test_fallback_rows_record_their_route(self, tmp_path, monkeypatch):
        """Provenance is answerable after the fact."""
        monkeypatch.setattr("time.sleep", lambda _s: None)
        db = tmp_path / "p.duckdb"
        fetch_and_persist_prices(
            chain="ethereum",
            token_address=UNI,
            span_days=1,
            db_path=db,
            client=self._historical_client(),
        )
        with connect(db) as conn:
            routes = conn.execute("SELECT DISTINCT route FROM token_price").fetchall()
        assert routes == [("historical",)]

    def test_chart_rows_record_their_route(self, tmp_path):
        db = tmp_path / "p.duckdb"
        fetch_and_persist_prices(
            chain="ethereum",
            token_address=UNI,
            span_days=1,
            db_path=db,
            client=_client(_points(("2026-09-30T05:10:00+00:00", 8.80))),
        )
        with connect(db) as conn:
            routes = conn.execute("SELECT DISTINCT route FROM token_price").fetchall()
        assert routes == [("chart",)]

    def test_both_routes_empty_caches_unpriceable(self, tmp_path, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _s: None)
        db = tmp_path / "p.duckdb"
        result = fetch_and_persist_prices(
            chain="ethereum",
            token_address=UNI,
            span_days=1,
            db_path=db,
            client=self._empty_chart_client(),
        )
        assert result.route == "unpriceable"
        assert result.written == 0
        with connect(db) as conn:
            assert route_cache.get_route(conn, "ethereum", UNI) == "unpriceable"

    def test_cached_unpriceable_fetches_nothing(self, tmp_path):
        """No probe, no fallback walk — the expensive path is not re-paid."""
        db = tmp_path / "p.duckdb"
        with connect(db) as conn:
            route_cache.create_tables(conn)
            route_cache.set_route(conn, "ethereum", UNI, "unpriceable")

        client = _client(_points(("2026-09-30T05:10:00+00:00", 8.80)))
        result = fetch_and_persist_prices(
            chain="ethereum", token_address=UNI, span_days=1, db_path=db, client=client
        )
        assert result.route == "unpriceable"
        assert result.written == 0
        assert client.get.call_count == 0

    def test_a_cached_historical_verdict_is_not_re_probed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _s: None)
        db = tmp_path / "p.duckdb"
        with connect(db) as conn:
            route_cache.create_tables(conn)
            route_cache.set_route(conn, "ethereum", UNI, "historical")

        client = self._historical_client()
        result = fetch_and_persist_prices(
            chain="ethereum", token_address=UNI, span_days=1, db_path=db, client=client
        )
        assert result.route_probed is False
        # 24 historical calls for a 1-day span, and no /chart probe among them.
        assert client.get.call_count == 24
        assert all("/prices/historical/" in c.args[0] for c in client.get.call_args_list)
