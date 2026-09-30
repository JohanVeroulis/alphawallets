"""Tests for the historical price models."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from alphawallets.fetchers.prices.models import RawPricePoint, TokenPrice

UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"


# ---------- Fixtures ----------


@pytest.fixture
def raw_kwargs() -> dict:
    return {
        "chain": "ethereum",
        "token_address": UNI,
        "timestamp": 1790747390,  # deliberately not hour-aligned
        "price": 8.86693292401652,
        "confidence": 0.99,
        "symbol": "UNI",
        "decimals": 18,
    }


@pytest.fixture
def price_kwargs() -> dict:
    return {
        "chain": "ethereum",
        "token_address": UNI,
        "ts": datetime(2026, 9, 30, 10, 0, tzinfo=UTC),
        "price_usd": 8.87,
        "confidence": 0.99,
        "source": "defillama",
        "fetched_at": datetime(2026, 9, 30, 11, 22, 33, tzinfo=UTC),
    }


# ---------- RawPricePoint ----------


class TestRawPricePoint:
    def test_valid(self, raw_kwargs):
        raw = RawPricePoint(**raw_kwargs)
        assert raw.chain == "ethereum"
        assert raw.timestamp == 1790747390
        assert raw.symbol == "UNI"
        assert raw.decimals == 18

    def test_address_lowercased(self, raw_kwargs):
        raw = RawPricePoint(**{**raw_kwargs, "token_address": UNI.upper().replace("0X", "0x")})
        assert raw.token_address == UNI

    def test_unaligned_timestamp_allowed(self, raw_kwargs):
        """The raw model must accept what the API actually sends."""
        raw = RawPricePoint(**raw_kwargs)
        assert raw.timestamp % 3600 != 0

    def test_optional_fields_default_none(self, raw_kwargs):
        minimal = {
            k: v for k, v in raw_kwargs.items() if k not in ("confidence", "symbol", "decimals")
        }
        raw = RawPricePoint(**minimal)
        assert raw.confidence is None
        assert raw.symbol is None
        assert raw.decimals is None

    def test_zero_price_allowed(self, raw_kwargs):
        # A delisted or illiquid token can legitimately report 0
        assert RawPricePoint(**{**raw_kwargs, "price": 0.0}).price == 0.0

    def test_negative_price_rejected(self, raw_kwargs):
        with pytest.raises(ValidationError):
            RawPricePoint(**{**raw_kwargs, "price": -1.0})

    def test_zero_timestamp_rejected(self, raw_kwargs):
        with pytest.raises(ValidationError):
            RawPricePoint(**{**raw_kwargs, "timestamp": 0})

    def test_confidence_above_one_rejected(self, raw_kwargs):
        with pytest.raises(ValidationError):
            RawPricePoint(**{**raw_kwargs, "confidence": 1.5})

    def test_invalid_address_rejected(self, raw_kwargs):
        with pytest.raises(ValidationError):
            RawPricePoint(**{**raw_kwargs, "token_address": "not-an-address"})

    def test_invalid_chain_rejected(self, raw_kwargs):
        with pytest.raises(ValidationError):
            RawPricePoint(**{**raw_kwargs, "chain": "solana"})

    def test_frozen(self, raw_kwargs):
        raw = RawPricePoint(**raw_kwargs)
        with pytest.raises(ValidationError):
            raw.price = 1.0


# ---------- TokenPrice ----------


class TestTokenPrice:
    def test_valid(self, price_kwargs):
        price = TokenPrice(**price_kwargs)
        assert price.chain == "ethereum"
        assert price.price_usd == 8.87
        assert price.source == "defillama"

    def test_source_defaults_to_defillama(self, price_kwargs):
        kwargs = {k: v for k, v in price_kwargs.items() if k != "source"}
        assert TokenPrice(**kwargs).source == "defillama"

    def test_alternate_source_accepted(self, price_kwargs):
        """The PK includes source so a fallback provider can coexist."""
        assert TokenPrice(**{**price_kwargs, "source": "coingecko"}).source == "coingecko"

    def test_address_lowercased(self, price_kwargs):
        price = TokenPrice(**{**price_kwargs, "token_address": UNI.upper().replace("0X", "0x")})
        assert price.token_address == UNI

    def test_unaligned_ts_rejected(self, price_kwargs):
        """The PnL join is date_trunc('hour', ...) — unaligned rows never match."""
        bad = datetime(2026, 9, 30, 10, 30, tzinfo=UTC)
        with pytest.raises(ValidationError, match="hour-aligned"):
            TokenPrice(**{**price_kwargs, "ts": bad})

    def test_ts_with_seconds_rejected(self, price_kwargs):
        bad = datetime(2026, 9, 30, 10, 0, 1, tzinfo=UTC)
        with pytest.raises(ValidationError, match="hour-aligned"):
            TokenPrice(**{**price_kwargs, "ts": bad})

    def test_ts_with_microseconds_rejected(self, price_kwargs):
        bad = datetime(2026, 9, 30, 10, 0, 0, 500, tzinfo=UTC)
        with pytest.raises(ValidationError, match="hour-aligned"):
            TokenPrice(**{**price_kwargs, "ts": bad})

    def test_naive_ts_rejected(self, price_kwargs):
        with pytest.raises(ValidationError, match="timezone-aware"):
            TokenPrice(**{**price_kwargs, "ts": datetime(2026, 9, 30, 10, 0)})

    def test_naive_fetched_at_rejected(self, price_kwargs):
        with pytest.raises(ValidationError, match="timezone-aware"):
            TokenPrice(**{**price_kwargs, "fetched_at": datetime(2026, 9, 30, 11, 0)})

    def test_non_utc_aligned_ts_accepted(self, price_kwargs):
        """Any aware tz is accepted as long as the wall clock is hour-aligned."""
        athens = timezone(timedelta(hours=3))
        price = TokenPrice(**{**price_kwargs, "ts": datetime(2026, 9, 30, 13, 0, tzinfo=athens)})
        assert price.ts.utcoffset() is not None

    def test_negative_price_rejected(self, price_kwargs):
        with pytest.raises(ValidationError):
            TokenPrice(**{**price_kwargs, "price_usd": -0.01})

    def test_confidence_optional(self, price_kwargs):
        kwargs = {k: v for k, v in price_kwargs.items() if k != "confidence"}
        assert TokenPrice(**kwargs).confidence is None

    def test_frozen(self, price_kwargs):
        price = TokenPrice(**price_kwargs)
        with pytest.raises(ValidationError):
            price.price_usd = 1.0
