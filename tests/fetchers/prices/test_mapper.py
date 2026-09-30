"""Tests for the DefiLlama price mapper.

Fixtures use timestamps and values taken from real /chart probe responses for
UNI on Ethereum, which is why none of them are hour-aligned.
"""

from datetime import UTC, datetime

import pytest

from alphawallets.fetchers.prices.mapper import (
    align_to_hour,
    to_raw_price_point,
    to_token_price,
)
from alphawallets.fetchers.prices.models import RawPricePoint, TokenPrice

UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"


# ---------- Fixtures ----------


@pytest.fixture
def probe_entry() -> dict:
    """A real entry from the 2026-09-30 probe: 2026-09-30T05:49:50Z."""
    return {
        "timestamp": 1790747390,
        "price": 8.86693292401652,
        "symbol": "UNI",
        "decimals": 18,
        "confidence": 0.99,
    }


@pytest.fixture
def raw(probe_entry: dict) -> RawPricePoint:
    point = to_raw_price_point(probe_entry, chain="ethereum", token_address=UNI)
    assert point is not None
    return point


# ---------- align_to_hour ----------


class TestAlignToHour:
    def test_truncates_to_hour_start(self):
        # 1790747390 = 2026-09-30T05:49:50Z
        assert align_to_hour(1790747390) == datetime(2026, 9, 30, 5, 0, tzinfo=UTC)

    def test_truncates_down_not_round(self):
        """10:58 belongs to hour 10, not 11 — rounding would invent an observation."""
        late_in_hour = int(datetime(2026, 9, 30, 10, 58, 30, tzinfo=UTC).timestamp())
        assert align_to_hour(late_in_hour) == datetime(2026, 9, 30, 10, 0, tzinfo=UTC)

    def test_exact_hour_unchanged(self):
        on_hour = int(datetime(2026, 9, 30, 10, 0, tzinfo=UTC).timestamp())
        assert align_to_hour(on_hour) == datetime(2026, 9, 30, 10, 0, tzinfo=UTC)

    def test_one_second_before_hour_stays_in_previous(self):
        just_before = int(datetime(2026, 9, 30, 10, 59, 59, tzinfo=UTC).timestamp())
        assert align_to_hour(just_before) == datetime(2026, 9, 30, 10, 0, tzinfo=UTC)

    def test_result_is_utc_aware(self):
        result = align_to_hour(1790747390)
        assert result.tzinfo is not None
        assert result.utcoffset().total_seconds() == 0

    def test_result_passes_tokenprice_alignment_validator(self):
        """The whole point: alignment output must satisfy the model invariant."""
        aligned = align_to_hour(1790747390)
        assert (aligned.minute, aligned.second, aligned.microsecond) == (0, 0, 0)


# ---------- to_raw_price_point ----------


class TestToRawPricePoint:
    def test_probe_entry_maps(self, probe_entry):
        point = to_raw_price_point(probe_entry, chain="ethereum", token_address=UNI)
        assert isinstance(point, RawPricePoint)
        assert point.timestamp == 1790747390
        assert point.price == 8.86693292401652
        assert point.confidence == 0.99
        assert point.symbol == "UNI"
        assert point.decimals == 18

    def test_timestamp_kept_unaligned(self, probe_entry):
        """The raw model preserves what the API sent, quirks included."""
        point = to_raw_price_point(probe_entry, chain="ethereum", token_address=UNI)
        assert point.timestamp % 3600 != 0

    def test_address_lowercased(self, probe_entry):
        point = to_raw_price_point(
            probe_entry, chain="ethereum", token_address=UNI.upper().replace("0X", "0x")
        )
        assert point.token_address == UNI

    def test_base_chain_accepted(self, probe_entry):
        point = to_raw_price_point(probe_entry, chain="base", token_address=UNI)
        assert point.chain == "base"

    def test_missing_confidence_becomes_none(self, probe_entry):
        entry = {k: v for k, v in probe_entry.items() if k != "confidence"}
        point = to_raw_price_point(entry, chain="ethereum", token_address=UNI)
        assert point is not None
        assert point.confidence is None

    def test_null_confidence_becomes_none(self, probe_entry):
        point = to_raw_price_point(
            {**probe_entry, "confidence": None}, chain="ethereum", token_address=UNI
        )
        assert point.confidence is None

    def test_missing_symbol_and_decimals_tolerated(self, probe_entry):
        entry = {"timestamp": probe_entry["timestamp"], "price": probe_entry["price"]}
        point = to_raw_price_point(entry, chain="ethereum", token_address=UNI)
        assert point is not None
        assert point.symbol is None
        assert point.decimals is None

    def test_zero_price_mapped_not_skipped(self, probe_entry):
        """A delisted or fully illiquid token can legitimately report 0."""
        point = to_raw_price_point(
            {**probe_entry, "price": 0.0}, chain="ethereum", token_address=UNI
        )
        assert point is not None
        assert point.price == 0.0

    def test_integer_price_coerced_to_float(self, probe_entry):
        point = to_raw_price_point({**probe_entry, "price": 9}, chain="ethereum", token_address=UNI)
        assert point.price == 9.0

    def test_string_timestamp_coerced(self, probe_entry):
        point = to_raw_price_point(
            {**probe_entry, "timestamp": "1790747390"}, chain="ethereum", token_address=UNI
        )
        assert point.timestamp == 1790747390

    def test_missing_timestamp_skipped(self, probe_entry):
        entry = {k: v for k, v in probe_entry.items() if k != "timestamp"}
        assert to_raw_price_point(entry, chain="ethereum", token_address=UNI) is None

    def test_missing_price_skipped(self, probe_entry):
        entry = {k: v for k, v in probe_entry.items() if k != "price"}
        assert to_raw_price_point(entry, chain="ethereum", token_address=UNI) is None

    def test_negative_price_skipped(self, probe_entry):
        assert (
            to_raw_price_point({**probe_entry, "price": -1.0}, chain="ethereum", token_address=UNI)
            is None
        )

    def test_unparseable_price_skipped(self, probe_entry):
        assert (
            to_raw_price_point(
                {**probe_entry, "price": "not-a-number"}, chain="ethereum", token_address=UNI
            )
            is None
        )

    def test_confidence_out_of_range_skipped(self, probe_entry):
        assert (
            to_raw_price_point(
                {**probe_entry, "confidence": 5}, chain="ethereum", token_address=UNI
            )
            is None
        )

    def test_skip_logs_warning(self, probe_entry, caplog):
        entry = {k: v for k, v in probe_entry.items() if k != "price"}
        with caplog.at_level("WARNING"):
            to_raw_price_point(entry, chain="ethereum", token_address=UNI)
        assert "Skipping price point" in caplog.text


