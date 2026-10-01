"""Tests for the wallet activity proof: the Event model and price classification."""

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from alphawallets.db import connect
from alphawallets.fetchers.erc20.writer import create_tables as create_erc20_tables
from alphawallets.fetchers.prices.writer import create_tables as create_price_tables
from alphawallets.fetchers.uniswap_v3.writer import create_tables as create_swap_tables
from alphawallets.pipeline.exploration.models import (
    UNI_ETHEREUM,
    Event,
    classify_price_status,
)
from alphawallets.pipeline.exploration.wallet_activity_proof import (
    TimelineSummary,
    _build_arg_parser,
    build_wallet_timeline,
    format_event_line,
    format_timeline,
    main,
    summarise,
)

from .conftest import GRID_HEAD as FIXTURE_HEAD
from .conftest import H05 as FIXTURE_H05
from .conftest import H07 as FIXTURE_H07
from .conftest import UNI, WALLET

# A fixed "now" so nothing in these tests depends on when they run. 06:38Z sits
# inside hour 06:00, mirroring the first live run where the newest price row was
# 05:00Z and seven swaps in hour 06 were pending.
# The price grid's newest hour, and an hour comfortably inside the covered range.
HEAD = datetime(2026, 10, 1, 6, 0, 0, tzinfo=UTC)
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
    """The three-state split, keyed off the price grid's head.

    Keyed off MAX(token_price.ts) rather than the wall clock: the first live run
    showed AW_03 publishing 05:00Z at 06:30Z, so the provider runs about two hours
    behind and a wall-clock rule reported that lag as a backfill hole.
    """

    def test_price_row_present_is_priced(self):
        assert (
            classify_price_status(PAST_HOUR, has_price_row=True, price_grid_head=HEAD) == "priced"
        )

    def test_hour_above_the_head_is_pending(self):
        above = HEAD + timedelta(hours=1)
        assert classify_price_status(above, has_price_row=False, price_grid_head=HEAD) == "pending"

    def test_hour_below_the_head_is_unavailable(self):
        """A hole inside the range we believe we cover — worth investigating."""
        below = HEAD - timedelta(hours=2)
        assert (
            classify_price_status(below, has_price_row=False, price_grid_head=HEAD) == "unavailable"
        )

    def test_hour_equal_to_the_head_without_a_row_is_unavailable(self):
        """The boundary is inclusive: the head hour is inside the covered range."""
        assert (
            classify_price_status(HEAD, has_price_row=False, price_grid_head=HEAD) == "unavailable"
        )

    def test_no_prices_at_all_is_pending_not_unavailable(self):
        """Nothing can be a hole when there is no covered range to hole."""
        assert (
            classify_price_status(PAST_HOUR, has_price_row=False, price_grid_head=None) == "pending"
        )

    def test_no_prices_at_all_never_raises(self):
        """An unpriced token is an actionable state, not an error."""
        for hour in (PAST_HOUR, HEAD, HEAD + timedelta(hours=5)):
            assert classify_price_status(hour, has_price_row=False, price_grid_head=None) == (
                "pending"
            )

    def test_priced_wins_even_above_the_head(self):
        """has_price_row is authoritative; the head is only for the unpriced case."""
        above = HEAD + timedelta(hours=1)
        assert classify_price_status(above, has_price_row=True, price_grid_head=HEAD) == "priced"

    def test_non_utc_head_is_converted(self):
        """A +05:30 head must not shift the boundary."""
        kolkata_head = HEAD.astimezone(ZoneInfo("Asia/Kolkata"))
        below = HEAD - timedelta(hours=1)
        assert (
            classify_price_status(below, has_price_row=False, price_grid_head=kolkata_head)
            == "unavailable"
        )

    def test_non_utc_event_hour_is_converted(self):
        kolkata_hour = (HEAD + timedelta(hours=1)).astimezone(ZoneInfo("Asia/Kolkata"))
        assert (
            classify_price_status(kolkata_hour, has_price_row=False, price_grid_head=HEAD)
            == "pending"
        )


class TestConstants:
    def test_uni_address_is_lowercase(self):
        """The cache stores lowercase; a checksummed constant would match nothing."""
        assert UNI_ETHEREUM.lower() == UNI_ETHEREUM
        assert len(UNI_ETHEREUM) == 42


