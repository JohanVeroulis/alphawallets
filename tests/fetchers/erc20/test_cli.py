"""Tests for the aw_02 CLI arg parser and range validation."""

import pytest

from alphawallets.fetchers.erc20.aw_02_erc20_transfers import (
    DEFAULT_TOKENS,
    _build_arg_parser,
    _validate_range_args,
)

UNI = DEFAULT_TOKENS["UNI"]


class TestArgParser:
    def test_minimal_valid_args(self):
        parser = _build_arg_parser()
        args = parser.parse_args(["--chain", "ethereum"])
        assert args.chain == "ethereum"
        assert args.blocks is None
        assert args.from_block is None
        assert args.to_block is None

    def test_contract_defaults_to_uni(self):
        parser = _build_arg_parser()
        args = parser.parse_args(["--chain", "ethereum"])
        assert args.contract == UNI

    def test_contract_override(self):
        parser = _build_arg_parser()
        other = "0x" + "b" * 40
        args = parser.parse_args(["--chain", "base", "--contract", other])
        assert args.contract == other

    def test_missing_chain_errors(self):
        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_invalid_chain_errors(self):
        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--chain", "solana"])

    def test_verbose_flag(self):
        parser = _build_arg_parser()
        args = parser.parse_args(["--chain", "ethereum", "-v"])
        assert args.verbose is True

    def test_db_path_parsed_as_path(self):
        from pathlib import Path

        parser = _build_arg_parser()
        args = parser.parse_args(["--chain", "ethereum", "--db-path", "/tmp/x.duckdb"])
        assert isinstance(args.db_path, Path)


class TestValidateRangeArgs:
    def _parse(self, extra: list[str]):
        parser = _build_arg_parser()
        args = parser.parse_args(["--chain", "ethereum", *extra])
        return args, parser

    def test_no_args_defaults_to_1000(self):
        args, parser = self._parse([])
        assert _validate_range_args(args, parser) == 1000

    def test_blocks_only(self):
        args, parser = self._parse(["--blocks", "500"])
        assert _validate_range_args(args, parser) == 500

    def test_explicit_range_returns_zero(self):
        args, parser = self._parse(["--from-block", "1000", "--to-block", "2000"])
        assert _validate_range_args(args, parser) == 0

    def test_from_without_to_errors(self):
        args, parser = self._parse(["--from-block", "1000"])
        with pytest.raises(SystemExit):
            _validate_range_args(args, parser)

    def test_to_without_from_errors(self):
        args, parser = self._parse(["--to-block", "2000"])
        with pytest.raises(SystemExit):
            _validate_range_args(args, parser)

    def test_blocks_with_explicit_range_errors(self):
        args, parser = self._parse(["--blocks", "500", "--from-block", "1", "--to-block", "2"])
        with pytest.raises(SystemExit):
            _validate_range_args(args, parser)

    def test_blocks_with_half_range_errors(self):
        args, parser = self._parse(["--blocks", "500", "--from-block", "1"])
        with pytest.raises(SystemExit):
            _validate_range_args(args, parser)
