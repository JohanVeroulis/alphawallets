"""Tests for the ERC-20 Transfers API mapper.

Fixtures mirror the shape of real `alchemy_getAssetTransfers` entries for UNI
on Ethereum, with addresses anonymized. Three cases the brief calls out:
normal transfer, missing logIndex, missing metadata.blockTimestamp.
"""

from datetime import UTC, datetime

import pytest

from alphawallets.fetchers.erc20.mapper import (
    _hex_to_int,
    _parse_iso_timestamp,
    to_erc20_transfer,
    to_raw_asset_transfer,
)
from alphawallets.fetchers.erc20.models import ERC20Transfer, RawAssetTransfer

UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"


# ---------- Fixtures ----------


@pytest.fixture
def normal_entry() -> dict:
    """A complete ERC-20 transfer: 100 UNI, with logIndex and metadata."""
    return {
        "blockNum": "0x18da6b4",
        "uniqueId": "0x" + "a" * 64 + ":log:42",
        "hash": "0x" + "a" * 64,
        "from": "0x" + "1" * 40,
        "to": "0x" + "2" * 40,
        "value": 100.0,
        "erc721TokenId": None,
        "erc1155Metadata": None,
        "tokenId": None,
        "asset": "UNI",
        "category": "erc20",
        "rawContract": {
            "value": "0x56bc75e2d63100000",  # 100 * 10^18
            "address": UNI,
            "decimal": "0x12",  # 18
        },
        "metadata": {"blockTimestamp": "2026-09-29T12:34:56.000Z"},
        "logIndex": "0x2a",  # 42
    }


@pytest.fixture
def entry_without_log_index(normal_entry: dict) -> dict:
    """Most Transfers API responses omit logIndex entirely."""
    return {k: v for k, v in normal_entry.items() if k != "logIndex"}


@pytest.fixture
def entry_without_timestamp(normal_entry: dict) -> dict:
    """withMetadata=true normally guarantees this, but tolerate its absence."""
    entry = dict(normal_entry)
    entry["metadata"] = {}
    return entry


# ---------- Hex / timestamp helpers ----------


class TestHexToInt:
    def test_hex_string(self):
        assert _hex_to_int("0x18da6b4") == 26_060_468

    def test_decimals(self):
        assert _hex_to_int("0x12") == 18

    def test_int_passthrough(self):
        assert _hex_to_int(42) == 42

    def test_none(self):
        assert _hex_to_int(None) is None

    def test_unparseable_string(self):
        assert _hex_to_int("not-hex") is None

    def test_unexpected_type(self):
        assert _hex_to_int(["0x1"]) is None


class TestParseIsoTimestamp:
    def test_z_suffix(self):
        result = _parse_iso_timestamp("2026-09-29T12:34:56.000Z")
        assert result == datetime(2026, 9, 29, 12, 34, 56, tzinfo=UTC)
        assert result.tzinfo is not None

    def test_offset_suffix(self):
        result = _parse_iso_timestamp("2026-09-29T12:34:56+00:00")
        assert result == datetime(2026, 9, 29, 12, 34, 56, tzinfo=UTC)

    def test_naive_gets_utc(self):
        result = _parse_iso_timestamp("2026-09-29T12:34:56")
        assert result is not None
        assert result.tzinfo is UTC

    def test_none(self):
        assert _parse_iso_timestamp(None) is None

    def test_empty_string(self):
        assert _parse_iso_timestamp("") is None

    def test_garbage(self):
        assert _parse_iso_timestamp("yesterday") is None


# ---------- to_raw_asset_transfer ----------


