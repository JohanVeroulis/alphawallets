"""Tests for the V1 token registry."""

import re

import pytest

from alphawallets.tokens import (
    KNOWN_SYMBOLS,
    QUOTE_ONLY_SYMBOLS,
    REQUIRED_CHAIN,
    SUBJECT_SYMBOLS,
    V1_TOKENS,
    TokenNotOnChainError,
    UnknownTokenError,
    all_token_chain_pairs,
    chains_for_token,
    get_token_address,
)

# The twelve tokens V1 ranks wallets on (CLAUDE.md Section 2).
EXPECTED_SUBJECT_SYMBOLS = {
    "UNI",
    "AAVE",
    "LDO",
    "PENDLE",
    "CRV",
    "ENA",
    "MKR",
    "MORPHO",
    "LINK",
    "ARB",
    "EIGEN",
    "ETHFI",
}

# Quote assets: tracked so swaps can anchor on their price (ADR 0015), never
# leaderboard subjects.
EXPECTED_QUOTE_SYMBOLS = {"WETH", "USDC"}

EXPECTED_SYMBOLS = EXPECTED_SUBJECT_SYMBOLS | EXPECTED_QUOTE_SYMBOLS

ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")

# The non-transferable MORPHO. symbol()/decimals()/totalSupply() cannot tell it
# apart from the real one, and the Superchain token list ships it as canonical,
# so it is the single most likely wrong address to end up in this registry.
LEGACY_MORPHO = "0x9994e35db50125e0df82e4c2dde62496ce330999"

# Base's bridged USDbC. A different token from native USDC, and the wrong one to
# anchor swap prices against. Unlike the two MORPHOs these ARE separable by
# metadata — USDbC reports symbol 'USDbC' — but the addresses are easy to
# transpose by hand.
BRIDGED_USDBC = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"


class TestRegistryCompleteness:
    def test_every_expected_symbol_present(self):
        assert set(V1_TOKENS) == EXPECTED_SYMBOLS

    def test_twelve_subjects_and_two_quote_assets(self):
        """The subject count is what CLAUDE.md Section 2 locks; quote assets are
        tracked infrastructure, not an expansion of scope."""
        assert SUBJECT_SYMBOLS == EXPECTED_SUBJECT_SYMBOLS
        assert len(SUBJECT_SYMBOLS) == 12
        assert QUOTE_ONLY_SYMBOLS == EXPECTED_QUOTE_SYMBOLS

    def test_quote_only_symbols_are_marked(self):
        for symbol in EXPECTED_QUOTE_SYMBOLS:
            assert symbol in QUOTE_ONLY_SYMBOLS
            assert symbol not in SUBJECT_SYMBOLS

    def test_subject_symbols_are_not_marked_as_quote(self):
        """A subject token wrongly marked quote-only would vanish from the
        leaderboard universe silently."""
        for symbol in EXPECTED_SUBJECT_SYMBOLS:
            assert symbol not in QUOTE_ONLY_SYMBOLS
            assert symbol in SUBJECT_SYMBOLS

    def test_the_two_sets_partition_the_registry(self):
        """No symbol is both, and none is neither — computed, not listed."""
        assert frozenset() == QUOTE_ONLY_SYMBOLS & SUBJECT_SYMBOLS
        assert frozenset(V1_TOKENS) == QUOTE_ONLY_SYMBOLS | SUBJECT_SYMBOLS

    def test_quote_assets_are_on_both_chains(self):
        """A swap on either chain needs its quote side priced."""
        for symbol in EXPECTED_QUOTE_SYMBOLS:
            assert set(V1_TOKENS[symbol]) == {"ethereum", "base"}

    def test_known_symbols_matches_the_registry(self):
        assert frozenset(V1_TOKENS) == KNOWN_SYMBOLS

    def test_every_token_has_an_ethereum_address(self):
        """Ethereum is the required chain; Base coverage is per-token."""
        missing = [s for s, addrs in V1_TOKENS.items() if REQUIRED_CHAIN not in addrs]
        assert missing == []

    def test_every_chain_key_is_in_v1_scope(self):
        """Arbitrum is enabled on the Alchemy app but out of V1 scope."""
        for symbol, addresses in V1_TOKENS.items():
            assert set(addresses) <= {"ethereum", "base"}, symbol

    def test_no_token_entry_is_empty(self):
        for symbol, addresses in V1_TOKENS.items():
            assert addresses, symbol


class TestAddressFormat:
    @pytest.mark.parametrize("symbol", sorted(EXPECTED_SYMBOLS))
    def test_addresses_are_lowercase_hex_of_the_right_length(self, symbol):
        for chain, address in V1_TOKENS[symbol].items():
            assert ADDRESS_RE.fullmatch(address), f"{symbol} on {chain}: {address}"

    def test_addresses_are_stored_lowercase(self):
        """The cache stores lowercase; a checksummed entry would match nothing."""
        for symbol, addresses in V1_TOKENS.items():
            for chain, address in addresses.items():
                assert address == address.lower(), f"{symbol} on {chain}"

    def test_no_duplicate_address_within_a_chain(self):
        """Two symbols resolving to one address would mean a copy-paste error."""
        seen: dict[tuple[str, str], str] = {}
        for symbol, addresses in V1_TOKENS.items():
            for chain, address in addresses.items():
                key = (chain, address)
                assert key not in seen, f"{symbol} duplicates {seen.get(key)} on {chain}"
                seen[key] = symbol


