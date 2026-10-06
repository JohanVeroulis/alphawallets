"""Tests for pipeline.pnl.airdrop_registry.

Covers:
- Dataclass invariants (CONFIRMED needs addresses; UNVERIFIED/UNAVAILABLE forbid them;
  addresses must be lowercase hex).
- The ADR 0001/0012 scope decisions carried into the registry (ARB
  UNAVAILABLE_BY_DESIGN, MORPHO on Base UNVERIFIED).
- Lookup helpers return the right default behaviour for the three statuses and
  for tokens outside the airdrop set.
"""

import re

import pytest

from alphawallets.pipeline.pnl.airdrop_registry import (
    AirdropAttributionStatus,
    AirdropRecord,
    airdrop_attribution_available,
    all_records,
    get_airdrop_record,
    is_airdrop_distributor,
)

ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
VALID_ADDR = "0x" + "a" * 40
OTHER_ADDR = "0x" + "b" * 40

# Airdrop tokens from ADR 0001. The registry must have an entry per
# (symbol, chain) listed here. Anything else should return None from
# get_airdrop_record.
EXPECTED_AIRDROP_PAIRS = {
    ("UNI", "ethereum"),
    ("ARB", "ethereum"),
    ("ENA", "ethereum"),
    ("EIGEN", "ethereum"),
    ("MORPHO", "ethereum"),
    ("MORPHO", "base"),
    ("ETHFI", "ethereum"),
}

# Tracked V1 tokens that are NOT airdrop tokens — must return None.
NON_AIRDROP_SYMBOLS = {"AAVE", "LDO", "PENDLE", "CRV", "MKR", "LINK"}


# ---------- AirdropRecord dataclass invariants ----------


class TestAirdropRecord:
    def test_confirmed_requires_distributors(self):
        with pytest.raises(ValueError, match="CONFIRMED status requires"):
            AirdropRecord(
                token_symbol="UNI",
                chain="ethereum",
                status=AirdropAttributionStatus.CONFIRMED,
                distributors=frozenset(),
                note="",
            )

    def test_unverified_forbids_distributors(self):
        with pytest.raises(ValueError, match="only valid with CONFIRMED"):
            AirdropRecord(
                token_symbol="UNI",
                chain="ethereum",
                status=AirdropAttributionStatus.UNVERIFIED,
                distributors=frozenset({VALID_ADDR}),
                note="",
            )

    def test_unavailable_by_design_forbids_distributors(self):
        with pytest.raises(ValueError, match="only valid with CONFIRMED"):
            AirdropRecord(
                token_symbol="ARB",
                chain="ethereum",
                status=AirdropAttributionStatus.UNAVAILABLE_BY_DESIGN,
                distributors=frozenset({VALID_ADDR}),
                note="",
            )

    def test_rejects_uppercase_distributor(self):
        with pytest.raises(ValueError, match="must be a 42-char lowercase"):
            AirdropRecord(
                token_symbol="UNI",
                chain="ethereum",
                status=AirdropAttributionStatus.CONFIRMED,
                distributors=frozenset({"0x" + "A" * 40}),
                note="",
            )

    def test_rejects_short_distributor(self):
        with pytest.raises(ValueError, match="must be a 42-char lowercase"):
            AirdropRecord(
                token_symbol="UNI",
                chain="ethereum",
                status=AirdropAttributionStatus.CONFIRMED,
                distributors=frozenset({"0xdeadbeef"}),
                note="",
            )

    def test_rejects_missing_0x_prefix(self):
        with pytest.raises(ValueError, match="must be a 42-char lowercase"):
            AirdropRecord(
                token_symbol="UNI",
                chain="ethereum",
                status=AirdropAttributionStatus.CONFIRMED,
                distributors=frozenset({"a" * 42}),  # right length, wrong prefix
                note="",
            )

    def test_valid_confirmed_record(self):
        record = AirdropRecord(
            token_symbol="UNI",
            chain="ethereum",
            status=AirdropAttributionStatus.CONFIRMED,
            distributors=frozenset({VALID_ADDR, OTHER_ADDR}),
            note="",
        )
        assert len(record.distributors) == 2

    def test_frozen(self):
        from dataclasses import FrozenInstanceError

        record = AirdropRecord(
            token_symbol="UNI",
            chain="ethereum",
            status=AirdropAttributionStatus.UNVERIFIED,
            distributors=frozenset(),
            note="",
        )
        with pytest.raises(FrozenInstanceError):
            record.note = "mutated"  # type: ignore[misc]


