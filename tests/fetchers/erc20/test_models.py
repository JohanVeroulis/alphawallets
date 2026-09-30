"""Tests for ERC-20 fetcher Pydantic models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from alphawallets.fetchers.erc20.models import ERC20Transfer, RawAssetTransfer

# ---------- Fixtures ----------

# Uniswap (UNI) token contract on Ethereum.
UNI_ETHEREUM = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"


@pytest.fixture
def valid_raw_transfer_kwargs() -> dict:
    return {
        "chain": "ethereum",
        "unique_id": "0x" + "a" * 64 + ":log:5",
        "block_num": 22040964,
        "tx_hash": "0x" + "a" * 64,
        "from_addr": "0x" + "1" * 40,
        "to_addr": "0x" + "2" * 40,
        "value_raw": "1000000000000000000",  # 1 UNI in wei
        "value_decimal": 1.0,
        "asset": "UNI",
        "category": "erc20",
        "contract_address": UNI_ETHEREUM,
        "contract_decimal": 18,
        "log_index": 5,
        "block_timestamp": datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
    }


@pytest.fixture
def valid_erc20_transfer_kwargs() -> dict:
    return {
        "chain": "ethereum",
        "block_number": 22040964,
        "block_timestamp": datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
        "tx_hash": "0x" + "a" * 64,
        "log_index": 5,
        "unique_id": "0x" + "a" * 64 + ":log:5",
        "token_address": UNI_ETHEREUM,
        "from_addr": "0x" + "1" * 40,
        "to_addr": "0x" + "2" * 40,
        "value_raw": "1000000000000000000",
        "token_decimals": 18,
    }


# ---------- RawAssetTransfer ----------


class TestRawAssetTransfer:
    def test_valid(self, valid_raw_transfer_kwargs):
        raw = RawAssetTransfer(**valid_raw_transfer_kwargs)
        assert raw.chain == "ethereum"
        assert raw.value_raw == "1000000000000000000"
        assert raw.log_index == 5

    def test_addresses_lowercased(self, valid_raw_transfer_kwargs):
        kwargs = {
            **valid_raw_transfer_kwargs,
            "from_addr": "0xAABBCCDDEEFFAABBCCDDEEFFAABBCCDDEEFFAABB",
            "to_addr": "0x" + "F" * 40,
            "contract_address": UNI_ETHEREUM.upper(),
            "tx_hash": "0x" + "A" * 64,
        }
        raw = RawAssetTransfer(**kwargs)
        assert raw.from_addr == "0xaabbccddeeffaabbccddeeffaabbccddeeffaabb"
        assert raw.to_addr == "0x" + "f" * 40
        assert raw.contract_address == UNI_ETHEREUM
        assert raw.tx_hash == "0x" + "a" * 64

    def test_log_index_optional(self, valid_raw_transfer_kwargs):
        kwargs = {**valid_raw_transfer_kwargs, "log_index": None}
        raw = RawAssetTransfer(**kwargs)
        assert raw.log_index is None

    def test_value_decimal_optional(self, valid_raw_transfer_kwargs):
        kwargs = {**valid_raw_transfer_kwargs, "value_decimal": None}
        raw = RawAssetTransfer(**kwargs)
        assert raw.value_decimal is None

    def test_asset_optional(self, valid_raw_transfer_kwargs):
        kwargs = {**valid_raw_transfer_kwargs, "asset": None}
        raw = RawAssetTransfer(**kwargs)
        assert raw.asset is None

    def test_frozen(self, valid_raw_transfer_kwargs):
        raw = RawAssetTransfer(**valid_raw_transfer_kwargs)
        with pytest.raises(ValidationError):
            raw.asset = "AAVE"  # type: ignore[misc]

    def test_bad_tx_hash_rejected(self, valid_raw_transfer_kwargs):
        kwargs = {**valid_raw_transfer_kwargs, "tx_hash": "not-a-hash"}
        with pytest.raises(ValidationError):
            RawAssetTransfer(**kwargs)

    def test_bad_address_rejected(self, valid_raw_transfer_kwargs):
        kwargs = {**valid_raw_transfer_kwargs, "from_addr": "0xZZZ"}
        with pytest.raises(ValidationError):
            RawAssetTransfer(**kwargs)

    def test_negative_block_rejected(self, valid_raw_transfer_kwargs):
        kwargs = {**valid_raw_transfer_kwargs, "block_num": -1}
        with pytest.raises(ValidationError):
            RawAssetTransfer(**kwargs)


# ---------- ERC20Transfer ----------


class TestERC20Transfer:
    def test_valid(self, valid_erc20_transfer_kwargs):
        t = ERC20Transfer(**valid_erc20_transfer_kwargs)
        assert t.chain == "ethereum"
        assert t.token_address == UNI_ETHEREUM

    def test_log_index_can_be_none(self, valid_erc20_transfer_kwargs):
        kwargs = {**valid_erc20_transfer_kwargs, "log_index": None}
        t = ERC20Transfer(**kwargs)
        assert t.log_index is None
        # unique_id carries dedup responsibility in that case
        assert t.unique_id

    def test_addresses_lowercased(self, valid_erc20_transfer_kwargs):
        kwargs = {
            **valid_erc20_transfer_kwargs,
            "token_address": UNI_ETHEREUM.upper(),
        }
        t = ERC20Transfer(**kwargs)
        assert t.token_address == UNI_ETHEREUM

    def test_frozen(self, valid_erc20_transfer_kwargs):
        t = ERC20Transfer(**valid_erc20_transfer_kwargs)
        with pytest.raises(ValidationError):
            t.value_raw = "0"  # type: ignore[misc]

    def test_bad_chain_rejected(self, valid_erc20_transfer_kwargs):
        kwargs = {**valid_erc20_transfer_kwargs, "chain": "solana"}
        with pytest.raises(ValidationError):
            ERC20Transfer(**kwargs)