class TestTimelineSummary:
    def test_construction_and_derived_properties(self):
        summary = TimelineSummary(
            total=10, swaps=4, transfers=6, priced=7, pending=3, unavailable=0
        )
        assert summary.covered_range_events == 7
        assert summary.coverage_pct == 100.0
        assert summary.is_fully_priced is True

    def test_frozen(self):
        summary = TimelineSummary(total=1, swaps=1, transfers=0, priced=1, pending=0, unavailable=0)
        with pytest.raises(ValidationError):
            summary.priced = 99

    def test_pending_excluded_from_the_denominator(self):
        """The whole point: provider lag must not read as falling coverage."""
        summary = TimelineSummary(
            total=50, swaps=50, transfers=0, priced=43, pending=7, unavailable=0
        )
        assert summary.covered_range_events == 43
        assert summary.coverage_pct == 100.0
        assert summary.is_fully_priced is True

    def test_unavailable_breaks_full_pricing(self):
        """A single hole inside the covered range is a real signal."""
        summary = TimelineSummary(
            total=50, swaps=50, transfers=0, priced=42, pending=7, unavailable=1
        )
        assert summary.covered_range_events == 43
        assert summary.coverage_pct == pytest.approx(97.67, abs=0.01)
        assert summary.is_fully_priced is False

    def test_all_pending_is_not_a_division_by_zero(self):
        summary = TimelineSummary(total=3, swaps=0, transfers=3, priced=0, pending=3, unavailable=0)
        assert summary.covered_range_events == 0
        assert summary.coverage_pct == 100.0

    def test_negative_counters_rejected(self):
        with pytest.raises(ValidationError):
            TimelineSummary(total=1, swaps=1, transfers=0, priced=-1, pending=0, unavailable=0)

    def test_hour_lists_default_to_empty(self):
        summary = TimelineSummary(total=0, swaps=0, transfers=0, priced=0, pending=0, unavailable=0)
        assert summary.unavailable_hours == []
        assert summary.pending_hours == []
        assert summary.price_grid_head is None


class TestSummarise:
    def test_counts_by_type_and_status(self, cache):
        events, summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI)
        assert summary.total == len(events) == 6
        assert (summary.swaps, summary.transfers) == (2, 4)
        assert (summary.priced, summary.pending, summary.unavailable) == (3, 1, 2)

    def test_grid_head_recorded(self, cache):
        """The fixture prices hours 03, 04 and 06, so the head is 06:00Z."""
        _events, summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI)
        assert summary.price_grid_head == FIXTURE_HEAD

    def test_pending_hours_are_above_the_head(self, cache):
        _events, summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI)
        assert summary.pending_hours == [FIXTURE_H07]
        assert all(h > summary.price_grid_head for h in summary.pending_hours)

    def test_unavailable_hours_are_inside_the_covered_range(self, cache):
        """Hour 05 is a hole between priced hours 04 and 06 — a real gap."""
        _events, summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI)
        assert summary.unavailable_hours == [FIXTURE_H05]
        assert all(h <= summary.price_grid_head for h in summary.unavailable_hours)

    def test_grid_head_advance_prices_the_pending_event(self, cache):
        """Simulate an AW_03 re-run: the pending hour gains a price row.

        Stronger than advancing a clock — this is the actual event that resolves
        a pending event, and the classification follows the data rather than time.
        """
        _events, before = build_wallet_timeline(cache, WALLET, "ethereum", UNI)
        assert (before.priced, before.pending) == (3, 1)

        cache.execute(
            "INSERT INTO token_price VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["ethereum", UNI, FIXTURE_H07, 8.96, 0.99, "defillama", FIXTURE_H07],
        )

        _events, after = build_wallet_timeline(cache, WALLET, "ethereum", UNI)
        assert after.price_grid_head == FIXTURE_H07
        assert (after.priced, after.pending) == (4, 0)
        assert after.unavailable == 2  # the hour-05 hole is untouched

    def test_filling_the_hole_reaches_full_pricing(self, cache):
        """The other direction: patching hour 05 clears the unavailable bucket."""
        cache.execute(
            "INSERT INTO token_price VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["ethereum", UNI, FIXTURE_H05, 8.93, 0.99, "defillama", FIXTURE_H05],
        )
        _events, summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI)
        assert summary.unavailable == 0
        assert summary.is_fully_priced is True
        assert summary.coverage_pct == 100.0

    def test_no_prices_at_all_makes_everything_pending(self, cache):
        """Empty price table: pending, never unavailable, and never an exception."""
        cache.execute("DELETE FROM token_price")
        events, summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI)
        assert summary.price_grid_head is None
        assert summary.pending == len(events) == 6
        assert summary.unavailable == 0
        assert summary.coverage_pct == 100.0

    def test_empty_timeline_summarises_cleanly(self):
        summary = summarise([])
        assert summary.total == 0
        assert summary.coverage_pct == 100.0
        assert summary.is_fully_priced is True