# ---------- Registry contents ----------


class TestRegistryContents:
    def test_every_expected_pair_present(self):
        for symbol, chain in EXPECTED_AIRDROP_PAIRS:
            record = get_airdrop_record(symbol, chain)
            assert record is not None, f"{symbol} on {chain} missing from registry"

    def test_arb_ethereum_is_unavailable_by_design(self):
        # ADR 0001: ARB claim happened on Arbitrum, V1 only sees bridged ARB.
        record = get_airdrop_record("ARB", "ethereum")
        assert record is not None
        assert record.status is AirdropAttributionStatus.UNAVAILABLE_BY_DESIGN
        assert record.distributors == frozenset()
        assert "Arbitrum" in record.note

    def test_morpho_base_is_unverified(self):
        # ADR 0001 Week 3 check — distribution may have happened on Base.
        record = get_airdrop_record("MORPHO", "base")
        assert record is not None
        assert record.status is AirdropAttributionStatus.UNVERIFIED

    def test_non_airdrop_tokens_return_none(self):
        # AAVE, LDO, PENDLE, CRV, MKR, LINK — tracked but no airdrop in V1 scope.
        for symbol in NON_AIRDROP_SYMBOLS:
            assert get_airdrop_record(symbol, "ethereum") is None, (
                f"{symbol} should not be in the airdrop registry"
            )

    def test_unknown_symbol_returns_none(self):
        assert get_airdrop_record("DOGECOIN", "ethereum") is None

    def test_symbol_lookup_is_case_insensitive(self):
        assert get_airdrop_record("uni", "ethereum") is not None
        assert get_airdrop_record("Uni", "ethereum") is not None


# ---------- is_airdrop_distributor ----------


class TestIsAirdropDistributor:
    def test_unverified_returns_false_regardless_of_address(self):
        # Every UNVERIFIED entry today has an empty distributors set;
        # the function must return False even for a plausible-looking address.
        assert is_airdrop_distributor(VALID_ADDR, "UNI", "ethereum") is False

    def test_unavailable_by_design_returns_false(self):
        # Any address passed as a "distributor" for ARB on Ethereum is wrong
        # by definition — there is no Ethereum distributor.
        assert is_airdrop_distributor(VALID_ADDR, "ARB", "ethereum") is False

    def test_non_airdrop_token_returns_false(self):
        # AAVE has no airdrop in V1; the function should return False for
        # any from_addr rather than raising.
        assert is_airdrop_distributor(VALID_ADDR, "AAVE", "ethereum") is False

    def test_unknown_token_returns_false(self):
        assert is_airdrop_distributor(VALID_ADDR, "SHIB", "ethereum") is False


# ---------- airdrop_attribution_available ----------


class TestAirdropAttributionAvailable:
    def test_false_for_unverified(self):
        assert airdrop_attribution_available("UNI", "ethereum") is False

    def test_false_for_unavailable_by_design(self):
        assert airdrop_attribution_available("ARB", "ethereum") is False

    def test_false_for_non_airdrop_token(self):
        assert airdrop_attribution_available("AAVE", "ethereum") is False

    def test_false_for_unknown_token(self):
        assert airdrop_attribution_available("UNKNOWN", "ethereum") is False


# ---------- all_records ----------


class TestAllRecords:
    def test_returns_every_pair_in_registry(self):
        records = all_records()
        pairs = {(r.token_symbol, r.chain) for r in records}
        assert pairs == EXPECTED_AIRDROP_PAIRS

    def test_sorted_by_symbol_then_chain(self):
        records = all_records()
        keys = [(r.token_symbol, r.chain) for r in records]
        assert keys == sorted(keys)

    def test_at_least_one_unavailable_by_design(self):
        # ARB. The explicit record is load-bearing — the ADR calls it out as
        # the one case that could be mistaken for "forgot to add".
        statuses = {r.status for r in all_records()}
        assert AirdropAttributionStatus.UNAVAILABLE_BY_DESIGN in statuses
