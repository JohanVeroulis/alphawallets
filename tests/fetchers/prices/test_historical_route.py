"""Tests for the /prices/historical fallback route (ADR 0010)."""

import httpx
import pytest

from alphawallets.fetchers.prices.defillama_client import (
    MAX_RETRY_AFTER_SECONDS,
    _retry_after_seconds,
    chart_has_coverage,
    fetch_historical_price,
    fetch_historical_span,
)

MKR = "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2"
UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
MKR_CID = f"ethereum:{MKR}"
TS = 1791262190


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


def _price_payload(cid: str, price: float, timestamp: int = TS, **extra) -> dict:
    coin = {
        "price": price,
        "timestamp": timestamp,
        "symbol": "MKR",
        "decimals": 18,
        "confidence": 0.99,
    }
    coin.update(extra)
    return {"coins": {cid: coin}}


class TestFetchHistoricalPrice:
    def test_happy_path_returns_chart_shaped_entry(self):
        """The shape must match fetch_chart_chunk so the mapper stays route-blind."""
        client = _client(lambda r: httpx.Response(200, json=_price_payload(MKR_CID, 2014.5405)))
        entry = fetch_historical_price(client, "ethereum", MKR, TS)
        assert entry == {
            "timestamp": TS,
            "price": 2014.5405,
            "symbol": "MKR",
            "decimals": 18,
            "confidence": 0.99,
        }

    def test_requests_the_documented_url(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json=_price_payload(MKR_CID, 1.0))

        fetch_historical_price(_client(handler), "ethereum", MKR, TS)
        assert seen == [f"https://coins.llama.fi/prices/historical/{TS}/{MKR_CID}"]

    def test_coin_absent_returns_none_not_an_error(self):
        """A 200 that omits the coin is a coverage answer, not a failure."""
        client = _client(lambda r: httpx.Response(200, json={"coins": {}}))
        assert fetch_historical_price(client, "ethereum", MKR, TS) is None

    def test_empty_body_returns_none(self):
        client = _client(lambda r: httpx.Response(200, json={}))
        assert fetch_historical_price(client, "ethereum", MKR, TS) is None

    def test_404_returns_none(self):
        client = _client(lambda r: httpx.Response(404, json={"message": "not found"}))
        assert fetch_historical_price(client, "ethereum", MKR, TS) is None

    def test_entry_without_a_price_returns_none(self):
        payload = {"coins": {MKR_CID: {"timestamp": TS, "symbol": "MKR"}}}
        client = _client(lambda r: httpx.Response(200, json=payload))
        assert fetch_historical_price(client, "ethereum", MKR, TS) is None

    def test_400_raises_with_the_providers_message(self):
        client = _client(lambda r: httpx.Response(400, json={"message": "bad coin id"}))
        with pytest.raises(ValueError, match="bad coin id"):
            fetch_historical_price(client, "ethereum", MKR, TS)

    def test_uses_the_reported_timestamp_not_the_requested_one(self):
        """The provider prices the nearest observation it has; alignment is the
        mapper's job, so the observed instant is what must be returned."""
        client = _client(
            lambda r: httpx.Response(200, json=_price_payload(MKR_CID, 1.0, timestamp=TS - 137))
        )
        entry = fetch_historical_price(client, "ethereum", MKR, TS)
        assert entry["timestamp"] == TS - 137

    def test_unusable_reported_timestamp_falls_back_to_the_request(self):
        client = _client(
            lambda r: httpx.Response(200, json=_price_payload(MKR_CID, 1.0, timestamp=None))
        )
        entry = fetch_historical_price(client, "ethereum", MKR, TS)
        assert entry["timestamp"] == TS

    def test_retries_then_succeeds_on_429(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _s: None)
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "1"}, json={})
            return httpx.Response(200, json=_price_payload(MKR_CID, 5.0))

        entry = fetch_historical_price(_client(handler), "ethereum", MKR, TS)
        assert entry["price"] == 5.0
        assert calls["n"] == 2

    def test_honours_retry_after_over_exponential_backoff(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("time.sleep", slept.append)
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "7"}, json={})
            return httpx.Response(200, json=_price_payload(MKR_CID, 5.0))

        fetch_historical_price(_client(handler), "ethereum", MKR, TS)
        # 7s from the header, not the 1s first exponential step.
        assert slept == [7.0]

    def test_transport_error_propagates_after_retries(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _s: None)

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out")

        with pytest.raises(httpx.HTTPError):
            fetch_historical_price(_client(handler), "ethereum", MKR, TS)


