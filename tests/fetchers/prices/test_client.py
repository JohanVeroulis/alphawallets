"""Tests for the DefiLlama /chart client. All HTTP is mocked."""

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from alphawallets.fetchers.prices import defillama_client as dc
from alphawallets.fetchers.prices.defillama_client import (
    MAX_POINTS_PER_REQUEST,
    coin_id,
    fetch_chart,
    fetch_chart_chunk,
    period_seconds,
)

UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
CID = f"ethereum:{UNI}"


# ---------- Helpers ----------


def _response(status: int = 200, json_body: Any = None, text: str = "") -> MagicMock:
    """A stand-in for httpx.Response with just what the client touches."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.text = text
    if json_body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_body
    resp.raise_for_status.side_effect = (
        None
        if status < 400
        else httpx.HTTPStatusError("err", request=MagicMock(), response=MagicMock())
    )
    return resp


def _chart_body(n: int, start_ts: int = 1_790_000_000, step: int = 3480, **coin_meta) -> dict:
    """A /chart payload with n price points, deliberately not hour-aligned."""
    meta = {"symbol": "UNI", "decimals": 18, "confidence": 0.99, **coin_meta}
    return {
        "coins": {
            CID: {
                **meta,
                "prices": [
                    {"timestamp": start_ts + i * step, "price": 8.0 + i * 0.01} for i in range(n)
                ],
            }
        }
    }


def _client(*responses: MagicMock) -> MagicMock:
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = list(responses)
    return client


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Retry backoff must not slow the suite."""
    monkeypatch.setattr(dc.time, "sleep", lambda _s: None)


# ---------- Small helpers ----------


class TestCoinId:
    def test_format(self):
        assert coin_id("ethereum", UNI) == CID

    def test_lowercases_address(self):
        assert coin_id("ethereum", UNI.upper().replace("0X", "0x")) == CID


class TestPeriodSeconds:
    def test_hourly(self):
        assert period_seconds("1h") == 3600

    def test_daily(self):
        assert period_seconds("1d") == 86400

    def test_unknown_period_raises(self):
        with pytest.raises(ValueError, match="Unsupported period"):
            period_seconds("7m")


# ---------- fetch_chart_chunk ----------


