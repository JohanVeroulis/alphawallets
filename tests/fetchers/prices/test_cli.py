"""Tests for the aw_03 CLI arg parser and span validation."""

from pathlib import Path

import pytest

from alphawallets.fetchers.prices.aw_03_defillama_historical_prices import (
    DEFAULT_PERIOD,
    DEFAULT_SPAN_DAYS,
    DEFAULT_TOKENS,
    _build_arg_parser,
    _validate_range_args,
)

UNI = DEFAULT_TOKENS["UNI"]


class TestArgParser:
    def test_minimal_valid_args(self):
        args = _build_arg_parser().parse_args(["--chain", "ethereum"])
        assert args.chain == "ethereum"
        assert args.token == UNI
        assert args.span_days == DEFAULT_SPAN_DAYS
        assert args.period == DEFAULT_PERIOD

    def test_token_override(self):
        other = "0x" + "b" * 40
        args = _build_arg_parser().parse_args(["--chain", "base", "--token", other])
        assert args.token == other

    def test_span_and_period_override(self):
        args = _build_arg_parser().parse_args(
            ["--chain", "ethereum", "--span-days", "7", "--period", "1d"]
        )
        assert args.span_days == 7
        assert args.period == "1d"

    def test_missing_chain_rejected(self):
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args([])

    def test_invalid_chain_rejected(self):
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args(["--chain", "solana"])

    def test_non_integer_span_rejected(self):
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args(["--chain", "ethereum", "--span-days", "thirty"])

    def test_db_path_parsed_as_path(self):
        args = _build_arg_parser().parse_args(
            ["--chain", "ethereum", "--db-path", "/tmp/prices.duckdb"]
        )
        assert isinstance(args.db_path, Path)

    def test_verbose_flag(self):
        args = _build_arg_parser().parse_args(["--chain", "ethereum", "-v"])
        assert args.verbose is True


class TestValidateRangeArgs:
    def _parse(self, extra: list[str]):
        parser = _build_arg_parser()
        return parser.parse_args(["--chain", "ethereum", *extra]), parser

    def test_default_span_plans_two_chunks(self):
        """30 days hourly = 720 points, over the 500 cap, so two chunks."""
        args, parser = self._parse([])
        total, n_chunks, chunk_span = _validate_range_args(args, parser)
        assert total == DEFAULT_SPAN_DAYS * 24
        assert n_chunks == 2
        assert chunk_span == total // n_chunks

    def test_small_span_plans_one_chunk(self):
        args, parser = self._parse(["--span-days", "7"])
        total, n_chunks, chunk_span = _validate_range_args(args, parser)
        assert (total, n_chunks, chunk_span) == (7 * 24, 1, 7 * 24)

    def test_daily_period_needs_one_chunk(self):
        args, parser = self._parse(["--span-days", "30", "--period", "1d"])
        total, n_chunks, _ = _validate_range_args(args, parser)
        assert (total, n_chunks) == (30, 1)

    def test_zero_span_days_rejected(self):
        args, parser = self._parse(["--span-days", "0"])
        with pytest.raises(SystemExit):
            _validate_range_args(args, parser)

    def test_negative_span_days_rejected(self):
        args, parser = self._parse(["--span-days", "-5"])
        with pytest.raises(SystemExit):
            _validate_range_args(args, parser)

    def test_unsupported_period_rejected(self):
        args, parser = self._parse(["--period", "7m"])
        with pytest.raises(SystemExit):
            _validate_range_args(args, parser)

    def test_plan_matches_chunk_plan_helper(self):
        """The CLI summary must state the same plan the client will follow."""
        from alphawallets.fetchers.prices.defillama_client import chunk_plan

        args, parser = self._parse(["--span-days", "45"])
        assert _validate_range_args(args, parser) == chunk_plan(45, DEFAULT_PERIOD)