class TestMorphoGuard:
    """Regression guard for the one address most likely to be wrong here.

    symbol(), decimals() and totalSupply() are effectively identical on the two
    MORPHO deployments, so metadata verification passes on the legacy token. The
    Superchain token list ships that legacy address as canonical for Ethereum.
    See the ADR 0008 amendment for the discrimination method.
    """

    def test_registry_holds_the_transferable_morpho(self):
        assert V1_TOKENS["MORPHO"]["ethereum"] == ("0x58d97b57bb95320f9a05dc918aef65434969c2b2")

    def test_bridged_usdbc_appears_nowhere(self):
        """Base USDC must be the native Circle-issued token, not the bridge's."""
        for symbol, addresses in V1_TOKENS.items():
            for chain, address in addresses.items():
                assert address != BRIDGED_USDBC, f"{symbol} on {chain} is USDbC"

    def test_base_usdc_is_the_native_token(self):
        assert V1_TOKENS["USDC"]["base"] == "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

    def test_legacy_morpho_appears_nowhere(self):
        for symbol, addresses in V1_TOKENS.items():
            for chain, address in addresses.items():
                assert address != LEGACY_MORPHO, (
                    f"{symbol} on {chain} is the non-transferable MORPHO"
                )


class TestGetTokenAddress:
    def test_resolves_a_known_symbol(self):
        assert get_token_address("UNI", "ethereum") == (
            "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
        )

    def test_resolves_on_base(self):
        assert get_token_address("UNI", "base") == ("0xc3de830ea07524a0761646a6a4e4be0e114a3c83")

    @pytest.mark.parametrize("given", ["uni", "UNI", "Uni", "uNi"])
    def test_symbol_is_case_insensitive(self, given):
        assert get_token_address(given, "ethereum") == get_token_address("UNI", "ethereum")

    def test_surrounding_whitespace_tolerated(self):
        assert get_token_address("  uni  ", "ethereum") == get_token_address("UNI", "ethereum")

    @pytest.mark.parametrize("symbol", sorted(EXPECTED_SYMBOLS))
    def test_every_symbol_resolves_on_ethereum(self, symbol):
        assert ADDRESS_RE.fullmatch(get_token_address(symbol, "ethereum"))

    def test_unknown_symbol_raises_naming_the_symbol_and_the_valid_set(self):
        with pytest.raises(UnknownTokenError) as excinfo:
            get_token_address("NOTATOKEN", "ethereum")
        message = str(excinfo.value)
        assert "NOTATOKEN" in message
        assert "UNI" in message  # the valid set is listed
        assert "ETHFI" in message

    def test_unknown_symbol_is_a_keyerror(self):
        """Subclassing KeyError keeps registry-shaped call sites working."""
        with pytest.raises(KeyError):
            get_token_address("NOTATOKEN", "ethereum")

    def test_token_not_on_chain_raises_naming_both(self):
        with pytest.raises(TokenNotOnChainError) as excinfo:
            get_token_address("LDO", "base")
        message = str(excinfo.value)
        assert "LDO" in message
        assert "base" in message
        assert "ethereum" in message  # what IS available

    def test_not_on_chain_message_explains_that_absence_means_unverified(self):
        """An operator must not read a missing Base entry as 'no such token'."""
        with pytest.raises(TokenNotOnChainError, match="unverified, not nonexistent"):
            get_token_address("ENA", "base")

    def test_not_on_chain_does_not_fall_back_to_ethereum(self):
        """A silent fallback would fetch the wrong chain under a plausible label."""
        with pytest.raises(TokenNotOnChainError):
            get_token_address("MKR", "base")

    @pytest.mark.parametrize("symbol", ["LDO", "ENA", "MKR", "ARB", "EIGEN", "ETHFI"])
    def test_the_six_unverified_base_tokens_raise(self, symbol):
        """Pins today's Base coverage: adding one of these must update this list."""
        with pytest.raises(TokenNotOnChainError):
            get_token_address(symbol, "base")


class TestChainsForToken:
    def test_lists_both_chains(self):
        assert chains_for_token("UNI") == ["base", "ethereum"]

    def test_lists_one_chain(self):
        assert chains_for_token("LDO") == ["ethereum"]

    def test_case_insensitive(self):
        assert chains_for_token("uni") == chains_for_token("UNI")

    def test_unknown_symbol_raises(self):
        with pytest.raises(UnknownTokenError):
            chains_for_token("NOTATOKEN")


class TestAllTokenChainPairs:
    def test_pair_count_matches_the_registry(self):
        """Computed from the registry, so adding a token cannot silently skip it."""
        expected = sum(len(addresses) for addresses in V1_TOKENS.values())
        assert len(all_token_chain_pairs()) == expected

    def test_twenty_two_pairs_today(self):
        """12 subject Ethereum + 6 subject Base + 2 quote assets x 2 chains."""
        pairs = all_token_chain_pairs()
        assert len(pairs) == 22
        assert sum(1 for _, chain in pairs if chain == "ethereum") == 14
        assert sum(1 for _, chain in pairs if chain == "base") == 8

    def test_subject_pair_count_is_unchanged(self):
        """Adding quote assets must not change the subject surface — that is what
        makes this additive rather than a scope expansion."""
        subject_pairs = [
            (symbol, chain)
            for symbol, chain in all_token_chain_pairs()
            if symbol in SUBJECT_SYMBOLS
        ]
        assert len(subject_pairs) == 18
        assert sum(1 for _, chain in subject_pairs if chain == "ethereum") == 12
        assert sum(1 for _, chain in subject_pairs if chain == "base") == 6

    def test_every_pair_resolves(self):
        for symbol, chain in all_token_chain_pairs():
            assert ADDRESS_RE.fullmatch(get_token_address(symbol, chain))

    def test_sorted_and_deterministic(self):
        pairs = all_token_chain_pairs()
        assert pairs == sorted(pairs)
        assert pairs == all_token_chain_pairs()
