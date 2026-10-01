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
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = [_response(p) for p in pages]
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
        assert client.get.call_count == 2
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
