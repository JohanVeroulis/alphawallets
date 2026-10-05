"""Tests for the route-less token set."""

from alphawallets.unpriceable import KNOWN_UNPRICEABLE_TOKENS, is_unpriceable

MKR_ETH = "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2"
UNI_ETH = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"


class TestKnownSet:
    def test_contains_mkr_on_ethereum(self):
        assert ("ethereum", MKR_ETH) in KNOWN_UNPRICEABLE_TOKENS

    def test_only_mkr_listed_today(self):
        """Pins the set: adding an entry suppresses a real signal, so it is deliberate."""
        assert frozenset({("ethereum", MKR_ETH)}) == KNOWN_UNPRICEABLE_TOKENS

    def test_every_address_is_lowercase(self):
        """The lookup lowercases its input, so a mixed-case entry would never match."""
        for _chain, address in KNOWN_UNPRICEABLE_TOKENS:
            assert address == address.lower()

    def test_every_chain_is_in_v1_scope(self):
        for chain, _address in KNOWN_UNPRICEABLE_TOKENS:
            assert chain in {"ethereum", "base"}


class TestIsUnpriceable:
    def test_listed_token_is_unpriceable(self):
        assert is_unpriceable("ethereum", MKR_ETH) is True

    def test_unlisted_token_is_not(self):
        assert is_unpriceable("ethereum", UNI_ETH) is False

    def test_address_is_case_insensitive(self):
        assert is_unpriceable("ethereum", MKR_ETH.upper()) is True

    def test_checksummed_address_matches(self):
        checksummed = "0x9f8F72aA9304c8B593d555F12eF6589cC3A579A2"
        assert is_unpriceable("ethereum", checksummed) is True

    def test_surrounding_whitespace_tolerated(self):
        assert is_unpriceable("ethereum", f"  {MKR_ETH}  ") is True

    def test_scoped_per_chain(self):
        """MKR is listed on Ethereum only; the same address on Base is not covered.

        The gap is a property of (chain, token) because DefiLlama's coin ids are
        chain-prefixed, so a token can be served on one chain and not another.
        """
        assert is_unpriceable("base", MKR_ETH) is False

    def test_unknown_token_returns_false(self):
        assert is_unpriceable("ethereum", "0x" + "9" * 40) is False

    def test_returns_a_bool_not_a_truthy_value(self):
        """Callers pass this straight into a Pydantic bool field."""
        assert isinstance(is_unpriceable("ethereum", MKR_ETH), bool)
        assert isinstance(is_unpriceable("ethereum", UNI_ETH), bool)