class TestFetchChartChunk:
    def test_returns_points(self):
        client = _client(_response(200, _chart_body(3)))
        points = fetch_chart_chunk(client, "ethereum", UNI, span=3)
        assert len(points) == 3
        assert points[0]["price"] == 8.0

    def test_coin_metadata_flattened_onto_each_point(self):
        """The API reports symbol/decimals/confidence once per coin, not per point."""
        client = _client(_response(200, _chart_body(3)))
        points = fetch_chart_chunk(client, "ethereum", UNI, span=3)
        assert all(p["symbol"] == "UNI" for p in points)
        assert all(p["decimals"] == 18 for p in points)
        assert all(p["confidence"] == 0.99 for p in points)

    def test_request_params(self):
        client = _client(_response(200, _chart_body(1)))
        fetch_chart_chunk(client, "ethereum", UNI, span=100, period="1h", start_ts=1_790_000_000)

        url = client.get.call_args[0][0]
        params = client.get.call_args[1]["params"]
        assert url.endswith(f"/chart/{CID}")
        assert params == {"span": 100, "period": "1h", "start": 1_790_000_000}

    def test_start_omitted_when_none(self):
        client = _client(_response(200, _chart_body(1)))
        fetch_chart_chunk(client, "ethereum", UNI, span=10)
        assert "start" not in client.get.call_args[1]["params"]

    def test_span_over_cap_rejected_before_request(self):
        client = _client()
        with pytest.raises(ValueError, match="exceeds the maximum of 500"):
            fetch_chart_chunk(client, "ethereum", UNI, span=MAX_POINTS_PER_REQUEST + 1)
        client.get.assert_not_called()

    def test_span_over_cap_message_names_point_count(self):
        client = _client()
        with pytest.raises(ValueError, match="Requested 720 data points"):
            fetch_chart_chunk(client, "ethereum", UNI, span=720)

    def test_zero_span_rejected(self):
        client = _client()
        with pytest.raises(ValueError, match="span must be positive"):
            fetch_chart_chunk(client, "ethereum", UNI, span=0)

    def test_api_400_raises_with_api_message(self):
        """The real 400 body is the useful diagnostic; surface it verbatim."""
        body = {"message": "Requested 720 data points exceeds the maximum of 500."}
        client = _client(_response(400, body))
        with pytest.raises(ValueError, match="exceeds the maximum of 500"):
            fetch_chart_chunk(client, "ethereum", UNI, span=400)

    def test_api_400_message_includes_coin_id(self):
        body = {"message": "bad request"}
        client = _client(_response(400, body))
        with pytest.raises(ValueError, match=CID):
            fetch_chart_chunk(client, "ethereum", UNI, span=10)

    def test_missing_coin_raises_with_hint(self):
        client = _client(_response(200, {"coins": {}}))
        with pytest.raises(ValueError, match="no data for"):
            fetch_chart_chunk(client, "ethereum", UNI, span=10)

    def test_empty_prices_returns_empty_list(self):
        body = {"coins": {CID: {"symbol": "UNI", "decimals": 18, "confidence": 0.9, "prices": []}}}
        client = _client(_response(200, body))
        assert fetch_chart_chunk(client, "ethereum", UNI, span=10) == []

    def test_missing_metadata_becomes_none(self):
        body = {"coins": {CID: {"prices": [{"timestamp": 1, "price": 2.0}]}}}
        client = _client(_response(200, body))
        point = fetch_chart_chunk(client, "ethereum", UNI, span=1)[0]
        assert point["symbol"] is None
        assert point["decimals"] is None
        assert point["confidence"] is None

    def test_point_with_null_timestamp_dropped(self):
        """fetch_chart sorts and dedupes on timestamp, so a null must not pass through."""
        body = {
            "coins": {
                CID: {
                    "symbol": "UNI",
                    "decimals": 18,
                    "confidence": 0.99,
                    "prices": [
                        {"timestamp": 1_790_000_000, "price": 8.0},
                        {"timestamp": None, "price": 8.5},
                        {"price": 9.0},  # missing entirely
                    ],
                }
            }
        }
        client = _client(_response(200, body))
        points = fetch_chart_chunk(client, "ethereum", UNI, span=3)
        assert len(points) == 1
        assert points[0]["timestamp"] == 1_790_000_000

    def test_point_with_missing_price_kept_for_mapper(self):
        """Price validation belongs to the mapper, not this layer."""
        body = {
            "coins": {
                CID: {
                    "symbol": "UNI",
                    "decimals": 18,
                    "confidence": 0.99,
                    "prices": [{"timestamp": 1_790_000_000}],
                }
            }
        }
        client = _client(_response(200, body))
        points = fetch_chart_chunk(client, "ethereum", UNI, span=1)
        assert len(points) == 1
        assert points[0]["price"] is None

    def test_server_error_is_raised_after_retries(self):
        client = _client(*[_response(500, {"message": "boom"}) for _ in range(3)])
        with pytest.raises(httpx.HTTPStatusError):
            fetch_chart_chunk(client, "ethereum", UNI, span=10)
        assert client.get.call_count == 3


# ---------- Retry behaviour ----------


class TestRetry:
    def test_429_then_success(self):
        client = _client(_response(429, {"message": "slow down"}), _response(200, _chart_body(2)))
        points = fetch_chart_chunk(client, "ethereum", UNI, span=2)
        assert len(points) == 2
        assert client.get.call_count == 2

    def test_503_then_success(self):
        client = _client(_response(503, {"message": "unavailable"}), _response(200, _chart_body(1)))
        assert len(fetch_chart_chunk(client, "ethereum", UNI, span=1)) == 1

    def test_400_is_not_retried(self):
        """A bad request won't get better by repeating it."""
        client = _client(_response(400, {"message": "nope"}))
        with pytest.raises(ValueError):
            fetch_chart_chunk(client, "ethereum", UNI, span=10)
        assert client.get.call_count == 1

    def test_transport_error_retried_then_raised(self):
        client = MagicMock(spec=httpx.Client)
        client.get.side_effect = httpx.ConnectError("no route")
        with pytest.raises(httpx.ConnectError):
            fetch_chart_chunk(client, "ethereum", UNI, span=10)
        assert client.get.call_count == 3

    def test_transport_error_then_success(self):
        client = MagicMock(spec=httpx.Client)
        client.get.side_effect = [httpx.ConnectError("blip"), _response(200, _chart_body(1))]
        assert len(fetch_chart_chunk(client, "ethereum", UNI, span=1)) == 1


# ---------- fetch_chart (chunking) ----------