# ---------- to_token_price ----------


class TestToTokenPrice:
    def test_produces_aligned_row(self, raw):
        price = to_token_price(raw)
        assert isinstance(price, TokenPrice)
        assert price.ts == datetime(2026, 9, 30, 5, 0, tzinfo=UTC)

    def test_fields_carry_over(self, raw):
        price = to_token_price(raw)
        assert price.chain == raw.chain
        assert price.token_address == raw.token_address
        assert price.price_usd == raw.price
        assert price.confidence == raw.confidence

    def test_source_defaults_to_defillama(self, raw):
        assert to_token_price(raw).source == "defillama"

    def test_source_override(self, raw):
        assert to_token_price(raw, source="coingecko").source == "coingecko"

    def test_fetched_at_defaults_to_now(self, raw):
        before = datetime.now(tz=UTC)
        price = to_token_price(raw)
        assert before <= price.fetched_at <= datetime.now(tz=UTC)

    def test_fetched_at_override(self, raw):
        stamp = datetime(2026, 9, 30, 11, 22, 33, tzinfo=UTC)
        assert to_token_price(raw, fetched_at=stamp).fetched_at == stamp

    def test_zero_price_carried(self, probe_entry):
        raw_zero = to_raw_price_point(
            {**probe_entry, "price": 0.0}, chain="ethereum", token_address=UNI
        )
        assert to_token_price(raw_zero).price_usd == 0.0

    def test_adjacent_points_in_same_hour_produce_two_rows_with_identical_ts(self):
        """Mapper normalizes, it does not deduplicate.

        Two raw observations inside one hour (10:02 and 10:58) must both map,
        with the same ts. The writer's INSERT OR IGNORE resolves the collision
        and the orchestrator counts it, so the overlap stays visible rather
        than vanishing mid-pipeline.
        """
        early = int(datetime(2026, 9, 30, 10, 2, 11, tzinfo=UTC).timestamp())
        late = int(datetime(2026, 9, 30, 10, 58, 44, tzinfo=UTC).timestamp())

        raws = [
            to_raw_price_point(
                {"timestamp": ts, "price": price, "confidence": 0.99},
                chain="ethereum",
                token_address=UNI,
            )
            for ts, price in ((early, 8.80), (late, 8.95))
        ]
        prices = [to_token_price(r) for r in raws]

        assert len(prices) == 2, "mapper must not collapse same-hour points"
        assert prices[0].ts == prices[1].ts == datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
        # Distinct observations, distinct prices — only the hour bucket matches
        assert prices[0].price_usd != prices[1].price_usd
        assert raws[0].timestamp != raws[1].timestamp

    def test_full_chain_from_probe_entry(self, probe_entry):
        """entry -> RawPricePoint -> TokenPrice, as the orchestrator will run it."""
        raw_point = to_raw_price_point(probe_entry, chain="ethereum", token_address=UNI)
        price = to_token_price(raw_point, fetched_at=datetime(2026, 9, 30, 12, 0, tzinfo=UTC))
        assert price.ts == datetime(2026, 9, 30, 5, 0, tzinfo=UTC)
        assert price.price_usd == pytest.approx(8.86693292401652)
        assert price.source == "defillama"
