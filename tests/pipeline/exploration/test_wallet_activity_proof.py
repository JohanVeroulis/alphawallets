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

from .conftest import H05 as FIXTURE_H05
from .conftest import H06 as FIXTURE_H06
from .conftest import NOW as FIXTURE_NOW
from .conftest import UNI, WALLET

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


class TestTimelineSummary:
    def test_construction_and_derived_properties(self):
        summary = TimelineSummary(
            total=10, swaps=4, transfers=6, priced=7, pending=3, unavailable=0
        )
        assert summary.complete_hour_events == 7
        assert summary.coverage_pct == 100.0
        assert summary.is_fully_priced is True

    def test_frozen(self):
        summary = TimelineSummary(total=1, swaps=1, transfers=0, priced=1, pending=0, unavailable=0)
        with pytest.raises(ValidationError):
            summary.priced = 99

    def test_pending_excluded_from_the_denominator(self):
        """The whole point: a structural lag must not read as falling coverage."""
        summary = TimelineSummary(
            total=50, swaps=50, transfers=0, priced=43, pending=7, unavailable=0
        )
        assert summary.complete_hour_events == 43
        assert summary.coverage_pct == 100.0
        assert summary.is_fully_priced is True

    def test_unavailable_breaks_full_pricing(self):
        """A single past-hour gap is a real signal and must not be swallowed."""
        summary = TimelineSummary(
            total=50, swaps=50, transfers=0, priced=42, pending=7, unavailable=1
        )
        assert summary.complete_hour_events == 43
        assert summary.coverage_pct == pytest.approx(97.67, abs=0.01)
        assert summary.is_fully_priced is False

    def test_all_pending_is_not_a_division_by_zero(self):
        summary = TimelineSummary(total=3, swaps=0, transfers=3, priced=0, pending=3, unavailable=0)
        assert summary.complete_hour_events == 0
        assert summary.coverage_pct == 100.0

    def test_negative_counters_rejected(self):
        with pytest.raises(ValidationError):
            TimelineSummary(total=1, swaps=1, transfers=0, priced=-1, pending=0, unavailable=0)

    def test_unavailable_hours_defaults_to_empty_list(self):
        summary = TimelineSummary(total=0, swaps=0, transfers=0, priced=0, pending=0, unavailable=0)
        assert summary.unavailable_hours == []


class TestSummarise:
    def test_counts_by_type_and_status(self, cache):
        events, summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI, now_utc=FIXTURE_NOW)
        assert summary.total == len(events) == 5
        assert (summary.swaps, summary.transfers) == (2, 3)
        assert (summary.priced, summary.pending, summary.unavailable) == (2, 1, 2)

    def test_pending_hour_is_the_current_hour(self, cache):
        _events, summary = build_wallet_timeline(
            cache, WALLET, "ethereum", UNI, now_utc=FIXTURE_NOW
        )
        assert summary.pending_hour == FIXTURE_H06

    def test_unavailable_hours_lists_the_gap(self, cache):
        """Both hour-05 events share one hour, so the list is deduplicated."""
        _events, summary = build_wallet_timeline(
            cache, WALLET, "ethereum", UNI, now_utc=FIXTURE_NOW
        )
        assert summary.unavailable_hours == [FIXTURE_H05]

    def test_pending_hour_is_none_when_nothing_pending(self, cache):
        """Advance the clock past every event: nothing is in the current hour."""
        later = FIXTURE_NOW + timedelta(hours=5)
        _events, summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI, now_utc=later)
        assert summary.pending == 0
        assert summary.pending_hour is None

    def test_pending_becomes_unavailable_once_its_hour_closes(self, cache):
        """Same data, later clock: the hour-06 event is now a real gap."""
        later = FIXTURE_NOW + timedelta(hours=5)
        _events, summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI, now_utc=later)
        assert summary.unavailable == 3
        assert FIXTURE_H06 in summary.unavailable_hours

    def test_empty_timeline_summarises_cleanly(self):
        summary = summarise([])
        assert summary.total == 0
        assert summary.coverage_pct == 100.0
        assert summary.is_fully_priced is True


class TestBuildWalletTimeline:
    def test_returns_events_and_summary(self, cache):
        events, summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI, now_utc=FIXTURE_NOW)
        assert isinstance(summary, TimelineSummary)
        assert all(isinstance(e, Event) for e in events)

    def test_chronological(self, cache):
        events, _summary = build_wallet_timeline(
            cache, WALLET, "ethereum", UNI, now_utc=FIXTURE_NOW
        )
        assert [e.ts for e in events] == sorted(e.ts for e in events)

    def test_uppercase_wallet_matches(self, cache):
        lower, _ = build_wallet_timeline(cache, WALLET, "ethereum", UNI, now_utc=FIXTURE_NOW)
        upper, _ = build_wallet_timeline(
            cache, WALLET.upper(), "ethereum", UNI, now_utc=FIXTURE_NOW
        )
        assert [e.tx_hash for e in upper] == [e.tx_hash for e in lower]

    def test_base_chain_sees_only_base_rows(self, cache):
        """The fixture's one Base row — proof the chain filter reaches this layer."""
        events, summary = build_wallet_timeline(cache, WALLET, "base", UNI, now_utc=FIXTURE_NOW)
        assert summary.total == 1
        assert events[0].event_type == "transfer"

    def test_unknown_wallet_is_empty_not_an_error(self, cache):
        events, summary = build_wallet_timeline(
            cache, "0x" + "c" * 40, "ethereum", UNI, now_utc=FIXTURE_NOW
        )
        assert events == []
        assert summary.total == 0

    def test_defaults_to_ethereum_and_uni(self, cache):
        events, _summary = build_wallet_timeline(cache, WALLET, now_utc=FIXTURE_NOW)
        assert len(events) == 5


class TestFormatting:
    @pytest.fixture
    def rendered(self, cache):
        events, summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI, now_utc=FIXTURE_NOW)
        return format_timeline(events, summary, WALLET, "ethereum", UNI)

    def test_header_shows_shortened_wallet_and_token(self, rendered):
        assert "Wallet: 0x1111...1111" in rendered
        assert "Token: UNI (0x1f98...f984)" in rendered

    def test_event_counts_line(self, rendered):
        assert "Events: 5 (2 swaps, 3 transfers)" in rendered

    def test_coverage_line_names_complete_hour_denominator(self, rendered):
        assert "Priced: 2/4 complete-hour events (50%)" in rendered

    def test_coverage_line_labels_the_pending_hour(self, rendered):
        assert "Pending: 1 (current hour 2026-10-01 06:00Z)" in rendered

    def test_coverage_line_lists_unavailable_hours(self, rendered):
        assert "Unavailable: 2 [2026-10-01 05:00Z]" in rendered

    def test_pending_note_explains_why(self, rendered):
        """The proof must read honestly without this chat for context."""
        assert "pending, not missing" in rendered
        assert "next prices backfill will price them" in rendered

    def test_unavailable_warning_names_the_remedy(self, rendered):
        assert "real gap" in rendered
        assert "Re-run AW_03" in rendered

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
        events, _summary = build_wallet_timeline(
            cache, WALLET, "ethereum", UNI, now_utc=FIXTURE_NOW
        )
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
        later = FIXTURE_NOW + timedelta(hours=5)
        events, summary = build_wallet_timeline(cache, WALLET, "ethereum", UNI, now_utc=later)
        rendered = format_timeline(events, summary, WALLET, "ethereum", UNI)
        assert "Pending: 0 (none)" in rendered
        assert "pending, not missing" not in rendered


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
        assert "Events: 5" in out

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