class TestBuildWalletTimeline:
    def test_returns_events_and_summary(self, cache):
        events, summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI)
        assert isinstance(summary, TimelineSummary)
        assert all(isinstance(e, Event) for e in events)

    def test_chronological(self, cache):
        events, _summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI)
        assert [e.ts for e in events] == sorted(e.ts for e in events)

    def test_uppercase_wallet_matches(self, cache):
        lower, _ = build_wallet_timeline(cache, WALLET, "ethereum", UNI)
        upper, _ = build_wallet_timeline(cache, WALLET.upper(), "ethereum", UNI)
        assert [e.tx_hash for e in upper] == [e.tx_hash for e in lower]

    def test_base_chain_sees_only_base_rows(self, cache):
        """The fixture's one Base row — proof the chain filter reaches this layer."""
        events, summary = build_wallet_timeline(cache, WALLET, "base", UNI)
        assert summary.total == 1
        assert events[0].event_type == "transfer"

    def test_unknown_wallet_is_empty_not_an_error(self, cache):
        events, summary = build_wallet_timeline(cache, "0x" + "c" * 40, "ethereum", UNI)
        assert events == []
        assert summary.total == 0

    def test_defaults_to_ethereum_and_uni(self, cache):
        events, _summary = build_wallet_timeline(cache, WALLET)
        assert len(events) == 6


class TestFormatting:
    @pytest.fixture
    def rendered(self, cache):
        events, summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI)
        return format_timeline(events, summary, WALLET, "ethereum", UNI)

    def test_header_shows_shortened_wallet_and_token(self, rendered):
        assert "Wallet: 0x1111...1111" in rendered
        assert "Token: UNI (0x1f98...f984)" in rendered

    def test_event_counts_line(self, rendered):
        assert "Events: 6 (2 swaps, 4 transfers)" in rendered

    def test_coverage_line_names_covered_range_denominator(self, rendered):
        assert "Priced: 3/5 covered-range events (60%)" in rendered

    def test_coverage_line_lists_pending_hours(self, rendered):
        assert "Pending: 1 [2026-10-01 07:00Z]" in rendered

    def test_grid_head_is_shown(self, rendered):
        """The reference point for the whole classification must be visible."""
        assert "Price grid head: 2026-10-01 06:00Z" in rendered

    def test_coverage_line_lists_unavailable_hours(self, rendered):
        assert "Unavailable: 2 [2026-10-01 05:00Z]" in rendered

    def test_pending_note_explains_why(self, rendered):
        """The proof must read honestly without this chat for context."""
        assert "above the price grid head" in rendered
        assert "provider's publishing lag" in rendered
        assert "two hours behind" in rendered

    def test_unavailable_warning_names_the_remedy(self, rendered):
        assert "real hole inside the range we cover" in rendered
        assert "Re-run AW_03" in rendered

    def test_unpriced_token_note_is_actionable(self, cache):
        """No prices at all gets its own message, not the lag explanation."""
        cache.execute("DELETE FROM token_price")
        events, summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI)
        rendered = format_timeline(events, summary, WALLET, "ethereum", UNI)
        assert "no prices exist for this chain and token" in rendered
        assert "Run AW_03 for this token" in rendered
        assert "none — this token has no prices in the cache" in rendered

    def test_priced_row_shows_price_and_value(self, rendered):
        assert "@ $8.87" in rendered
        assert "= $    4,435.00" in rendered

    def test_pending_row_shows_pending_not_blank(self, rendered):
        assert "@ pending" in rendered

    def test_unavailable_row_shows_unavailable_not_blank(self, rendered):
        assert "@ unavailable" in rendered

    def test_unpriced_rows_show_a_dash_for_value(self, rendered):
        assert "--" in rendered

    def test_swap_rows_show_the_paired_leg(self, rendered):
        assert "(-> 1.50 WETH)" in rendered

    def test_every_event_has_a_line(self, cache, rendered):
        events, _summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI)
        for event in events:
            assert event.ts.strftime("%H:%MZ") in rendered

    def test_approximate_amount_marked(self):
        """decimals_assumed must be visible on the row, not only in the dict."""
        event = Event(
            ts=FIXTURE_H05,
            event_type="transfer",
            direction="in",
            amount_token=Decimal("7"),
            price_status="pending",
            tx_hash="0x" + "a" * 64,
            amount_approximate=True,
        )
        assert "(~)" in format_event_line(event, "UNI")

    def test_exact_amount_not_marked(self):
        event = Event(
            ts=FIXTURE_H05,
            event_type="transfer",
            direction="in",
            amount_token=Decimal("7"),
            price_status="pending",
            tx_hash="0x" + "a" * 64,
        )
        assert "(~)" not in format_event_line(event, "UNI")

    def test_empty_timeline_says_so(self):
        summary = summarise([])
        rendered = format_timeline([], summary, WALLET, "ethereum", UNI)
        assert "(no events)" in rendered

    def test_no_pending_note_when_nothing_pending(self, cache):
        """Price the hour above the head, and the lag note disappears."""
        cache.execute(
            "INSERT INTO token_price VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["ethereum", UNI, FIXTURE_H07, 8.96, 0.99, "defillama", FIXTURE_H07],
        )
        events, summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI)
        rendered = format_timeline(events, summary, WALLET, "ethereum", UNI)
        assert "Pending: 0" in rendered
        assert "publishing lag" not in rendered


