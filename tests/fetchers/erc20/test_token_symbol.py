"""Tests for AW_02's --token-symbol resolution against the V1 registry."""

import sys
from unittest.mock import patch

import pytest

from alphawallets.fetchers.erc20.aw_02_erc20_transfers import (
    DEFAULT_TOKENS,
    _build_arg_parser,
    _resolve_contract,
    main,
)
from alphawallets.tokens import get_token_address

UNI_ETH = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
UNI_BASE = "0xc3de830ea07524a0761646a6a4e4be0e114a3c83"


def _resolve(argv: list[str]) -> tuple[str, str]:
    parser = _build_arg_parser()
    return _resolve_contract(parser.parse_args(argv), parser)


class TestResolveContract:
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

    def test_explicit_contract_is_passed_through(self):
        address = "0x" + "a" * 40
        contract, label = _resolve(["--chain", "ethereum", "--contract", address])
        assert contract == address
        assert "--contract" in label

    def test_neither_flag_falls_back_to_uni(self):
        """Back-compat: every earlier invocation behaved this way."""
        contract, label = _resolve(["--chain", "ethereum"])
        assert contract == DEFAULT_TOKENS["UNI"]
        assert "UNI" in label
        assert "default" in label


class TestMutexAndErrors:
    def test_symbol_with_contract_errors_naming_both(self, capsys):
        parser = _build_arg_parser()
        args = parser.parse_args(
            ["--chain", "ethereum", "--token-symbol", "UNI", "--contract", "0x" + "a" * 40]
        )
        from alphawallets.fetchers.erc20.aw_02_erc20_transfers import _validate_range_args

        with pytest.raises(SystemExit):
            _validate_range_args(args, parser)
        err = capsys.readouterr().err
        assert "--token-symbol" in err
        assert "--contract" in err

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
            patch("alphawallets.fetchers.erc20.aw_02_erc20_transfers.make_web3") as mock_w3,
            patch(
                "alphawallets.fetchers.erc20.aw_02_erc20_transfers."
                "fetch_and_persist_erc20_transfers"
            ) as mock_fetch,
            patch.object(
                sys,
                "argv",
                ["prog", "--chain", "ethereum", "--token-symbol", "AAVE", "--blocks", "10"],
            ),
        ):
            mock_w3.return_value.eth.block_number = 26100000
            main()

        assert mock_fetch.call_args.kwargs["contract_address"] == get_token_address(
            "AAVE", "ethereum"
        )
        out = capsys.readouterr().out
        assert "Token:      AAVE" in out

    def test_summary_names_the_symbol_not_just_the_hex(self, capsys):
        with (
            patch("alphawallets.fetchers.erc20.aw_02_erc20_transfers.make_web3") as mock_w3,
            patch(
                "alphawallets.fetchers.erc20.aw_02_erc20_transfers."
                "fetch_and_persist_erc20_transfers"
            ),
            patch.object(
                sys,
                "argv",
                ["prog", "--chain", "base", "--token-symbol", "LINK", "--blocks", "10"],
            ),
        ):
            mock_w3.return_value.eth.block_number = 52000000
            main()

        out = capsys.readouterr().out
        assert "LINK" in out
        assert get_token_address("LINK", "base") in out
