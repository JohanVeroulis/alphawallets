"""Tests for the FIFO cost-basis engine (ADR 0012 step 2)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from alphawallets.pipeline.pnl.cost_basis import FIFOEngine, InsufficientBalanceError

WALLET = "0x" + "1" * 40
UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"

T0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)
T3 = T0 + timedelta(hours=3)

WEI = Decimal(10) ** 18  # one whole token at 18 decimals


def engine(decimals: int = 18) -> FIFOEngine:
    return FIFOEngine(wallet=WALLET, token_address=UNI, token_decimals=decimals)


class TestConstruction:
    def test_starts_empty(self):
        e = engine()
        assert e.balance_token() == Decimal(0)
        assert e.lots_snapshot() == []
        assert e.avg_cost_basis_usd() is None

    def test_addresses_lowercased(self):
        e = FIFOEngine(wallet=WALLET.upper(), token_address=UNI.upper(), token_decimals=18)
        assert e.wallet == WALLET
        assert e.token_address == UNI

    def test_negative_decimals_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            FIFOEngine(wallet=WALLET, token_address=UNI, token_decimals=-1)

    def test_zero_decimals_is_valid(self):
        """Some tokens have no fractional part."""
        e = engine(decimals=0)
        e.add_lot(T0, Decimal(5), 2.0, "trading")
        assert e.balance_token() == Decimal(5)
        # With 0 decimals, 5 base units are 5 whole tokens.
        assert e.consume(T1, Decimal(5), 3.0)[0].pnl_usd == pytest.approx(5.0)

    def test_balance_by_source_both_keys_present_when_empty(self):
        """Both keys always present, so a caller needs no default."""
        assert engine().balance_token_by_source() == {
            "trading": Decimal(0),
            "airdrop": Decimal(0),
        }

    def test_repr_is_informative(self):
        e = engine()
        e.add_lot(T0, WEI, 1.0, "trading")
        text = repr(e)
        assert UNI in text
        assert "lots=1" in text


class TestAddLot:
    def test_single_trading_lot(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        assert e.balance_token() == WEI
        snapshot = e.lots_snapshot()
        assert len(snapshot) == 1
        assert snapshot[0].source == "trading"
        assert snapshot[0].unit_cost_usd == 10.0

    def test_airdrop_lot_at_zero_cost(self):
        """Airdropped tokens genuinely were free (ADR 0012 decision 4)."""
        e = engine()
        e.add_lot(T0, WEI, 0.0, "airdrop")
        assert e.balance_token() == WEI
        assert e.lots_snapshot()[0].unit_cost_usd == 0.0

    def test_zero_quantity_rejected(self):
        with pytest.raises(ValidationError):
            engine().add_lot(T0, Decimal(0), 1.0, "trading")

    def test_negative_quantity_rejected(self):
        with pytest.raises(ValidationError):
            engine().add_lot(T0, Decimal(-1), 1.0, "trading")

    def test_negative_cost_rejected(self):
        with pytest.raises(ValidationError):
            engine().add_lot(T0, WEI, -1.0, "trading")

    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValidationError):
            engine().add_lot(datetime(2026, 10, 1), WEI, 1.0, "trading")

    def test_unknown_source_rejected(self):
        with pytest.raises(ValidationError):
            engine().add_lot(T0, WEI, 1.0, "mining")

    def test_balance_by_source_splits_mixed_adds(self):
        e = engine()
        e.add_lot(T0, WEI * 2, 10.0, "trading")
        e.add_lot(T1, WEI * 3, 0.0, "airdrop")
        assert e.balance_token_by_source() == {
            "trading": WEI * 2,
            "airdrop": WEI * 3,
        }
        assert e.balance_token() == WEI * 5

    def test_lots_stay_in_arrival_order(self):
        e = engine()
        e.add_lot(T0, WEI, 1.0, "trading")
        e.add_lot(T1, WEI, 2.0, "trading")
        assert [lot.acquired_at for lot in e.lots_snapshot()] == [T0, T1]


class TestConsumeSingleLot:
    def test_full_consumption_empties_the_stack(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        realizations = e.consume(T1, WEI, 12.0)
        assert len(realizations) == 1
        assert realizations[0].qty_token == WEI
        assert realizations[0].source == "trading"
        assert e.balance_token() == Decimal(0)
        assert e.lots_snapshot() == []

    def test_partial_consumption_leaves_the_remainder(self):
        e = engine()
        e.add_lot(T0, WEI * 10, 10.0, "trading")
        realizations = e.consume(T1, WEI * 3, 12.0)
        assert len(realizations) == 1
        assert realizations[0].qty_token == WEI * 3
        assert e.balance_token() == WEI * 7
        remaining = e.lots_snapshot()
        assert len(remaining) == 1
        assert remaining[0].qty_token == WEI * 7
        # The replacement keeps the original acquisition time and cost.
        assert remaining[0].acquired_at == T0
        assert remaining[0].unit_cost_usd == 10.0

    def test_realization_carries_both_prices(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        r = e.consume(T1, WEI, 12.0)[0]
        assert r.unit_cost_usd == 10.0
        assert r.unit_sale_usd == 12.0
        assert r.realized_at == T1

    def test_zero_quantity_rejected(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        with pytest.raises(ValueError, match="must be positive"):
            e.consume(T1, Decimal(0), 12.0)

    def test_negative_quantity_rejected(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        with pytest.raises(ValueError, match="must be positive"):
            e.consume(T1, Decimal(-1), 12.0)

    def test_consume_on_an_empty_stack_raises(self):
        with pytest.raises(InsufficientBalanceError):
            engine().consume(T1, WEI, 12.0)


class TestInsufficientBalance:
    def test_raises_rather_than_clamping(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        with pytest.raises(InsufficientBalanceError):
            e.consume(T1, WEI * 2, 12.0)

    def test_message_names_the_four_diagnostic_values(self):
        """Wallet, token, requested and available are what identify the missing event."""
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        with pytest.raises(InsufficientBalanceError) as excinfo:
            e.consume(T1, WEI * 3, 12.0)
        message = str(excinfo.value)
        assert WALLET in message
        assert UNI in message
        assert str(WEI * 3) in message  # requested
        assert str(WEI) in message  # available

    def test_is_a_valueerror(self):
        """So a caller catching ValueError around the arithmetic still catches it."""
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        with pytest.raises(ValueError):
            e.consume(T1, WEI * 2, 12.0)

    def test_stack_untouched_after_a_failed_consume(self):
        """The check runs before any consumption, so there is no half-consumed state."""
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        e.add_lot(T1, WEI, 11.0, "trading")
        before = e.lots_snapshot()
        with pytest.raises(InsufficientBalanceError):
            e.consume(T2, WEI * 5, 12.0)
        assert e.lots_snapshot() == before
        assert e.balance_token() == WEI * 2

    def test_exact_balance_is_not_insufficient(self):
        """The boundary is inclusive — selling the whole position is normal."""
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        assert len(e.consume(T1, WEI, 12.0)) == 1


class TestConsumeMultiLot:
    def test_sale_spanning_two_trading_lots(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        e.add_lot(T1, WEI, 20.0, "trading")
        realizations = e.consume(T2, WEI * 2, 30.0)

        assert len(realizations) == 2
        assert sum(r.qty_token for r in realizations) == WEI * 2
        # Each carries its own lot's cost basis, oldest first.
        assert [r.unit_cost_usd for r in realizations] == [10.0, 20.0]
        assert e.balance_token() == Decimal(0)

    def test_oldest_first_across_sources(self):
        """Unified FIFO: an older airdrop lot is consumed before a newer trading one.

        Partitioned queues would bake in a harvest policy; fungibility on-chain
        means the engine should not make that choice.
        """
        e = engine()
        e.add_lot(T0, WEI, 0.0, "airdrop")
        e.add_lot(T1, WEI, 10.0, "trading")
        realizations = e.consume(T2, WEI * 2, 15.0)
        assert [r.source for r in realizations] == ["airdrop", "trading"]

    def test_trading_then_airdrop_when_that_is_the_age_order(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        e.add_lot(T1, WEI, 0.0, "airdrop")
        realizations = e.consume(T2, WEI * 2, 15.0)
        assert [r.source for r in realizations] == ["trading", "airdrop"]
        assert [r.unit_cost_usd for r in realizations] == [10.0, 0.0]

    def test_sale_crossing_the_boundary_splits_pnl_by_source(self):
        """One sale, two Realizations, each attributable to its own bucket."""
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        e.add_lot(T1, WEI, 0.0, "airdrop")
        realizations = e.consume(T2, WEI * 2, 15.0)
        by_source = {r.source: r.pnl_usd for r in realizations}
        assert by_source["trading"] == pytest.approx(5.0)
        assert by_source["airdrop"] == pytest.approx(15.0)

    def test_sale_spanning_three_lots_full_full_partial(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        e.add_lot(T1, WEI, 20.0, "trading")
        e.add_lot(T2, WEI * 5, 30.0, "trading")
        realizations = e.consume(T3, WEI * 3, 40.0)

        assert len(realizations) == 3
        assert [r.qty_token for r in realizations] == [WEI, WEI, WEI]
        # Only the third lot survives, reduced by what was taken.
        remaining = e.lots_snapshot()
        assert len(remaining) == 1
        assert remaining[0].qty_token == WEI * 4
        assert remaining[0].acquired_at == T2

    def test_partial_lot_stays_at_the_front(self):
        """The partially-consumed lot is still the oldest, so it goes next."""
        e = engine()
        e.add_lot(T0, WEI * 3, 10.0, "trading")
        e.add_lot(T1, WEI, 20.0, "trading")
        e.consume(T2, WEI, 15.0)
        assert [lot.acquired_at for lot in e.lots_snapshot()] == [T0, T1]
        # The next sale continues from the same lot.
        assert e.consume(T3, WEI, 15.0)[0].unit_cost_usd == 10.0

    def test_successive_sales_walk_the_queue(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        e.add_lot(T1, WEI, 20.0, "trading")
        first = e.consume(T2, WEI, 15.0)
        second = e.consume(T3, WEI, 15.0)
        assert first[0].unit_cost_usd == 10.0
        assert second[0].unit_cost_usd == 20.0


class TestPnLMath:
    def test_gain(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        assert e.consume(T1, WEI, 12.0)[0].pnl_usd == pytest.approx(2.0)

    def test_loss(self):
        e = engine()
        e.add_lot(T0, WEI, 20.0, "trading")
        assert e.consume(T1, WEI, 10.0)[0].pnl_usd == pytest.approx(-10.0)

    def test_zero_pnl_at_the_same_price(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        assert e.consume(T1, WEI, 10.0)[0].pnl_usd == pytest.approx(0.0)

    def test_airdrop_sale_realizes_full_proceeds(self):
        """Zero cost basis is correct here — the tokens were free."""
        e = engine()
        e.add_lot(T0, WEI, 0.0, "airdrop")
        r = e.consume(T1, WEI, 15.0)[0]
        assert r.pnl_usd == pytest.approx(15.0)
        assert r.source == "airdrop"

    def test_pnl_scales_with_quantity(self):
        e = engine()
        e.add_lot(T0, WEI * 4, 10.0, "trading")
        assert e.consume(T1, WEI * 4, 12.0)[0].pnl_usd == pytest.approx(8.0)

    def test_partial_consumption_prices_only_what_was_taken(self):
        e = engine()
        e.add_lot(T0, WEI * 10, 10.0, "trading")
        assert e.consume(T1, WEI * 2, 12.0)[0].pnl_usd == pytest.approx(4.0)

    def test_eighteen_decimal_scaling(self):
        """Readability case: 10^18 base units is one whole token."""
        e = engine()
        e.add_lot(T0, WEI, 1.0, "trading")
        assert e.consume(T1, WEI, 1.5)[0].pnl_usd == 0.5

    def test_decimal_path_gates_binary_float_error(self):
        """0.3 - 0.1 under binary float is 0.19999999999999998.

        This test gates that the engine runs the subtraction through Decimal
        before casting to float. A pure-float implementation fails the exact
        equality below; the 10^18 case above would pass either way, because
        1.5 - 1.0 happens to be exact in binary.
        """
        e = engine()
        e.add_lot(T0, WEI, 0.1, "trading")
        assert e.consume(T1, WEI, 0.3)[0].pnl_usd == 0.2

    def test_non_round_quantity_precision(self):
        """A quantity that is not a clean multiple of the divisor."""
        e = engine()
        qty = Decimal("333333333333333333")  # 0.333333333333333333 tokens
        e.add_lot(T0, qty, 1.0, "trading")
        pnl = e.consume(T1, qty, 2.0)[0].pnl_usd
        assert pnl == pytest.approx(0.333333333333333333, abs=1e-15)

    def test_small_decimals_token(self):
        """USDC-style 6 decimals: the divisor must come from the constructor."""
        e = engine(decimals=6)
        one_usdc = Decimal(10**6)
        e.add_lot(T0, one_usdc * 100, 1.0, "trading")
        assert e.consume(T1, one_usdc * 100, 1.1)[0].pnl_usd == pytest.approx(10.0)


class TestAvgCostBasis:
    def test_none_when_empty(self):
        assert engine().avg_cost_basis_usd() is None

    def test_none_after_full_consumption(self):
        """None rather than 0.0: a zero average is a different, real statement."""
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        e.consume(T1, WEI, 12.0)
        assert e.avg_cost_basis_usd() is None

    def test_single_lot_is_its_own_cost(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        assert e.avg_cost_basis_usd() == pytest.approx(10.0)

    def test_equal_quantities_average_evenly(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        e.add_lot(T1, WEI, 20.0, "trading")
        assert e.avg_cost_basis_usd() == pytest.approx(15.0)

    def test_weighted_by_quantity(self):
        """1 @ $10 + 3 @ $20 = $70 over 4 tokens = $17.50."""
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        e.add_lot(T1, WEI * 3, 20.0, "trading")
        assert e.avg_cost_basis_usd() == pytest.approx(17.5)

    def test_airdrop_lots_pull_the_average_down(self):
        """It describes the whole remaining position, not just its trading half."""
        e = engine()
        e.add_lot(T0, WEI, 20.0, "trading")
        e.add_lot(T1, WEI, 0.0, "airdrop")
        assert e.avg_cost_basis_usd() == pytest.approx(10.0)

    def test_reflects_the_stack_after_partial_consumption(self):
        """Selling the cheap lot leaves the expensive one as the whole basis."""
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        e.add_lot(T1, WEI, 20.0, "trading")
        e.consume(T2, WEI, 15.0)
        assert e.avg_cost_basis_usd() == pytest.approx(20.0)

    def test_all_airdrop_position_averages_zero_not_none(self):
        """0.0 and None mean different things; this is the 0.0 case."""
        e = engine()
        e.add_lot(T0, WEI, 0.0, "airdrop")
        assert e.avg_cost_basis_usd() == 0.0


class TestIsolation:
    def test_snapshot_is_a_copy(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        snapshot = e.lots_snapshot()
        snapshot.clear()
        assert len(e.lots_snapshot()) == 1
        assert e.balance_token() == WEI

    def test_snapshot_reordering_does_not_affect_the_engine(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        e.add_lot(T1, WEI, 20.0, "trading")
        snapshot = e.lots_snapshot()
        snapshot.reverse()
        assert e.consume(T2, WEI, 15.0)[0].unit_cost_usd == 10.0

    def test_lots_are_frozen(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        with pytest.raises(ValidationError):
            e.lots_snapshot()[0].qty_token = Decimal(1)

    def test_realizations_are_frozen(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        r = e.consume(T1, WEI, 12.0)[0]
        with pytest.raises(ValidationError):
            r.pnl_usd = 999.0

    def test_two_engines_do_not_share_state(self):
        """The deque must be per-instance, not a class attribute."""
        a, b = engine(), engine()
        a.add_lot(T0, WEI, 10.0, "trading")
        assert b.balance_token() == Decimal(0)
        assert b.lots_snapshot() == []

    def test_balance_by_source_is_a_fresh_dict(self):
        e = engine()
        e.add_lot(T0, WEI, 10.0, "trading")
        totals = e.balance_token_by_source()
        totals["trading"] = Decimal(999)
        assert e.balance_token_by_source()["trading"] == WEI
