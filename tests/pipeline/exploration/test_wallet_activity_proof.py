"""Tests for the wallet activity proof: the Event model and price classification."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from alphawallets.pipeline.exploration.wallet_activity_proof import (
    UNI_ETHEREUM,
    Event,
    classify_price_status,
)

# A fixed "now" so nothing in these tests depends on when they run. 06:38Z sits
# inside hour 06:00, mirroring the first live run where the newest price row was
# 05:00Z and seven swaps in hour 06 were pending.
NOW = datetime(2026, 10, 1, 6, 38, 11, tzinfo=UTC)
CURRENT_HOUR = datetime(2026, 10, 1, 6, 0, 0, tzinfo=UTC)
PAST_HOUR = datetime(2026, 10, 1, 5, 0, 0, tzinfo=UTC)

TX = "0x" + "a" * 64


def make_event(**overrides) -> Event:
    """Build a valid transfer Event, overriding any field."""
    fields = {
        "ts": PAST_HOUR + timedelta(minutes=17),
        "event_type": "transfer",
        "direction": "in",
        "amount_token": Decimal("1234.56"),
        "price_usd": 8.94,
        "price_status": "priced",
        "tx_hash": TX,
        "counterparty": "0x" + "b" * 40,
    }
    fields.update(overrides)
    return Event(**fields)


class TestEventBasics:
    def test_valid_transfer(self):
        event = make_event()
        assert event.event_type == "transfer"
        assert event.direction == "in"
        assert event.amount_token == Decimal("1234.56")

    def test_valid_swap(self):
        event = make_event(
            event_type="swap",
            direction="sell",
            counterparty=None,
            other_amount=Decimal("1.5"),
            other_token="WETH",
        )
        assert event.direction == "sell"
        assert event.other_token == "WETH"

    def test_frozen(self):
        event = make_event()
        with pytest.raises(ValidationError):
            event.amount_token = Decimal("1")

    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            make_event(ts=datetime(2026, 10, 1, 5, 17, 0))

    def test_tx_hash_lowercased(self):
        event = make_event(tx_hash="0x" + "A" * 64)
        assert event.tx_hash == "0x" + "a" * 64

    def test_counterparty_lowercased(self):
        event = make_event(counterparty="0x" + "B" * 40)
        assert event.counterparty == "0x" + "b" * 40

    def test_counterparty_may_be_none(self):
        assert make_event(counterparty=None).counterparty is None


class TestValueDerivation:
    def test_value_usd_derived_from_amount_and_price(self):
        event = make_event(amount_token=Decimal("100"), price_usd=8.5)
        assert event.value_usd == Decimal("850.0")

    def test_value_usd_is_decimal_not_float(self):
        """Decimal * float raises in Python; the model must not hand that to callers."""
        event = make_event(amount_token=Decimal("1234.56"), price_usd=8.94)
        assert isinstance(event.value_usd, Decimal)

    def test_derivation_keeps_decimal_precision(self):
        """float arithmetic would drift here; Decimal(str(...)) does not."""
        event = make_event(amount_token=Decimal("0.1"), price_usd=0.1)
        assert event.value_usd == Decimal("0.01")

    def test_explicit_value_usd_is_not_overwritten(self):
        event = make_event(value_usd=Decimal("999"))
        assert event.value_usd == Decimal("999")

    def test_unpriced_event_has_no_value(self):
        event = make_event(price_usd=None, price_status="pending")
        assert event.price_usd is None
        assert event.value_usd is None


class TestCoherenceInvariants:
    """Both of these would otherwise produce a plausible-looking timeline."""

    def test_swap_cannot_be_in(self):
        with pytest.raises(ValidationError, match="not valid for event_type"):
            make_event(event_type="swap", direction="in")

    def test_swap_cannot_be_out(self):
        with pytest.raises(ValidationError, match="not valid for event_type"):
            make_event(event_type="swap", direction="out")

    def test_transfer_cannot_be_buy(self):
        with pytest.raises(ValidationError, match="not valid for event_type"):
            make_event(event_type="transfer", direction="buy")

    def test_transfer_cannot_be_sell(self):
        with pytest.raises(ValidationError, match="not valid for event_type"):
            make_event(event_type="transfer", direction="sell")

    def test_priced_without_price_rejected(self):
        """A row claiming to be priced with no price would inflate coverage."""
        with pytest.raises(ValidationError, match="requires a price_usd"):
            make_event(price_status="priced", price_usd=None)

    def test_pending_with_price_rejected(self):
        with pytest.raises(ValidationError, match="must not carry a price_usd"):
            make_event(price_status="pending", price_usd=8.94)

    def test_unavailable_with_price_rejected(self):
        with pytest.raises(ValidationError, match="must not carry a price_usd"):
            make_event(price_status="unavailable", price_usd=8.94)

    def test_unknown_direction_rejected(self):
        with pytest.raises(ValidationError):
            make_event(direction="sideways")

    def test_unknown_event_type_rejected(self):
        with pytest.raises(ValidationError):
            make_event(event_type="mint")

    def test_unknown_price_status_rejected(self):
        with pytest.raises(ValidationError):
            make_event(price_status="maybe")


class TestClassifyPriceStatus:
    """The three-state split that keeps a structural lag from reading as a gap."""

    def test_price_row_present_is_priced(self):
        assert classify_price_status(PAST_HOUR, has_price_row=True, now_utc=NOW) == "priced"

    def test_current_hour_without_price_is_pending(self):
        assert classify_price_status(CURRENT_HOUR, has_price_row=False, now_utc=NOW) == "pending"

    def test_past_hour_without_price_is_unavailable(self):
        assert classify_price_status(PAST_HOUR, has_price_row=False, now_utc=NOW) == "unavailable"

    def test_current_hour_with_price_is_priced(self):
        """Once the grid catches up, the same hour is simply priced."""
        assert classify_price_status(CURRENT_HOUR, has_price_row=True, now_utc=NOW) == "priced"

    def test_future_hour_is_pending_not_unavailable(self):
        """Clock skew between the node and this host must not read as a gap."""
        future = CURRENT_HOUR + timedelta(hours=1)
        assert classify_price_status(future, has_price_row=False, now_utc=NOW) == "pending"

    def test_hour_boundary_exactly_at_current_hour_start(self):
        assert (
            classify_price_status(CURRENT_HOUR, has_price_row=False, now_utc=CURRENT_HOUR)
            == "pending"
        )

    def test_one_second_before_current_hour_is_unavailable(self):
        """The boundary is the hour, not the minute — 05:59:59 belongs to hour 05."""
        just_before = CURRENT_HOUR - timedelta(hours=1)
        assert classify_price_status(just_before, has_price_row=False, now_utc=NOW) == "unavailable"

    def test_now_utc_defaults_to_wall_clock(self):
        """Omitting now_utc must still classify, using the real clock."""
        long_ago = datetime(2020, 1, 1, tzinfo=UTC)
        assert classify_price_status(long_ago, has_price_row=False) == "unavailable"

    def test_non_utc_now_is_converted(self):
        """A caller passing a non-UTC aware datetime must not shift the boundary."""
        kolkata_now = NOW.astimezone(ZoneInfo("Asia/Kolkata"))
        assert (
            classify_price_status(CURRENT_HOUR, has_price_row=False, now_utc=kolkata_now)
            == "pending"
        )


class TestConstants:
    def test_uni_address_is_lowercase(self):
        """The cache stores lowercase; a checksummed constant would match nothing."""
        assert UNI_ETHEREUM.lower() == UNI_ETHEREUM
        assert len(UNI_ETHEREUM) == 42
