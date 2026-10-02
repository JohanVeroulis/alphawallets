"""Tests for AW_03's --resume mode."""

import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from alphawallets.db import connect
from alphawallets.fetchers.prices.aw_03_defillama_historical_prices import (
    DEFAULT_SPAN_DAYS,
    _build_arg_parser,
    get_resume_point,
    main,
    span_days_since,
)
from alphawallets.fetchers.prices.writer import create_tables

UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"

NOW = datetime(2026, 10, 2, 9, 30, tzinfo=UTC)
H05 = datetime(2026, 10, 1, 5, 0, tzinfo=UTC)


@pytest.fixture
def conn():
    with connect(":memory:") as c:
        create_tables(c)
        yield c


def _add_price(conn, ts, token=UNI, chain="ethereum", source="defillama") -> None:
    conn.execute(
        "INSERT INTO token_price VALUES (?, ?, ?, ?, ?, ?, ?)",
        [chain, token, ts, 8.94, 0.99, source, NOW],
    )


class TestGetResumePoint:
    def test_returns_newest_stored_hour(self, conn):
        for hours in (0, 5, 3):
            _add_price(conn, H05 - timedelta(hours=hours))
        assert get_resume_point(conn, "ethereum", UNI) == H05

    def test_empty_table_returns_none(self, conn):
        assert get_resume_point(conn, "ethereum", UNI) is None

    def test_isolated_by_token(self, conn):
        _add_price(conn, H05, token=WETH)
        assert get_resume_point(conn, "ethereum", UNI) is None

    def test_isolated_by_chain(self, conn):
        _add_price(conn, H05, chain="base")
        assert get_resume_point(conn, "ethereum", UNI) is None

    def test_isolated_by_source(self, conn):
        """A CoinGecko backfill must not convince this fetcher it has covered a span.

        The four-column primary key keeps both sources' rows side by side, which
        is exactly why the source filter is needed here.
        """
        _add_price(conn, H05, source="coingecko")
        assert get_resume_point(conn, "ethereum", UNI) is None

    def test_picks_the_defillama_row_when_both_sources_present(self, conn):
        _add_price(conn, H05 + timedelta(hours=10), source="coingecko")
        _add_price(conn, H05, source="defillama")
        assert get_resume_point(conn, "ethereum", UNI) == H05

    def test_accepts_checksummed_token(self, conn):
        _add_price(conn, H05)
        assert get_resume_point(conn, "ethereum", UNI.upper()) == H05

    def test_result_is_utc(self, conn):
        _add_price(conn, H05)
        assert get_resume_point(conn, "ethereum", UNI).utcoffset().total_seconds() == 0


class TestSpanDaysSince:
    def test_rounds_up_a_partial_day(self):
        """28.5 hours of history needs 2 days, not 1 — a short span leaves a hole."""
        assert span_days_since(NOW - timedelta(hours=28, minutes=30), now_utc=NOW) == 2

    def test_exact_day_is_not_inflated(self):
        assert span_days_since(NOW - timedelta(days=3), now_utc=NOW) == 3

    def test_minimum_is_one_day(self):
        """A resume point minutes old still fetches a day: overlap is free."""
        assert span_days_since(NOW - timedelta(minutes=5), now_utc=NOW) == 1

    def test_resume_point_equal_to_now_is_one_day(self):
        assert span_days_since(NOW, now_utc=NOW) == 1

    def test_future_resume_point_is_one_day(self):
        """Clock skew must not produce a zero or negative span."""
        assert span_days_since(NOW + timedelta(hours=2), now_utc=NOW) == 1

    def test_non_utc_resume_point_is_converted(self):
        from zoneinfo import ZoneInfo

        kolkata = (NOW - timedelta(days=2)).astimezone(ZoneInfo("Asia/Kolkata"))
        assert span_days_since(kolkata, now_utc=NOW) == 2

    def test_defaults_to_wall_clock(self):
        """Omitting now_utc uses the real clock.

        47 hours rather than exactly 2 days: the function reads the clock a moment
        after the test does, so an exact multiple would round up to 3.
        """
        assert span_days_since(datetime.now(tz=UTC) - timedelta(hours=47)) == 2


