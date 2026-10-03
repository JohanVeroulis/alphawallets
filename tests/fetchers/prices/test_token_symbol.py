"""Tests for AW_03's --token-symbol resolution against the V1 registry."""

import sys
from unittest.mock import patch

import pytest

from alphawallets.fetchers.prices.aw_03_defillama_historical_prices import (
    DEFAULT_TOKENS,
    _build_arg_parser,
    _resolve_token,
    main,
)
from alphawallets.tokens import get_token_address

UNI_ETH = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
UNI_BASE = "0xc3de830ea07524a0761646a6a4e4be0e114a3c83"


def _resolve(argv: list[str]) -> tuple[str, str]:
    parser = _build_arg_parser()
    return _resolve_token(parser.parse_args(argv), parser)


class TestResolveToken:
    def test_symbol_resolves_on_ethereum(self):
        contract, label = _resolve(["--chain", "ethereum", "--token-symbol", "UNI"])
        assert contract == UNI_ETH
        assert label == "UNI"

    def test_same_symbol_resolves_differently_per_chain(self):
        """--chain disambiguates, which is the whole point of the registry."""
        eth, _ = _resolve(["--chain", "ethereum", "--token-symbol", "UNI"])
        base, _ = _resolve(["--chain", "base", "--token-symbol", "UNI"])
        assert eth == UNI_ETH
        assert base == UNI_BASE
        assert eth != base

    @pytest.mark.parametrize("symbol", ["AAVE", "LINK", "MORPHO", "CRV", "MKR", "ETHFI"])
    def test_every_symbol_resolves_to_the_registry_value(self, symbol):
        contract, label = _resolve(["--chain", "ethereum", "--token-symbol", symbol])
        assert contract == get_token_address(symbol, "ethereum")
        assert label == symbol

    def test_morpho_resolves_to_the_transferable_address(self):
        """The legacy MORPHO is non-transferable and unpriceable — see ADR 0008."""
        contract, _ = _resolve(["--chain", "ethereum", "--token-symbol", "MORPHO"])
        assert contract == "0x58d97b57bb95320f9a05dc918aef65434969c2b2"

    def test_explicit_address_is_passed_through(self):
        address = "0x" + "a" * 40
        contract, label = _resolve(["--chain", "ethereum", "--token", address])
        assert contract == address
        assert "--token" in label

    def test_neither_flag_falls_back_to_uni(self):
        """Back-compat: every earlier invocation behaved this way."""
        contract, label = _resolve(["--chain", "ethereum"])
        assert contract == DEFAULT_TOKENS["UNI"]
        assert "UNI" in label
        assert "default" in label


class TestMutexAndErrors:
    def test_symbol_with_address_errors_naming_both(self, capsys):
        parser = _build_arg_parser()
        args = parser.parse_args(
            ["--chain", "ethereum", "--token-symbol", "UNI", "--token", "0x" + "a" * 40]
        )
        from alphawallets.fetchers.prices.aw_03_defillama_historical_prices import (
            _validate_range_args,
        )

        with pytest.raises(SystemExit):
            _validate_range_args(args, parser)
        err = capsys.readouterr().err
        assert "--token-symbol" in err
        assert "--token" in err

    def test_unknown_symbol_rejected_by_argparse_choices(self, capsys):
        """Caught at parse time, so the error lists every valid symbol."""
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args(["--chain", "ethereum", "--token-symbol", "NOTATOKEN"])
        err = capsys.readouterr().err
        assert "NOTATOKEN" in err
        assert "UNI" in err

    def test_token_not_on_base_errors_naming_both(self, capsys):
        """LDO has no verified Base address, so --chain base must fail loudly."""
        with pytest.raises(SystemExit):
            _resolve(["--chain", "base", "--token-symbol", "LDO"])
        err = capsys.readouterr().err
        assert "LDO" in err
        assert "base" in err

    def test_not_on_base_does_not_silently_use_the_ethereum_address(self, capsys):
        with pytest.raises(SystemExit):
            _resolve(["--chain", "base", "--token-symbol", "ARB"])
        assert "unverified, not nonexistent" in capsys.readouterr().err


class TestCliIntegration:
    def test_symbol_reaches_the_orchestrator(self, capsys):
        with (
            patch(
                "alphawallets.fetchers.prices.aw_03_defillama_historical_prices."
                "fetch_and_persist_prices"
            ) as mock_fetch,
            patch.object(
                sys,
                "argv",
                ["prog", "--chain", "ethereum", "--token-symbol", "AAVE", "--span-days", "1"],
            ),
        ):
            main()

        assert mock_fetch.call_args.kwargs["token_address"] == get_token_address("AAVE", "ethereum")
        out = capsys.readouterr().out
        assert "Token:  AAVE" in out

    def test_summary_names_the_symbol_and_the_address(self, capsys):
        with (
            patch(
                "alphawallets.fetchers.prices.aw_03_defillama_historical_prices."
                "fetch_and_persist_prices"
            ),
            patch.object(
                sys,
                "argv",
                ["prog", "--chain", "base", "--token-symbol", "LINK", "--span-days", "1"],
            ),
        ):
            main()

        out = capsys.readouterr().out
        assert "LINK" in out
        assert get_token_address("LINK", "base") in out

    def test_span_days_still_works_with_a_symbol(self, capsys):
        """--token-symbol is mutually exclusive with --token, not with --span-days."""
        with (
            patch(
                "alphawallets.fetchers.prices.aw_03_defillama_historical_prices."
                "fetch_and_persist_prices"
            ) as mock_fetch,
            patch.object(
                sys,
                "argv",
                ["prog", "--chain", "ethereum", "--token-symbol", "CRV", "--span-days", "7"],
            ),
        ):
            main()

        assert mock_fetch.call_args.kwargs["span_days"] == 7
