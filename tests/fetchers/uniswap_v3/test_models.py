"""Tests for Uniswap V3 fetcher Pydantic models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from alphawallets.fetchers.uniswap_v3.models import RawSwapLog, UniswapV3Swap

# ---------- Fixtures ----------


@pytest.fixture
def valid_raw_log_kwargs() -> dict:
    return {
        "chain": "ethereum",
        "block_number": 22040964,
        "block_hash": "0x" + "a" * 64,
        "tx_hash": "0x" + "b" * 64,
        "log_index": 3,
        "transaction_index": 12,
        "address": "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640",
        "topics": ["0x" + "c" * 64, "0x" + "d" * 64, "0x" + "e" * 64],
        "data": "0x" + "f" * 320,
        "removed": False,
    }


@pytest.fixture
def valid_swap_kwargs() -> dict:
    return {
        "chain": "ethereum",
        "block_number": 22040964,
        "block_timestamp": datetime(2026, 9, 26, 10, 0, tzinfo=UTC),
        "tx_hash": "0x" + "a" * 64,
        "log_index": 3,
        "pool_address": "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640",
        "sender": "0x" + "1" * 40,
        "recipient": "0x" + "2" * 40,
        "amount0": -1_500_000_000,
        "amount1": 850_000_000_000_000_000,
        "sqrt_price_x96": 1_234_567_890_123_456_789_012_345_678,
        "liquidity": 999_999_999_999_999,
        "tick": 202456,
    }


# ---------- RawSwapLog ----------


class TestRawSwapLog:
    def test_valid(self, valid_raw_log_kwargs):
        raw = RawSwapLog(**valid_raw_log_kwargs)
        assert raw.chain == "ethereum"
        assert raw.block_number == 22040964

    def test_address_lowercased(self, valid_raw_log_kwargs):
        raw = RawSwapLog(**valid_raw_log_kwargs)
        assert raw.address == "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"
        assert raw.address == raw.address.lower()

    def test_topics_lowercased(self, valid_raw_log_kwargs):
        kwargs = {**valid_raw_log_kwargs, "topics": ["0x" + "A" * 64]}
        raw = RawSwapLog(**kwargs)
        assert raw.topics == ["0x" + "a" * 64]

    def test_invalid_address_rejected(self, valid_raw_log_kwargs):
        kwargs = {**valid_raw_log_kwargs, "address": "not-an-address"}
        with pytest.raises(ValidationError):
            RawSwapLog(**kwargs)

    def test_invalid_chain_rejected(self, valid_raw_log_kwargs):
        kwargs = {**valid_raw_log_kwargs, "chain": "arbitrum"}
        with pytest.raises(ValidationError):
            RawSwapLog(**kwargs)

    def test_negative_block_number_rejected(self, valid_raw_log_kwargs):
        kwargs = {**valid_raw_log_kwargs, "block_number": -1}
        with pytest.raises(ValidationError):
            RawSwapLog(**kwargs)

    def test_frozen(self, valid_raw_log_kwargs):
        raw = RawSwapLog(**valid_raw_log_kwargs)
        with pytest.raises(ValidationError):
            raw.block_number = 99999

    def test_removed_defaults_false(self, valid_raw_log_kwargs):
        kwargs = {k: v for k, v in valid_raw_log_kwargs.items() if k != "removed"}
        raw = RawSwapLog(**kwargs)
        assert raw.removed is False

    def test_negative_transaction_index_rejected(self, valid_raw_log_kwargs):
        kwargs = {**valid_raw_log_kwargs, "transaction_index": -1}
        with pytest.raises(ValidationError):
            RawSwapLog(**kwargs)


# ---------- UniswapV3Swap ----------


class TestUniswapV3Swap:
    def test_valid(self, valid_swap_kwargs):
        swap = UniswapV3Swap(**valid_swap_kwargs)
        assert swap.chain == "ethereum"
        assert swap.amount0 < 0
        assert swap.amount1 > 0

    def test_addresses_lowercased(self, valid_swap_kwargs):
        swap = UniswapV3Swap(**valid_swap_kwargs)
        assert swap.pool_address == swap.pool_address.lower()
        assert swap.sender == swap.sender.lower()
        assert swap.recipient == swap.recipient.lower()

    def test_invalid_chain_rejected(self, valid_swap_kwargs):
        kwargs = {**valid_swap_kwargs, "chain": "solana"}
        with pytest.raises(ValidationError):
            UniswapV3Swap(**kwargs)

    def test_negative_sqrt_price_rejected(self, valid_swap_kwargs):
        kwargs = {**valid_swap_kwargs, "sqrt_price_x96": -1}
        with pytest.raises(ValidationError):
            UniswapV3Swap(**kwargs)

    def test_negative_liquidity_rejected(self, valid_swap_kwargs):
        kwargs = {**valid_swap_kwargs, "liquidity": -1}
        with pytest.raises(ValidationError):
            UniswapV3Swap(**kwargs)

    def test_tick_can_be_negative(self, valid_swap_kwargs):
        # tick is int24 signed — negative values are legal
        kwargs = {**valid_swap_kwargs, "tick": -100000}
        swap = UniswapV3Swap(**kwargs)
        assert swap.tick == -100000

    def test_amounts_can_be_negative(self, valid_swap_kwargs):
        # amount0/amount1 are signed int256 — negative means out of pool
        swap = UniswapV3Swap(**valid_swap_kwargs)
        assert swap.amount0 == -1_500_000_000

    def test_frozen(self, valid_swap_kwargs):
        swap = UniswapV3Swap(**valid_swap_kwargs)
        with pytest.raises(ValidationError):
            swap.tick = 0
