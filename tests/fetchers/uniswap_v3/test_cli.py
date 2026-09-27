"""Tests for the aw_01 CLI arg parser and validation."""

import pytest

from alphawallets.fetchers.uniswap_v3.aw_01_uniswap_v3_swaps import (
    _build_arg_parser,
    _validate_range_args,
)

VALID_POOL = "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640"


class TestArgParser:
    def test_minimal_valid_args(self):
        parser = _build_arg_parser()
        args = parser.parse_args(["--chain", "ethereum", "--pool", VALID_POOL])
        assert args.chain == "ethereum"
        assert args.pool == VALID_POOL
        assert args.blocks is None
        assert args.from_block is None
        assert args.to_block is None

    def test_missing_chain_errors(self):
        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--pool", VALID_POOL])

    def test_missing_pool_errors(self):
        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--chain", "ethereum"])

    def test_invalid_chain_errors(self):
        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--chain", "solana", "--pool", VALID_POOL])

    def test_verbose_flag(self):
        parser = _build_arg_parser()
        args = parser.parse_args(["--chain", "base", "--pool", VALID_POOL, "-v"])
        assert args.verbose is True


class TestValidateRangeArgs:
    def _parse(self, extra: list[str]):
        parser = _build_arg_parser()
        args = parser.parse_args(["--chain", "ethereum", "--pool", VALID_POOL, *extra])
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

    def test_blocks_with_from_errors(self):
        args, parser = self._parse(
            ["--blocks", "500", "--from-block", "1000", "--to-block", "2000"]
        )
        with pytest.raises(SystemExit):
            _validate_range_args(args, parser)

    def test_blocks_with_from_only_errors(self):
        args, parser = self._parse(["--blocks", "500", "--from-block", "1000"])
        with pytest.raises(SystemExit):
            _validate_range_args(args, parser)
