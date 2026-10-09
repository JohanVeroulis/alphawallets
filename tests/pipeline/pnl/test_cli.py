"""Tests for the PnL CLI (ADR 0012 step 5)."""

import sys
from datetime import UTC, datetime, timedelta

import pytest

from alphawallets.db import connect
from alphawallets.fetchers.erc20.writer import create_tables as create_erc20_tables
from alphawallets.fetchers.prices.writer import create_tables as create_price_tables
from alphawallets.pipeline.pnl.__main__ import _build_arg_parser, _parse_as_of, main
from alphawallets.pipeline.pnl.writer import create_tables as create_pnl_tables

WALLET = "0x" + "1" * 40
OTHER = "0x" + "2" * 40
UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
UNI_BASE = "0xc3de830ea07524a0761646a6a4e4be0e114a3c83"

H0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
H1 = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
WEI = 10**18


def _seed(path):
    """A cache with one Ethereum and one Base acquisition, both priced."""
    with connect(path) as c:
        create_erc20_tables(c)
        create_price_tables(c)
        create_pnl_tables(c)
        for chain, token, price in (("ethereum", UNI, 10.0), ("base", UNI_BASE, 9.0)):
            c.execute(
                "INSERT INTO token_price VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [chain, token, H0, price, 0.99, "defillama", "chart", H0],
            )
        for i, (chain, token, wallet) in enumerate(
            (("ethereum", UNI, WALLET), ("base", UNI_BASE, OTHER))
        ):
            c.execute(
                "INSERT INTO erc20_transfer VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    chain,
                    i + 1,
                    H0,
                    "0x" + "a" * 64,
                    i,
                    f"t{i}",
                    token,
                    "0x" + "9" * 40,
                    wallet,
                    str(WEI),
                    18,
                ],
            )
    return path


@pytest.fixture
def cache_path(tmp_path):
    return _seed(tmp_path / "cache.duckdb")


def _run(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["prog", *argv])
    return main()


def _rows(path) -> int:
    with connect(path) as c:
        return c.execute("SELECT COUNT(*) FROM wallet_pnl").fetchone()[0]


class TestArgParsing:
    def test_defaults(self):
        args = _build_arg_parser().parse_args([])
        assert args.as_of is None
        assert args.chains is None
        assert args.wallets is None
        assert args.cache_db is None
        assert args.dry_run is False

    def test_all_options(self):
        args = _build_arg_parser().parse_args(
            [
                "--as-of",
                "2026-10-06T23:59:59Z",
                "--chains",
                "ethereum",
                "base",
                "--wallet",
                WALLET,
                "--wallet",
                OTHER,
                "--cache-db",
                "/tmp/x.duckdb",
                "--dry-run",
                "-v",
            ]
        )
        assert args.as_of == datetime(2026, 10, 6, 23, 59, 59, tzinfo=UTC)
        assert args.chains == ["ethereum", "base"]
        assert args.wallets == [WALLET, OTHER]
        assert args.dry_run is True
        assert args.verbose is True

    def test_wallet_is_repeatable(self):
        args = _build_arg_parser().parse_args(["--wallet", WALLET, "--wallet", OTHER])
        assert args.wallets == [WALLET, OTHER]

    def test_unknown_chain_rejected(self):
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args(["--chains", "solana"])


class TestAsOfParsing:
    def test_trailing_z_is_utc(self):
        assert _parse_as_of("2026-10-06T23:59:59Z") == datetime(2026, 10, 6, 23, 59, 59, tzinfo=UTC)

    def test_explicit_offset_is_converted_to_utc(self):
        assert _parse_as_of("2026-10-07T05:29:59+05:30") == datetime(
            2026, 10, 6, 23, 59, 59, tzinfo=UTC
        )

    def test_naive_timestamp_rejected(self):
        """Assuming UTC would let a local-time argument shift both window edges
        without anyone noticing."""
        import argparse

        with pytest.raises(argparse.ArgumentTypeError, match="must carry a timezone"):
            _parse_as_of("2026-10-06T23:59:59")

    def test_garbage_rejected_with_the_input_quoted(self):
        import argparse

        with pytest.raises(argparse.ArgumentTypeError, match="not-a-date"):
            _parse_as_of("not-a-date")

    def test_whitespace_tolerated(self):
        assert _parse_as_of("  2026-10-06T23:59:59Z  ").tzinfo is not None


