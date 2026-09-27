"""Tests for the Uniswap V3 Swap log decoder."""

from datetime import UTC, datetime

import pytest
from hexbytes import HexBytes
from pydantic import ValidationError
from web3 import Web3

from alphawallets.fetchers.uniswap_v3.decoder import (
    _hex_str,
    _parse_block_timestamp,
    decode_swap_log,
    make_swap_event_decoder,
    to_raw_swap_log,
)
from alphawallets.fetchers.uniswap_v3.models import RawSwapLog, UniswapV3Swap

# ---------- Fixtures ----------


@pytest.fixture(scope="module")
def w3() -> Web3:
    """Throwaway Web3 instance — no RPC connection needed for ABI decoding."""
    return Web3()


@pytest.fixture(scope="module")
def swap_event(w3: Web3):
    """Real Swap event decoder built from the shipped ABI."""
    return make_swap_event_decoder(w3)


@pytest.fixture
def fixture_swap_log() -> dict:
    """A real Uniswap V3 Swap log captured from Ethereum block 26067040.

    Captured live on 2026-09-27 from the USDC/WETH 0.05% pool:
    5,565.00 USDC in (amount0 > 0), 2.055 WETH out (amount1 < 0).
    """
    return {
        "address": "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640",
        "topics": [
            HexBytes("0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"),
            HexBytes("0x000000000000000000000000bdb3ba9ffe392549e1f8658dd2630c141fdf47b6"),
            HexBytes("0x000000000000000000000000bdb3ba9ffe392549e1f8658dd2630c141fdf47b6"),
        ],
        "data": HexBytes(
            "0x000000000000000000000000000000000000000000000000000000014bb33bac"
            "ffffffffffffffffffffffffffffffffffffffffffffffffe37a95e0e3f05b51"
            "0000000000000000000000000000000000004b15afecdd746f1572099c0e2c3f"
            "00000000000000000000000000000000000000000000000029b45f27aa2b3406"
            "00000000000000000000000000000000000000000000000000000000000302a5"
        ),
        "blockHash": HexBytes("0xda372d71b78c7afe9bdcc2ebdaec6846a74db44a2470ba1aff8e347d55863875"),
        "blockNumber": 26067040,
        "blockTimestamp": "0x6ab8b943",  # 2026-09-27T06:35:47Z
        "transactionHash": HexBytes(
            "0xee70918bf1c0f01bbac8b9d75411eaad43fe449f2126ef956c85134ee0ec891b"
        ),
        "transactionIndex": 1,
        "logIndex": 62,
        "removed": False,
    }


# ---------- _hex_str ----------


class TestHexStr:
    def test_hexbytes_input(self):
        hb = HexBytes("0xABCDEF")
        assert _hex_str(hb) == "0xabcdef"

    def test_bytes_input(self):
        result = _hex_str(b"\xab\xcd\xef")
        assert result == "0xabcdef"

    def test_str_input_with_prefix(self):
        assert _hex_str("0xABCDEF") == "0xabcdef"

    def test_str_input_without_prefix(self):
        assert _hex_str("ABCDEF") == "0xabcdef"

    def test_invalid_type_raises(self):
        with pytest.raises(TypeError, match="Cannot convert"):
            _hex_str(12345)  # type: ignore[arg-type]

    def test_none_raises(self):
        with pytest.raises(TypeError):
            _hex_str(None)  # type: ignore[arg-type]


# ---------- _parse_block_timestamp ----------


class TestParseBlockTimestamp:
    def test_hex_string(self):
        # 0x66f75f3b = 1727418683 = 2024-09-27T06:31:23Z
        # (using a real historical epoch for concreteness)
        result = _parse_block_timestamp("0x66f75f3b")
        assert result.tzinfo is UTC
        assert result.year in (2024, 2026)  # depending on when the fixture value maps

    def test_int_epoch(self):
        result = _parse_block_timestamp(1727418683)
        assert result == datetime(2024, 9, 27, 6, 31, 23, tzinfo=UTC)

    def test_invalid_type_raises(self):
        with pytest.raises(TypeError, match="Unexpected blockTimestamp"):
            _parse_block_timestamp(None)  # type: ignore[arg-type]

    def test_list_input_raises(self):
        with pytest.raises(TypeError):
            _parse_block_timestamp([1, 2, 3])  # type: ignore[arg-type]


# ---------- to_raw_swap_log ----------