class TestFetchChart:
    def test_single_chunk_when_under_cap(self):
        """10 days hourly = 240 points, one request."""
        client = _client(_response(200, _chart_body(240)))
        points = fetch_chart(client, "ethereum", UNI, span_days=10)
        assert len(points) == 240
        assert client.get.call_count == 1

    def test_thirty_days_hourly_splits_into_two_chunks(self):
        """720 points > 500 cap, so two balanced chunks of 360."""
        step = 3600
        first = _chart_body(360, start_ts=1_790_000_000, step=step)
        second = _chart_body(360, start_ts=1_790_000_000 + 360 * step, step=step)
        client = _client(_response(200, first), _response(200, second))

        points = fetch_chart(client, "ethereum", UNI, span_days=30)

        assert client.get.call_count == 2
        assert len(points) == 720
        spans = [call[1]["params"]["span"] for call in client.get.call_args_list]
        assert spans == [360, 360], "chunks should be balanced, not 500 + 220"

    def test_start_ts_walks_forward_between_chunks(self):
        step = 3600
        client = _client(
            _response(200, _chart_body(360, start_ts=1_790_000_000, step=step)),
            _response(200, _chart_body(360, start_ts=1_790_000_000 + 360 * step, step=step)),
        )
        fetch_chart(client, "ethereum", UNI, span_days=30)

        starts = [call[1]["params"]["start"] for call in client.get.call_args_list]
        assert len(starts) == 2
        # Second chunk begins exactly chunk_span * period_seconds after the first
        assert starts[1] - starts[0] == 360 * step

    def test_overlapping_chunks_deduplicated_on_raw_timestamp(self):
        """Boundaries can repeat a point; the union must stay distinct."""
        step = 3600
        base = 1_790_000_000
        first = _chart_body(360, start_ts=base, step=step)
        # Second chunk starts one point early, so its first entry repeats the
        # last entry of chunk one.
        second = _chart_body(360, start_ts=base + 359 * step, step=step)
        client = _client(_response(200, first), _response(200, second))

        points = fetch_chart(client, "ethereum", UNI, span_days=30)

        timestamps = [p["timestamp"] for p in points]
        assert len(timestamps) == len(set(timestamps)), "duplicate raw timestamps leaked through"
        assert len(points) == 719  # 720 fetched, one was a repeat

    def test_results_sorted_chronologically(self):
        step = 3600
        base = 1_790_000_000
        # Return the later chunk first to prove sorting isn't accidental
        later = _chart_body(360, start_ts=base + 360 * step, step=step)
        earlier = _chart_body(360, start_ts=base, step=step)
        client = _client(_response(200, later), _response(200, earlier))

        points = fetch_chart(client, "ethereum", UNI, span_days=30)
        timestamps = [p["timestamp"] for p in points]
        assert timestamps == sorted(timestamps)

    def test_three_chunks_for_sixty_days(self):
        """1440 points / 500 cap = 3 chunks of 480."""
        step = 3600
        base = 1_790_000_000
        responses = [
            _response(200, _chart_body(480, start_ts=base + i * 480 * step, step=step))
            for i in range(3)
        ]
        client = _client(*responses)

        points = fetch_chart(client, "ethereum", UNI, span_days=60)

        assert client.get.call_count == 3
        spans = [call[1]["params"]["span"] for call in client.get.call_args_list]
        assert spans == [480, 480, 480]
        assert len(points) == 1440

    def test_no_chunk_exceeds_cap(self):
        step = 3600
        base = 1_790_000_000
        client = _client(
            *[
                _response(200, _chart_body(480, start_ts=base + i * 480 * step, step=step))
                for i in range(3)
            ]
        )
        fetch_chart(client, "ethereum", UNI, span_days=60)
        for call in client.get.call_args_list:
            assert call[1]["params"]["span"] <= MAX_POINTS_PER_REQUEST

    def test_daily_period_needs_fewer_points(self):
        """30 days at 1d = 30 points, one chunk."""
        client = _client(_response(200, _chart_body(30, step=86400)))
        points = fetch_chart(client, "ethereum", UNI, span_days=30, period="1d")
        assert client.get.call_count == 1
        assert client.get.call_args[1]["params"]["span"] == 30
        assert len(points) == 30

    def test_zero_span_days_rejected(self):
        client = _client()
        with pytest.raises(ValueError, match="span_days must be positive"):
            fetch_chart(client, "ethereum", UNI, span_days=0)
        client.get.assert_not_called()

    def test_unsupported_period_rejected_before_request(self):
        client = _client()
        with pytest.raises(ValueError, match="Unsupported period"):
            fetch_chart(client, "ethereum", UNI, span_days=30, period="7m")
        client.get.assert_not_called()

    def test_per_chunk_logging(self, caplog):
        step = 3600
        base = 1_790_000_000
        client = _client(
            _response(200, _chart_body(360, start_ts=base, step=step)),
            _response(200, _chart_body(360, start_ts=base + 360 * step, step=step)),
        )
        with caplog.at_level("INFO"):
            fetch_chart(client, "ethereum", UNI, span_days=30)

        assert "chunk 1/2" in caplog.text
        assert "chunk 2/2" in caplog.text
        # The plan line lands before the chunks so a stalled backfill is legible
        assert "2 chunk(s) of 360" in caplog.text