class TestArgParser:
    def test_defaults(self):
        args = _build_arg_parser().parse_args([])
        assert args.wallet is None
        assert args.chain == "ethereum"
        assert args.token == UNI_ETHEREUM
        assert args.db_path is None
        assert args.verbose is False

    def test_all_options(self):
        args = _build_arg_parser().parse_args(
            [
                "--wallet",
                WALLET,
                "--chain",
                "base",
                "--token",
                UNI,
                "--db-path",
                "/tmp/x.duckdb",
                "-v",
            ]
        )
        assert args.wallet == WALLET
        assert args.chain == "base"
        assert args.db_path == Path("/tmp/x.duckdb")
        assert args.verbose is True

    def test_unknown_chain_rejected(self):
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args(["--chain", "solana"])

    def test_wallet_is_optional_for_auto_pick(self):
        """Omitting --wallet is the auto-pick path, not an error."""
        assert _build_arg_parser().parse_args([]).wallet is None


class TestCliMain:
    """The CLI against a real file-backed cache, through main()."""

    @pytest.fixture
    def db_path(self, tmp_path, cache):
        """Copy the fixture cache into a file main() can open by path."""
        path = tmp_path / "cache.duckdb"
        cache.execute(f"ATTACH '{path}' AS out")
        for table in ("uniswap_v3_swap", "erc20_transfer", "token_price"):
            cache.execute(f"CREATE TABLE out.{table} AS SELECT * FROM {table}")
        cache.execute("DETACH out")
        return path

    def test_explicit_wallet_prints_timeline(self, db_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["prog", "--wallet", WALLET, "--db-path", str(db_path)])
        assert main() == 0
        out = capsys.readouterr().out
        assert "AlphaWallets — Wallet Activity Proof" in out
        assert "Events: 6" in out

    def test_auto_pick_runs_without_a_wallet(self, db_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["prog", "--db-path", str(db_path)])
        assert main() == 0
        assert "Wallet:" in capsys.readouterr().out

    def test_empty_cache_exits_one_with_a_named_message(self, tmp_path, monkeypatch, capsys):
        """No stack trace: name the chain and token that had nothing."""
        empty = tmp_path / "empty.duckdb"
        with connect(empty) as conn:
            create_swap_tables(conn)
            create_erc20_tables(conn)
            create_price_tables(conn)

        monkeypatch.setattr(sys, "argv", ["prog", "--db-path", str(empty)])
        assert main() == 1
        out = capsys.readouterr().out
        assert "No activity in the cache" in out
        assert "chain=ethereum" in out
        assert UNI_ETHEREUM in out
        assert "AW_01" in out  # tells the operator what to do next

    def test_base_chain_with_no_base_activity_reports_cleanly(self, tmp_path, monkeypatch, capsys):
        """--chain base against Ethereum-only data: a message, not empty output."""
        empty = tmp_path / "eth_only.duckdb"
        with connect(empty) as conn:
            create_swap_tables(conn)
            create_erc20_tables(conn)
            create_price_tables(conn)

        monkeypatch.setattr(sys, "argv", ["prog", "--chain", "base", "--db-path", str(empty)])
        assert main() == 1
        assert "chain=base" in capsys.readouterr().out

    def test_wallet_with_no_events_exits_one(self, db_path, monkeypatch, capsys):
        monkeypatch.setattr(
            sys,
            "argv",
            ["prog", "--wallet", "0x" + "c" * 40, "--db-path", str(db_path)],
        )
        assert main() == 1
        assert "No events for wallet" in capsys.readouterr().out