class TestRetryAfterParsing:
    def test_absent_header_is_none(self):
        assert _retry_after_seconds(httpx.Response(429)) is None

    def test_delta_seconds_parsed(self):
        assert _retry_after_seconds(httpx.Response(429, headers={"Retry-After": "12"})) == 12.0

    def test_whitespace_tolerated(self):
        assert _retry_after_seconds(httpx.Response(429, headers={"Retry-After": " 3 "})) == 3.0

    def test_http_date_form_falls_back_to_backoff(self):
        """Valid HTTP but unobserved here; a misparsed date would sleep wildly wrong."""
        header = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        assert _retry_after_seconds(httpx.Response(429, headers=header)) is None

    def test_negative_ignored(self):
        assert _retry_after_seconds(httpx.Response(429, headers={"Retry-After": "-5"})) is None

    def test_capped(self):
        """An hour-long wait is not something a backfill should obey silently."""
        header = {"Retry-After": "3600"}
        assert _retry_after_seconds(httpx.Response(429, headers=header)) == (
            MAX_RETRY_AFTER_SECONDS
        )


class TestChartHasCoverage:
    def test_covered_coin_is_true(self):
        payload = {
            "coins": {
                f"ethereum:{UNI}": {
                    "symbol": "UNI",
                    "decimals": 18,
                    "confidence": 0.99,
                    "prices": [
                        {"timestamp": TS, "price": 8.8},
                        {"timestamp": TS + 3600, "price": 8.9},
                    ],
                }
            }
        }
        client = _client(lambda r: httpx.Response(200, json=payload))
        assert chart_has_coverage(client, "ethereum", UNI) is True

    def test_coin_absent_is_false(self):
        """The MKR case: a 200 with no entry for the coin."""
        client = _client(lambda r: httpx.Response(200, json={"coins": {}}))
        assert chart_has_coverage(client, "ethereum", MKR) is False

    def test_empty_price_list_is_false(self):
        payload = {"coins": {f"ethereum:{MKR}": {"symbol": "MKR", "prices": []}}}
        client = _client(lambda r: httpx.Response(200, json=payload))
        assert chart_has_coverage(client, "ethereum", MKR) is False

    def test_a_real_error_is_not_read_as_absence(self):
        """A 400 must raise, not silently cache the coin as route-less."""
        client = _client(lambda r: httpx.Response(400, json={"message": "malformed"}))
        with pytest.raises(ValueError, match="malformed"):
            chart_has_coverage(client, "ethereum", MKR)

    def test_probe_is_small(self):
        """A miss should cost almost nothing, so the probe asks for 2 points."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"coins": {}})

        chart_has_coverage(_client(handler), "ethereum", MKR)
        assert "span=2" in str(seen[0].url)


class TestFetchHistoricalSpan:
    def test_walks_one_request_per_period(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _s: None)
        requested: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            ts = int(str(request.url).split("/historical/")[1].split("/")[0])
            requested.append(ts)
            return httpx.Response(200, json=_price_payload(MKR_CID, 1.0, timestamp=ts))

        entries = fetch_historical_span(_client(handler), "ethereum", MKR, span_days=1)
        assert len(requested) == 24  # 1 day hourly
        assert len(entries) == 24
        assert requested == sorted(requested)

    def test_unpriced_hours_are_skipped_not_fabricated(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _s: None)
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] % 2:
                return httpx.Response(200, json={"coins": {}})
            ts = int(str(request.url).split("/historical/")[1].split("/")[0])
            return httpx.Response(200, json=_price_payload(MKR_CID, 2.0, timestamp=ts))

        entries = fetch_historical_span(_client(handler), "ethereum", MKR, span_days=1)
        assert len(entries) == 12
        assert all(e["price"] == 2.0 for e in entries)

    def test_duplicate_reported_timestamps_collapse(self, monkeypatch):
        """A sparser provider grid than we ask for must not produce dupes."""
        monkeypatch.setattr("time.sleep", lambda _s: None)
        client = _client(
            lambda r: httpx.Response(200, json=_price_payload(MKR_CID, 3.0, timestamp=TS))
        )
        entries = fetch_historical_span(client, "ethereum", MKR, span_days=1)
        assert len(entries) == 1

    def test_results_are_chronological(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _s: None)

        def handler(request: httpx.Request) -> httpx.Response:
            ts = int(str(request.url).split("/historical/")[1].split("/")[0])
            # Return them out of order relative to the request sequence.
            return httpx.Response(200, json=_price_payload(MKR_CID, 1.0, timestamp=-ts))

        entries = fetch_historical_span(_client(handler), "ethereum", MKR, span_days=1)
        assert [e["timestamp"] for e in entries] == sorted(e["timestamp"] for e in entries)

    def test_paces_requests(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("time.sleep", slept.append)
        client = _client(
            lambda r: httpx.Response(200, json=_price_payload(MKR_CID, 1.0, timestamp=TS))
        )
        fetch_historical_span(client, "ethereum", MKR, span_days=1, request_interval=0.25)
        # One sleep between each pair of requests, none before the first.
        assert slept == [0.25] * 23

    def test_empty_coverage_yields_no_entries(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _s: None)
        client = _client(lambda r: httpx.Response(200, json={"coins": {}}))
        assert fetch_historical_span(client, "ethereum", MKR, span_days=1) == []
