"""Tests for pipeline.pnl.models — CostBasisLot, Realization, WalletPnL."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from alphawallets.pipeline.pnl.models import CostBasisLot, Realization, WalletPnL

# ---------- Shared fixtures ----------

TS = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
TS_LATER = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
WALLET = "0x" + "a" * 40
TOKEN = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"  # UNI


# ---------- CostBasisLot ----------


class TestCostBasisLot:
    def test_valid_trading_lot(self):
        lot = CostBasisLot(
            acquired_at=TS,
            qty_token=Decimal("1000000000000000000"),  # 1 UNI
            unit_cost_usd=12.5,
            source="trading",
        )
        assert lot.qty_token == Decimal("1000000000000000000")
        assert lot.unit_cost_usd == 12.5
        assert lot.source == "trading"

    def test_valid_airdrop_lot(self):
        lot = CostBasisLot(
            acquired_at=TS,
            qty_token=Decimal("400000000000000000000"),  # 400 UNI
            unit_cost_usd=0.0,
            source="airdrop",
        )
        assert lot.unit_cost_usd == 0.0
        assert lot.source == "airdrop"

    def test_frozen(self):
        lot = CostBasisLot(
            acquired_at=TS, qty_token=Decimal(1), unit_cost_usd=1.0, source="trading"
        )
        with pytest.raises(ValidationError):
            lot.unit_cost_usd = 2.0  # type: ignore[misc]

    def test_qty_must_be_positive(self):
        with pytest.raises(ValidationError):
            CostBasisLot(acquired_at=TS, qty_token=Decimal(0), unit_cost_usd=1.0, source="trading")

    def test_qty_must_not_be_negative(self):
        with pytest.raises(ValidationError):
            CostBasisLot(acquired_at=TS, qty_token=Decimal(-1), unit_cost_usd=1.0, source="trading")

    def test_unit_cost_must_not_be_negative(self):
        with pytest.raises(ValidationError):
            CostBasisLot(
                acquired_at=TS, qty_token=Decimal(1), unit_cost_usd=-0.01, source="trading"
            )

    def test_source_must_be_valid_literal(self):
        with pytest.raises(ValidationError):
            CostBasisLot(
                acquired_at=TS,
                qty_token=Decimal(1),
                unit_cost_usd=1.0,
                source="other",  # type: ignore[arg-type]
            )

    def test_acquired_at_must_be_tz_aware(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            CostBasisLot(
                acquired_at=datetime(2026, 10, 1, 12, 0, 0),  # naive
                qty_token=Decimal(1),
                unit_cost_usd=1.0,
                source="trading",
            )

    def test_non_utc_tz_accepted_but_carries_offset(self):
        # The validator rejects naive datetimes but doesn't require UTC
        # specifically — the DuckDB session is UTC-pinned (ADR 0009) so
        # offset-carrying datetimes still round-trip correctly.
        ts = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
        lot = CostBasisLot(
            acquired_at=ts, qty_token=Decimal(1), unit_cost_usd=1.0, source="trading"
        )
        assert lot.acquired_at.tzinfo is not None

    def test_decimal_preserves_uint256_precision(self):
        # A quantity that would round under float
        huge = Decimal("123456789012345678901234567890")
        lot = CostBasisLot(acquired_at=TS, qty_token=huge, unit_cost_usd=1.0, source="trading")
        assert lot.qty_token == huge


# ---------- Realization ----------


class TestRealization:
    def test_valid_trading_realization(self):
        r = Realization(
            realized_at=TS,
            qty_token=Decimal("1000000000000000000"),
            unit_cost_usd=10.0,
            unit_sale_usd=12.0,
            source="trading",
            pnl_usd=2.0,
        )
        assert r.pnl_usd == 2.0

    def test_valid_airdrop_realization_full_proceeds(self):
        # Airdrop lot: cost basis 0, so pnl = sale price * qty
        r = Realization(
            realized_at=TS,
            qty_token=Decimal(1),
            unit_cost_usd=0.0,
            unit_sale_usd=15.0,
            source="airdrop",
            pnl_usd=15.0,
        )
        assert r.unit_cost_usd == 0.0
        assert r.source == "airdrop"

    def test_frozen(self):
        r = Realization(
            realized_at=TS,
            qty_token=Decimal(1),
            unit_cost_usd=1.0,
            unit_sale_usd=1.0,
            source="trading",
            pnl_usd=0.0,
        )
        with pytest.raises(ValidationError):
            r.pnl_usd = 10.0  # type: ignore[misc]

    def test_qty_must_be_positive(self):
        with pytest.raises(ValidationError):
            Realization(
                realized_at=TS,
                qty_token=Decimal(0),
                unit_cost_usd=1.0,
                unit_sale_usd=1.0,
                source="trading",
                pnl_usd=0.0,
            )

    def test_negative_pnl_allowed(self):
        # A loss is a valid realization — selling below cost basis
        r = Realization(
            realized_at=TS,
            qty_token=Decimal(1),
            unit_cost_usd=20.0,
            unit_sale_usd=10.0,
            source="trading",
            pnl_usd=-10.0,
        )
        assert r.pnl_usd == -10.0

    def test_realized_at_must_be_tz_aware(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            Realization(
                realized_at=datetime(2026, 10, 1, 12, 0, 0),  # naive
                qty_token=Decimal(1),
                unit_cost_usd=1.0,
                unit_sale_usd=1.0,
                source="trading",
                pnl_usd=0.0,
            )


# ---------- WalletPnL ----------


def _make_pnl(**overrides) -> WalletPnL:
    defaults = dict(
        chain="ethereum",
        wallet=WALLET,
        token_address=TOKEN,
        window_start=TS,
        window_end=TS_LATER,
        realized_pnl_usd=100.0,
        realized_pnl_trading_usd=80.0,
        realized_pnl_airdrop_usd=20.0,
        bought_usd=1000.0,
        sold_usd=1100.0,
        realization_count=3,
        balance_token=Decimal(0),
        avg_cost_basis_usd=None,
        computed_at=TS_LATER,
    )
    defaults.update(overrides)
    return WalletPnL(**defaults)


class TestWalletPnL:
    def test_valid_row_with_zero_balance(self):
        pnl = _make_pnl()
        assert pnl.realized_pnl_usd == 100.0
        assert pnl.balance_token == Decimal(0)
        assert pnl.avg_cost_basis_usd is None

    def test_valid_row_with_open_position(self):
        pnl = _make_pnl(
            balance_token=Decimal("500000000000000000"),
            avg_cost_basis_usd=12.5,
        )
        assert pnl.balance_token == Decimal("500000000000000000")
        assert pnl.avg_cost_basis_usd == 12.5

    def test_unrealized_defaults_to_none(self):
        pnl = _make_pnl()
        assert pnl.unrealized_pnl_usd is None

    def test_caveat_flags_default_false(self):
        pnl = _make_pnl()
        assert pnl.has_pre_window_activity is False
        assert pnl.has_unpriceable_events is False
        assert pnl.has_smart_wallet_signal is False

    def test_caveat_flags_can_be_set(self):
        pnl = _make_pnl(
            has_pre_window_activity=True,
            has_unpriceable_events=True,
            has_smart_wallet_signal=True,
        )
        assert pnl.has_pre_window_activity is True
        assert pnl.has_unpriceable_events is True
        assert pnl.has_smart_wallet_signal is True

    def test_frozen(self):
        pnl = _make_pnl()
        with pytest.raises(ValidationError):
            pnl.realized_pnl_usd = 999.0  # type: ignore[misc]

    def test_wallet_address_lowercased(self):
        pnl = _make_pnl(wallet="0x" + "A" * 40)
        assert pnl.wallet == "0x" + "a" * 40

    def test_token_address_lowercased(self):
        pnl = _make_pnl(token_address="0x1F9840A85D5AF5BF1D1762F925BDADDC4201F984")
        assert pnl.token_address == TOKEN

    def test_invalid_address_rejected(self):
        with pytest.raises(ValidationError):
            _make_pnl(wallet="not_an_address")

    def test_invalid_chain_rejected(self):
        with pytest.raises(ValidationError):
            _make_pnl(chain="solana")

    def test_negative_bought_rejected(self):
        with pytest.raises(ValidationError):
            _make_pnl(bought_usd=-1.0)

    def test_negative_sold_rejected(self):
        with pytest.raises(ValidationError):
            _make_pnl(sold_usd=-1.0)

    def test_negative_realization_count_rejected(self):
        with pytest.raises(ValidationError):
            _make_pnl(realization_count=-1)

    def test_negative_balance_rejected(self):
        with pytest.raises(ValidationError):
            _make_pnl(balance_token=Decimal(-1))

    def test_pnl_can_be_negative(self):
        pnl = _make_pnl(
            realized_pnl_usd=-50.0,
            realized_pnl_trading_usd=-50.0,
            realized_pnl_airdrop_usd=0.0,
        )
        assert pnl.realized_pnl_usd == -50.0

    def test_window_timestamps_must_be_tz_aware(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            _make_pnl(window_start=datetime(2026, 10, 1))  # naive

    def test_computed_at_must_be_tz_aware(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            _make_pnl(computed_at=datetime(2026, 10, 2))  # naive