class TestToRawAssetTransfer:
    def test_normal_entry(self, normal_entry):
        raw = to_raw_asset_transfer(normal_entry, chain="ethereum")
        assert isinstance(raw, RawAssetTransfer)
        assert raw.chain == "ethereum"
        assert raw.block_num == 26_060_468
        assert raw.unique_id == "0x" + "a" * 64 + ":log:42"
        assert raw.asset == "UNI"
        assert raw.category == "erc20"

    def test_value_stored_as_decimal_string(self, normal_entry):
        raw = to_raw_asset_transfer(normal_entry, chain="ethereum")
        # 0x56bc75e2d63100000 == 100 * 10^18
        assert raw.value_raw == "100000000000000000000"
        assert int(raw.value_raw) == 100 * 10**18

    def test_decimals_parsed(self, normal_entry):
        raw = to_raw_asset_transfer(normal_entry, chain="ethereum")
        assert raw.contract_decimal == 18

    def test_addresses_lowercased(self, normal_entry):
        entry = dict(normal_entry)
        entry["from"] = "0x" + "A" * 40
        entry["rawContract"] = {
            **normal_entry["rawContract"],
            "address": UNI.upper().replace("0X", "0x"),
        }
        raw = to_raw_asset_transfer(entry, chain="ethereum")
        assert raw.from_addr == "0x" + "a" * 40
        assert raw.contract_address == UNI

    def test_log_index_parsed_when_present(self, normal_entry):
        raw = to_raw_asset_transfer(normal_entry, chain="ethereum")
        assert raw.log_index == 42

    def test_log_index_none_when_absent(self, entry_without_log_index):
        raw = to_raw_asset_transfer(entry_without_log_index, chain="ethereum")
        assert raw is not None
        assert raw.log_index is None

    def test_timestamp_parsed(self, normal_entry):
        raw = to_raw_asset_transfer(normal_entry, chain="ethereum")
        assert raw.block_timestamp == datetime(2026, 9, 29, 12, 34, 56, tzinfo=UTC)

    def test_timestamp_none_when_metadata_empty(self, entry_without_timestamp):
        raw = to_raw_asset_transfer(entry_without_timestamp, chain="ethereum")
        assert raw is not None
        assert raw.block_timestamp is None

    def test_base_chain_accepted(self, normal_entry):
        raw = to_raw_asset_transfer(normal_entry, chain="base")
        assert raw.chain == "base"

    def test_missing_raw_value_skipped(self, normal_entry):
        entry = dict(normal_entry)
        entry["rawContract"] = {"address": UNI, "decimal": "0x12"}
        assert to_raw_asset_transfer(entry, chain="ethereum") is None

    def test_missing_contract_address_skipped(self, normal_entry):
        entry = dict(normal_entry)
        entry["rawContract"] = {"value": "0x1", "decimal": "0x12"}
        assert to_raw_asset_transfer(entry, chain="ethereum") is None

    def test_missing_raw_contract_entirely_skipped(self, normal_entry):
        entry = {k: v for k, v in normal_entry.items() if k != "rawContract"}
        assert to_raw_asset_transfer(entry, chain="ethereum") is None

    def test_unparseable_value_skipped(self, normal_entry):
        entry = dict(normal_entry)
        entry["rawContract"] = {**normal_entry["rawContract"], "value": "not-hex"}
        assert to_raw_asset_transfer(entry, chain="ethereum") is None

    def test_unparseable_block_num_skipped(self, normal_entry):
        entry = {**normal_entry, "blockNum": "nonsense"}
        assert to_raw_asset_transfer(entry, chain="ethereum") is None

    def test_invalid_address_skipped_not_raised(self, normal_entry):
        """Model validation failure is a skip, not a crash."""
        entry = {**normal_entry, "from": "not-an-address"}
        assert to_raw_asset_transfer(entry, chain="ethereum") is None

    def test_missing_hash_skipped_not_raised(self, normal_entry):
        entry = {k: v for k, v in normal_entry.items() if k != "hash"}
        assert to_raw_asset_transfer(entry, chain="ethereum") is None

    def test_skip_logs_warning(self, normal_entry, caplog):
        entry = dict(normal_entry)
        entry["rawContract"] = {"address": UNI}
        with caplog.at_level("WARNING"):
            to_raw_asset_transfer(entry, chain="ethereum")
        assert "Skipping transfer" in caplog.text


# ---------- to_erc20_transfer ----------


class TestToErc20Transfer:
    def test_normal_roundtrip(self, normal_entry):
        raw = to_raw_asset_transfer(normal_entry, chain="ethereum")
        decoded = to_erc20_transfer(raw)

        assert isinstance(decoded, ERC20Transfer)
        assert decoded.chain == "ethereum"
        assert decoded.block_number == 26_060_468
        assert decoded.block_timestamp == datetime(2026, 9, 29, 12, 34, 56, tzinfo=UTC)
        assert decoded.token_address == UNI
        assert decoded.from_addr == "0x" + "1" * 40
        assert decoded.to_addr == "0x" + "2" * 40
        assert decoded.value_raw == "100000000000000000000"
        assert decoded.token_decimals == 18
        assert decoded.log_index == 42
        assert decoded.unique_id == raw.unique_id

    def test_log_index_none_preserved(self, entry_without_log_index):
        raw = to_raw_asset_transfer(entry_without_log_index, chain="ethereum")
        decoded = to_erc20_transfer(raw)
        assert decoded is not None
        assert decoded.log_index is None
        # unique_id carries dedup responsibility in this case
        assert decoded.unique_id

    def test_missing_timestamp_returns_none(self, entry_without_timestamp):
        raw = to_raw_asset_transfer(entry_without_timestamp, chain="ethereum")
        assert raw is not None  # raw row is still persistable
        assert to_erc20_transfer(raw) is None

    def test_missing_timestamp_logs_warning(self, entry_without_timestamp, caplog):
        raw = to_raw_asset_transfer(entry_without_timestamp, chain="ethereum")
        with caplog.at_level("WARNING"):
            to_erc20_transfer(raw)
        assert "no block_timestamp" in caplog.text

    def test_base_chain_preserved(self, normal_entry):
        raw = to_raw_asset_transfer(normal_entry, chain="base")
        decoded = to_erc20_transfer(raw)
        assert decoded.chain == "base"