def _run_main(argv: list[str]) -> None:
    with patch.object(sys, "argv", ["prog", *argv]):
        main()


class TestResumeMutex:
    def test_resume_with_span_days_errors(self, capsys):
        with pytest.raises(SystemExit):
            _run_main(["--chain", "ethereum", "--resume", "--span-days", "10"])
        err = capsys.readouterr().err
        assert "--resume" in err
        assert "--span-days" in err

    def test_resume_alone_parses(self):
        args = _build_arg_parser().parse_args(["--chain", "ethereum", "--resume"])
        assert args.resume is True
        assert args.span_days is None

    def test_resume_defaults_to_false(self):
        assert _build_arg_parser().parse_args(["--chain", "ethereum"]).resume is False


class TestResumeCliIntegration:
    @pytest.fixture
    def db_path(self, tmp_path):
        path = tmp_path / "cache.duckdb"
        with connect(path) as c:
            create_tables(c)
            _add_price(c, H05)
        return path

    def test_span_computed_from_stored_hour(self, db_path, capsys):
        with patch(
            "alphawallets.fetchers.prices.aw_03_defillama_historical_prices."
            "fetch_and_persist_prices"
        ) as mock_fetch:
            _run_main(
                ["--chain", "ethereum", "--token", UNI, "--resume", "--db-path", str(db_path)]
            )

        span = mock_fetch.call_args.kwargs["span_days"]
        # H05 is 2026-10-01 05:00Z; the gap to now is at least a day and grows,
        # so assert the relationship rather than a literal that ages.
        assert span == span_days_since(H05)
        assert span >= 1
        assert "resume from 2026-10-01 05:00Z" in capsys.readouterr().out

    def test_falls_back_to_default_span_when_empty(self, tmp_path, capsys):
        empty = tmp_path / "empty.duckdb"
        with patch(
            "alphawallets.fetchers.prices.aw_03_defillama_historical_prices."
            "fetch_and_persist_prices"
        ) as mock_fetch:
            _run_main(["--chain", "ethereum", "--token", UNI, "--resume", "--db-path", str(empty)])

        assert mock_fetch.call_args.kwargs["span_days"] == DEFAULT_SPAN_DAYS
        assert "no previous data" in capsys.readouterr().out

    def test_resume_ignores_another_tokens_progress(self, tmp_path):
        path = tmp_path / "other.duckdb"
        with connect(path) as c:
            create_tables(c)
            _add_price(c, H05, token=WETH)

        with patch(
            "alphawallets.fetchers.prices.aw_03_defillama_historical_prices."
            "fetch_and_persist_prices"
        ) as mock_fetch:
            _run_main(["--chain", "ethereum", "--token", UNI, "--resume", "--db-path", str(path)])

        assert mock_fetch.call_args.kwargs["span_days"] == DEFAULT_SPAN_DAYS

    def test_default_run_uses_the_default_span(self, db_path, capsys):
        with patch(
            "alphawallets.fetchers.prices.aw_03_defillama_historical_prices."
            "fetch_and_persist_prices"
        ) as mock_fetch:
            _run_main(["--chain", "ethereum", "--token", UNI, "--db-path", str(db_path)])

        assert mock_fetch.call_args.kwargs["span_days"] == DEFAULT_SPAN_DAYS
        assert "default span" in capsys.readouterr().out

    def test_explicit_span_is_named_in_the_mode_line(self, db_path, capsys):
        with patch(
            "alphawallets.fetchers.prices.aw_03_defillama_historical_prices."
            "fetch_and_persist_prices"
        ) as mock_fetch:
            _run_main(
                [
                    "--chain",
                    "ethereum",
                    "--token",
                    UNI,
                    "--span-days",
                    "7",
                    "--db-path",
                    str(db_path),
                ]
            )

        assert mock_fetch.call_args.kwargs["span_days"] == 7
        assert "explicit span (7 days)" in capsys.readouterr().out