class TestCliRun:
    def test_writes_rows_and_exits_zero(self, cache_path, monkeypatch, capsys):
        assert _run(monkeypatch, "--cache-db", str(cache_path)) == 0
        assert _rows(cache_path) > 0
        assert "AlphaWallets — PnL Calculator" in capsys.readouterr().out

    def test_dry_run_writes_nothing(self, cache_path, monkeypatch, capsys):
        assert _run(monkeypatch, "--cache-db", str(cache_path), "--dry-run") == 0
        assert _rows(cache_path) == 0
        out = capsys.readouterr().out
        assert "Dry run — nothing written" in out
        assert "computed, not written" in out

    def test_chain_filter(self, cache_path, monkeypatch):
        _run(monkeypatch, "--cache-db", str(cache_path), "--chains", "ethereum")
        with connect(cache_path) as c:
            chains = {r[0] for r in c.execute("SELECT DISTINCT chain FROM wallet_pnl").fetchall()}
        assert chains == {"ethereum"}

    def test_wallet_filter(self, cache_path, monkeypatch):
        _run(monkeypatch, "--cache-db", str(cache_path), "--wallet", WALLET)
        with connect(cache_path) as c:
            wallets = {r[0] for r in c.execute("SELECT DISTINCT wallet FROM wallet_pnl").fetchall()}
        assert wallets == {WALLET}

    def test_explicit_as_of_is_used_as_the_window_end(self, cache_path, monkeypatch):
        as_of = (H1 + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        _run(monkeypatch, "--cache-db", str(cache_path), "--as-of", as_of)
        with connect(cache_path) as c:
            ends = {
                r[0] for r in c.execute("SELECT DISTINCT window_end FROM wallet_pnl").fetchall()
            }
        assert ends == {H1 + timedelta(days=1)}

    def test_rerun_is_idempotent_in_row_count(self, cache_path, monkeypatch):
        """INSERT OR REPLACE: a second run updates rather than duplicating."""
        _run(monkeypatch, "--cache-db", str(cache_path))
        first = _rows(cache_path)
        _run(monkeypatch, "--cache-db", str(cache_path))
        assert _rows(cache_path) == first

    def test_summary_reports_flag_distribution(self, cache_path, monkeypatch, capsys):
        """A flag nobody looks at is a flag that does not work."""
        _run(monkeypatch, "--cache-db", str(cache_path))
        out = capsys.readouterr().out
        assert "Caveat flags" in out
        assert "has_pre_window_activity" in out
        assert "has_unpriceable_events" in out
        assert "has_smart_wallet_signal" in out

    def test_summary_reports_top_wallets(self, cache_path, monkeypatch, capsys):
        _run(monkeypatch, "--cache-db", str(cache_path))
        assert "Top" in capsys.readouterr().out

    def test_creates_the_table_on_a_cache_without_it(self, tmp_path, monkeypatch):
        path = tmp_path / "fresh.duckdb"
        with connect(path) as c:
            create_erc20_tables(c)
            create_price_tables(c)
        assert _run(monkeypatch, "--cache-db", str(path)) == 0


class TestCliErrors:
    def test_missing_cache_file_is_a_clear_error(self, tmp_path, monkeypatch, capsys):
        """DuckDB would otherwise create an empty file and report "no transfers",
        which reads as a data problem rather than a wrong path."""
        missing = tmp_path / "nope.duckdb"
        assert _run(monkeypatch, "--cache-db", str(missing)) == 1
        err = capsys.readouterr().err
        assert "Cache not found" in err
        assert str(missing) in err

    def test_missing_cache_file_is_not_created(self, tmp_path, monkeypatch):
        missing = tmp_path / "nope.duckdb"
        _run(monkeypatch, "--cache-db", str(missing))
        assert not missing.exists()

    def test_empty_cache_exits_zero_with_an_explanation(self, tmp_path, monkeypatch, capsys):
        """Nothing to compute is not a failure."""
        path = tmp_path / "empty.duckdb"
        with connect(path) as c:
            create_erc20_tables(c)
            create_price_tables(c)
        assert _run(monkeypatch, "--cache-db", str(path)) == 0
        out = capsys.readouterr().out
        assert "No PnL rows produced" in out
        assert "Backfill first" in out

    def test_schema_drift_exits_one_with_the_message(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "drifted.duckdb"
        with connect(path) as c:
            create_erc20_tables(c)
            create_price_tables(c)
            c.execute("CREATE TABLE wallet_pnl (chain VARCHAR, wallet VARCHAR)")
        assert _run(monkeypatch, "--cache-db", str(path)) == 1
        err = capsys.readouterr().err
        assert "SchemaDriftError" in err
        assert "does not migrate" in err


class TestRankingExclusions:
    """The top-N print is the only leaderboard the project has, so the two
    exclusions it cites must actually be applied to it (ADR 0012 decision 5,
    ADR 0016). A flagged row sitting at the top of this list is what prompted
    ADR 0016 in the first place."""

    def _summary(self, capsys, rows):
        from alphawallets.pipeline.pnl.__main__ import _print_summary

        _print_summary(rows, written=len(rows), dry_run=False)
        return capsys.readouterr().out

    def _row(self, **over):
        from alphawallets.pipeline.pnl.models import WalletPnL

        base = dict(
            chain="ethereum",
            wallet=WALLET,
            token_address=UNI,
            window_start=H0 - timedelta(days=30),
            window_end=H0,
            realized_pnl_usd=100.0,
            realized_pnl_trading_usd=100.0,
            realized_pnl_airdrop_usd=0.0,
            unrealized_pnl_usd=None,
            bought_usd=500.0,
            sold_usd=600.0,
            realization_count=3,
            balance_token=0,
            avg_cost_basis_usd=None,
            has_pre_window_activity=False,
            has_unpriceable_events=False,
            has_smart_wallet_signal=False,
            computed_at=H0,
        )
        base.update(over)
        return WalletPnL(**base)

    def test_contract_mediated_row_is_not_ranked(self, capsys):
        flagged = self._row(
            wallet=OTHER, realized_pnl_trading_usd=9999.0, has_smart_wallet_signal=True
        )
        out = self._summary(capsys, [flagged, self._row()])
        assert "9,999.00" not in out
        assert "100.00" in out
        assert "1 of 2 30d rows excluded" in out

    def test_pre_window_row_is_not_ranked(self, capsys):
        flagged = self._row(
            wallet=OTHER, realized_pnl_trading_usd=9999.0, has_pre_window_activity=True
        )
        out = self._summary(capsys, [flagged, self._row()])
        assert "9,999.00" not in out

    def test_all_rows_excluded_says_so_rather_than_printing_nothing(self, capsys):
        out = self._summary(capsys, [self._row(has_smart_wallet_signal=True)])
        assert "No eligible rows to rank" in out
