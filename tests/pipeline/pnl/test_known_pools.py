"""Tests for the known-pool set (ADR 0014)."""

import re

import pytest

from alphawallets.fetchers.uniswap_v3.aw_01_uniswap_v3_swaps import DEFAULT_POOLS_BY_CHAIN
from alphawallets.pipeline.pnl.known_pools import KNOWN_POOLS, is_known_pool

ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")

UNI_WETH_ETH = "0x1d42064fc4beb5f8aaf85f4617ae8b3b5b8bd801"  # UNI/WETH 0.3%, ethereum
LINK_WETH_BASE = "0x224a5d3f2155f2f85af70b6d72aea61a15273ff4"  # LINK/WETH 0.3%, base
NOT_A_POOL = "0x" + "7" * 40


class TestKnownPoolsSet:
    def test_has_at_least_the_nineteen_configured_pools(self):
        assert len(KNOWN_POOLS) >= 19

    def test_size_matches_the_fetcher_configuration(self):
        """Computed from the config, so the count is derived rather than asserted.

        If AW_01 gains a pool this stays true; a hardcoded 19 would go stale and
        start lying about coverage.
        """
        expected = sum(len(pools) for pools in DEFAULT_POOLS_BY_CHAIN.values())
        assert len(KNOWN_POOLS) == expected

    def test_every_entry_is_a_chain_address_pair(self):
        for entry in KNOWN_POOLS:
            assert isinstance(entry, tuple)
            assert len(entry) == 2

    def test_every_address_is_lowercase_hex(self):
        """The fetcher config carries checksummed addresses for readability; the
        cache stores lowercase. Comparing the two forms would match nothing,
        which here would silently mean "no trade"."""
        for _chain, address in KNOWN_POOLS:
            assert ADDRESS_RE.fullmatch(address), address

    def test_every_chain_is_in_v1_scope(self):
        assert {chain for chain, _address in KNOWN_POOLS} <= {"ethereum", "base"}

    def test_both_chains_are_represented(self):
        assert {chain for chain, _address in KNOWN_POOLS} == {"ethereum", "base"}

    def test_contains_every_configured_address(self):
        for chain, pools in DEFAULT_POOLS_BY_CHAIN.items():
            for address in pools.values():
                assert (chain, address.lower()) in KNOWN_POOLS


class TestIsKnownPool:
    def test_configured_ethereum_pool(self):
        assert is_known_pool("ethereum", UNI_WETH_ETH) is True

    def test_configured_base_pool(self):
        assert is_known_pool("base", LINK_WETH_BASE) is True

    def test_random_address_is_not_a_pool(self):
        assert is_known_pool("ethereum", NOT_A_POOL) is False

    def test_pool_address_on_the_wrong_chain(self):
        """The same hex on another chain is an unrelated account; treating it as a
        pool there would realize a sale that never happened."""
        assert is_known_pool("base", UNI_WETH_ETH) is False
        assert is_known_pool("ethereum", LINK_WETH_BASE) is False

    @pytest.mark.parametrize(
        "given",
        [
            UNI_WETH_ETH,
            UNI_WETH_ETH.upper().replace("0X", "0x"),
            "0x1d42064Fc4Beb5F8aAF85F4617AE8b3b5B8Bd801",  # checksummed form
        ],
    )
    def test_case_insensitive(self, given):
        assert is_known_pool("ethereum", given) is True

    def test_surrounding_whitespace_tolerated(self):
        assert is_known_pool("ethereum", f"  {UNI_WETH_ETH}  ") is True

    def test_returns_a_bool_not_a_truthy_value(self):
        assert isinstance(is_known_pool("ethereum", UNI_WETH_ETH), bool)
        assert isinstance(is_known_pool("ethereum", NOT_A_POOL), bool)

    def test_every_configured_pool_resolves(self):
        for chain, pools in DEFAULT_POOLS_BY_CHAIN.items():
            for label, address in pools.items():
                assert is_known_pool(chain, address) is True, f"{label} on {chain}"
