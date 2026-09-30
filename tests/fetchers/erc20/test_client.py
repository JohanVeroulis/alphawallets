"""Tests for the Alchemy Transfers API client.

All tests mock `w3.provider.make_request` — no network access.
"""

from unittest.mock import MagicMock

import pytest

from alphawallets.fetchers.erc20.client import (
    MAX_COUNT_PER_PAGE,
    RPC_METHOD,
    fetch_asset_transfers,
)

UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"


def _make_w3(result: dict | None = None, error: dict | None = None) -> MagicMock:
    """Build a mock Web3 whose provider returns one canned RPC response."""
    w3 = MagicMock()
    response: dict = {}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result if result is not None else {"transfers": []}
    w3.provider.make_request.return_value = response
    return w3


class TestRequestShape:
    def test_method_and_param_envelope(self):
        w3 = _make_w3()
        fetch_asset_transfers(w3, UNI, 100, 200)

        method, params = w3.provider.make_request.call_args[0]
        assert method == RPC_METHOD
        # Alchemy takes a single object wrapped in a list
        assert isinstance(params, list)
        assert len(params) == 1

    def test_block_range_encoded_as_hex(self):
        w3 = _make_w3()
        fetch_asset_transfers(w3, UNI, 26_000_000, 26_001_000)

        payload = w3.provider.make_request.call_args[0][1][0]
        assert payload["fromBlock"] == hex(26_000_000)
        assert payload["toBlock"] == hex(26_001_000)

    def test_fixed_filters(self):
        w3 = _make_w3()
        fetch_asset_transfers(w3, UNI, 1, 2)

        payload = w3.provider.make_request.call_args[0][1][0]
        assert payload["category"] == ["erc20"]
        assert payload["contractAddresses"] == [UNI]
        assert payload["withMetadata"] is True
        assert payload["excludeZeroValue"] is True
        assert payload["maxCount"] == MAX_COUNT_PER_PAGE

    def test_page_key_omitted_on_first_call(self):
        w3 = _make_w3()
        fetch_asset_transfers(w3, UNI, 1, 2)

        payload = w3.provider.make_request.call_args[0][1][0]
        assert "pageKey" not in payload

    def test_page_key_included_when_given(self):
        w3 = _make_w3()
        fetch_asset_transfers(w3, UNI, 1, 2, page_key="abc123")

        payload = w3.provider.make_request.call_args[0][1][0]
        assert payload["pageKey"] == "abc123"


class TestResponseHandling:
    def test_returns_transfers_and_next_page_key(self):
        w3 = _make_w3({"transfers": [{"uniqueId": "a"}, {"uniqueId": "b"}], "pageKey": "next-1"})
        transfers, next_key = fetch_asset_transfers(w3, UNI, 1, 2)

        assert len(transfers) == 2
        assert next_key == "next-1"

    def test_last_page_returns_none_page_key(self):
        w3 = _make_w3({"transfers": [{"uniqueId": "a"}]})
        transfers, next_key = fetch_asset_transfers(w3, UNI, 1, 2)

        assert len(transfers) == 1
        assert next_key is None

    def test_empty_result_returns_empty_list(self):
        w3 = _make_w3({"transfers": []})
        transfers, next_key = fetch_asset_transfers(w3, UNI, 1, 2)

        assert transfers == []
        assert next_key is None

    def test_missing_result_key_tolerated(self):
        # A malformed-but-successful response shouldn't crash the caller
        w3 = MagicMock()
        w3.provider.make_request.return_value = {}
        transfers, next_key = fetch_asset_transfers(w3, UNI, 1, 2)

        assert transfers == []
        assert next_key is None

    def test_rpc_error_raises_value_error(self):
        w3 = _make_w3(error={"code": -32602, "message": "invalid params"})
        with pytest.raises(ValueError, match="invalid params"):
            fetch_asset_transfers(w3, UNI, 1, 2)

    def test_error_message_names_block_range(self):
        w3 = _make_w3(error={"code": 429, "message": "rate limited"})
        with pytest.raises(ValueError, match="100-200"):
            fetch_asset_transfers(w3, UNI, 100, 200)


class TestRangeValidation:
    def test_inverted_range_rejected(self):
        w3 = _make_w3()
        with pytest.raises(ValueError, match="from_block"):
            fetch_asset_transfers(w3, UNI, 200, 100)
        w3.provider.make_request.assert_not_called()

    def test_single_block_range_allowed(self):
        w3 = _make_w3()
        fetch_asset_transfers(w3, UNI, 100, 100)
        assert w3.provider.make_request.called


class TestPaginationLoop:
    def test_caller_loop_walks_pages_to_exhaustion(self):
        """Simulates the orchestrator's loop: follow page keys until None."""
        pages = [
            {"transfers": [{"uniqueId": "a"}], "pageKey": "p2"},
            {"transfers": [{"uniqueId": "b"}], "pageKey": "p3"},
            {"transfers": [{"uniqueId": "c"}]},  # last page
        ]
        w3 = MagicMock()
        w3.provider.make_request.side_effect = [{"result": p} for p in pages]

        collected: list[dict] = []
        page_key: str | None = None
        while True:
            transfers, page_key = fetch_asset_transfers(w3, UNI, 1, 2, page_key=page_key)
            collected.extend(transfers)
            if page_key is None:
                break

        assert [t["uniqueId"] for t in collected] == ["a", "b", "c"]
        assert w3.provider.make_request.call_count == 3
        # Second and third calls must carry the previous page's key
        second_payload = w3.provider.make_request.call_args_list[1][0][1][0]
        third_payload = w3.provider.make_request.call_args_list[2][0][1][0]
        assert second_payload["pageKey"] == "p2"
        assert third_payload["pageKey"] == "p3"