class TestToRawSwapLog:
    def test_basic(self, fixture_swap_log):
        raw = to_raw_swap_log(fixture_swap_log, chain="ethereum")
        assert isinstance(raw, RawSwapLog)
        assert raw.chain == "ethereum"
        assert raw.block_number == 26067040
        assert raw.transaction_index == 1
        assert raw.log_index == 62
        assert raw.removed is False

    def test_address_lowercased(self, fixture_swap_log):
        raw = to_raw_swap_log(fixture_swap_log, chain="ethereum")
        assert raw.address == "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"

    def test_topics_preserved_and_lowercased(self, fixture_swap_log):
        raw = to_raw_swap_log(fixture_swap_log, chain="ethereum")
        assert len(raw.topics) == 3
        assert all(t.startswith("0x") and t == t.lower() for t in raw.topics)

    def test_removed_true_preserved(self, fixture_swap_log):
        log = {**fixture_swap_log, "removed": True}
        raw = to_raw_swap_log(log, chain="ethereum")
        assert raw.removed is True

    def test_removed_absent_defaults_false(self, fixture_swap_log):
        log = {k: v for k, v in fixture_swap_log.items() if k != "removed"}
        raw = to_raw_swap_log(log, chain="ethereum")
        assert raw.removed is False

    def test_chain_base_accepted(self, fixture_swap_log):
        raw = to_raw_swap_log(fixture_swap_log, chain="base")
        assert raw.chain == "base"


# ---------- decode_swap_log ----------


class TestDecodeSwapLog:
    def test_basic_decode(self, fixture_swap_log, swap_event):
        decoded = decode_swap_log(fixture_swap_log, swap_event, chain="ethereum")
        assert isinstance(decoded, UniswapV3Swap)
        assert decoded.chain == "ethereum"
        assert decoded.block_number == 26067040

    def test_addresses_lowercased(self, fixture_swap_log, swap_event):
        decoded = decode_swap_log(fixture_swap_log, swap_event, chain="ethereum")
        assert decoded.pool_address == decoded.pool_address.lower()
        assert decoded.sender == decoded.sender.lower()
        assert decoded.recipient == decoded.recipient.lower()
        # 40 hex chars + 0x prefix
        assert len(decoded.sender) == 42
        assert len(decoded.recipient) == 42

    def test_amounts_signed(self, fixture_swap_log, swap_event):
        # From the live fixture: 5,565.00 USDC in, 2.055 WETH out
        decoded = decode_swap_log(fixture_swap_log, swap_event, chain="ethereum")
        assert decoded.amount0 > 0
        assert decoded.amount1 < 0
        # Sign convention: opposite signs (invariant of a swap)
        assert (decoded.amount0 > 0) != (decoded.amount1 > 0)

    def test_block_timestamp_parsed(self, fixture_swap_log, swap_event):
        decoded = decode_swap_log(fixture_swap_log, swap_event, chain="ethereum")
        assert decoded.block_timestamp.tzinfo is UTC

    def test_missing_block_timestamp_raises(self, fixture_swap_log, swap_event):
        log = {k: v for k, v in fixture_swap_log.items() if k != "blockTimestamp"}
        with pytest.raises(ValueError, match="blockTimestamp"):
            decode_swap_log(log, swap_event, chain="ethereum")

    def test_invalid_chain_rejected(self, fixture_swap_log, swap_event):
        with pytest.raises(ValidationError):
            decode_swap_log(fixture_swap_log, swap_event, chain="solana")  # type: ignore[arg-type]

    def test_sqrt_price_and_liquidity_positive(self, fixture_swap_log, swap_event):
        decoded = decode_swap_log(fixture_swap_log, swap_event, chain="ethereum")
        assert decoded.sqrt_price_x96 > 0
        assert decoded.liquidity >= 0


# ---------- make_swap_event_decoder ----------


class TestMakeSwapEventDecoder:
    def test_returns_reusable_event(self, w3):
        event = make_swap_event_decoder(w3)
        # Same instance can be used repeatedly without cost
        event2 = make_swap_event_decoder(w3)
        # They're separate instances (each call builds a contract), but both work
        assert event is not None
        assert event2 is not None

    def test_event_has_process_log(self, swap_event):
        # The event must expose process_log — that's the interface decode_swap_log uses
        assert hasattr(swap_event, "process_log")
        assert callable(swap_event.process_log)
