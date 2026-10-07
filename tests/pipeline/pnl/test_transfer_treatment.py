"""Tests for the transfer treatment classifier (ADR 0012 step 3)."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from alphawallets.fetchers.erc20.models import ERC20Transfer
from alphawallets.fetchers.uniswap_v3.aw_01_uniswap_v3_swaps import DEFAULT_POOLS_BY_CHAIN
from alphawallets.pipeline.pnl import airdrop_registry
from alphawallets.pipeline.pnl.airdrop_registry import (
    AirdropAttributionStatus,
    AirdropRecord,
)
from alphawallets.pipeline.pnl.transfer_treatment import (
    TransferClassification,
    TransferTreatment,
    classify_transfer,
)

WALLET = "0x" + "1" * 40
OTHER = "0x" + "2" * 40
THIRD = "0x" + "3" * 40
DISTRIBUTOR = "0x" + "d" * 40

UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
ARB = "0xb50721bcf8d664c30412cfbc6cf7a15145234ad1"
AAVE = "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9"
MORPHO_BASE = "0xbaa5cc21fd487b8fcc2f632f3f4e8d37262a0842"
LINK_BASE = "0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196"

# Configured V3 pools (ADR 0014). Real addresses, verified on-chain in PR #28.
UNI_WETH_ETH = "0x1d42064fc4beb5f8aaf85f4617ae8b3b5b8bd801"
LINK_WETH_BASE = "0x224a5d3f2155f2f85af70b6d72aea61a15273ff4"

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def transfer(
    from_addr: str,
    to_addr: str,
    token_address: str = UNI,
    chain: str = "ethereum",
) -> ERC20Transfer:
    """Build a decoded transfer. Only direction and counterparty matter here."""
    return ERC20Transfer(
        chain=chain,
        block_number=26_000_000,
        block_timestamp=T0,
        tx_hash="0x" + "a" * 64,
        log_index=1,
        unique_id="0x" + "a" * 64 + ":log:1",
        token_address=token_address,
        from_addr=from_addr,
        to_addr=to_addr,
        value_raw=str(10**18),
        token_decimals=18,
    )


@pytest.fixture
def confirmed_uni_distributor(monkeypatch):
    """Promote UNI/ethereum to CONFIRMED with one distributor.

    Patches the registry dict rather than stubbing is_airdrop_distributor, so
    the real lookup path runs — including AirdropRecord's own invariants. No
    entry is CONFIRMED yet in production, and this fixture must not be mistaken
    for one: it exists to exercise the AIRDROP_IN branch.
    """
    record = AirdropRecord(
        token_symbol="UNI",
        chain="ethereum",
        status=AirdropAttributionStatus.CONFIRMED,
        distributors=frozenset({DISTRIBUTOR}),
        note="Test fixture only — not a verified address.",
    )
    monkeypatch.setitem(airdrop_registry._REGISTRY, ("UNI", "ethereum"), record)
    return DISTRIBUTOR


class TestIncomingFromUnknownAddress:
    """ADR 0012 decision 2: price-at-receipt, not zero."""

    def test_classifies_as_trading_in(self):
        result = classify_transfer(transfer(OTHER, WALLET), WALLET, "UNI", 8.80)
        assert result.treatment is TransferTreatment.TRADING_IN
        assert result.source == "trading"
        assert result.unit_cost_usd == 8.80
        assert result.is_self_transfer is False

    def test_cex_withdrawal_shape_is_not_free(self):
        """The commonest wallet shape; zero-cost here would inflate every PnL."""
        result = classify_transfer(transfer(THIRD, WALLET), WALLET, "AAVE", 179.46)
        assert result.unit_cost_usd == 179.46
        assert result.source == "trading"


class TestIncomingFromDistributor:
    """ADR 0012 decision 4: zero cost, airdrop source."""

    def test_classifies_as_airdrop_in(self, confirmed_uni_distributor):
        result = classify_transfer(transfer(confirmed_uni_distributor, WALLET), WALLET, "UNI", None)
        assert result.treatment is TransferTreatment.AIRDROP_IN
        assert result.source == "airdrop"
        assert result.unit_cost_usd == 0.0
        assert result.is_self_transfer is False

    def test_a_different_sender_of_the_same_token_is_trading(self, confirmed_uni_distributor):
        """Only the distributor address is an airdrop; everything else is a buy."""
        result = classify_transfer(transfer(OTHER, WALLET), WALLET, "UNI", 8.80)
        assert result.treatment is TransferTreatment.TRADING_IN

    def test_distributor_sending_to_someone_else_is_ignored(self, confirmed_uni_distributor):
        result = classify_transfer(transfer(confirmed_uni_distributor, OTHER), WALLET, "UNI", None)
        assert result.treatment is TransferTreatment.IGNORED

    def test_symbol_lookup_is_case_insensitive(self, confirmed_uni_distributor):
        result = classify_transfer(transfer(confirmed_uni_distributor, WALLET), WALLET, "uni", None)
        assert result.treatment is TransferTreatment.AIRDROP_IN

    def test_distributor_on_the_wrong_chain_is_not_an_airdrop(self, confirmed_uni_distributor):
        """The registry keys on (symbol, chain); the same address on Base is not it."""
        result = classify_transfer(
            transfer(confirmed_uni_distributor, WALLET, token_address=UNI, chain="base"),
            WALLET,
            "UNI",
            8.80,
        )
        assert result.treatment is TransferTreatment.TRADING_IN


class TestOutgoing:
    """ADR 0012 decision 3: reduce the stack, realize nothing."""

    def test_classifies_as_out(self):
        result = classify_transfer(transfer(WALLET, OTHER), WALLET, "UNI", None)
        assert result.treatment is TransferTreatment.OUT
        assert result.source is None
        assert result.unit_cost_usd is None
        assert result.is_self_transfer is False

    def test_out_to_a_distributor_is_still_out(self, confirmed_uni_distributor):
        """Direction decides; the counterparty being a distributor is irrelevant."""
        result = classify_transfer(transfer(WALLET, confirmed_uni_distributor), WALLET, "UNI", None)
        assert result.treatment is TransferTreatment.OUT

    def test_passed_cost_is_discarded(self):
        """OUT takes no cost basis, whatever the caller resolved.

        We do not know whether the wallet sold, paid, bridged, or self-custodied,
        so carrying a price would imply a sale that may not have happened.
        """
        result = classify_transfer(transfer(WALLET, OTHER), WALLET, "UNI", 8.80)
        assert result.unit_cost_usd is None


class TestSelfTransfer:
    """ADR 0012 decision 2's second exception, deferred to V1.5."""

    def test_classifies_as_self_transfer(self):
        result = classify_transfer(transfer(WALLET, WALLET), WALLET, "UNI", 8.80)
        assert result.treatment is TransferTreatment.SELF_TRANSFER
        assert result.is_self_transfer is True

    def test_treated_as_a_trading_in_for_now(self):
        """V1 cannot detect that two addresses are one person, so the cost
        re-bases at the transfer price. Flagged rather than silently wrong."""
        result = classify_transfer(transfer(WALLET, WALLET), WALLET, "UNI", 8.80)
        assert result.source == "trading"
        assert result.unit_cost_usd == 8.80

    def test_checked_before_the_directional_branches(self):
        """A self-transfer satisfies both sides; testing either alone loses the flag."""
        result = classify_transfer(transfer(WALLET, WALLET), WALLET, "UNI", 8.80)
        assert result.treatment is not TransferTreatment.TRADING_IN
        assert result.treatment is not TransferTreatment.OUT

    def test_a_distributor_self_transfer_is_still_self(self, confirmed_uni_distributor):
        result = classify_transfer(
            transfer(confirmed_uni_distributor, confirmed_uni_distributor),
            confirmed_uni_distributor,
            "UNI",
            8.80,
        )
        assert result.treatment is TransferTreatment.SELF_TRANSFER

    def test_only_self_transfer_sets_the_flag(self):
        for tx in (transfer(OTHER, WALLET), transfer(WALLET, OTHER)):
            assert classify_transfer(tx, WALLET, "UNI", 8.80).is_self_transfer is False


class TestIgnored:
    """Defensive: the calculator pre-filters, so this signals a filter bug."""

    def test_wallet_is_neither_party(self):
        result = classify_transfer(transfer(OTHER, THIRD), WALLET, "UNI", 8.80)
        assert result.treatment is TransferTreatment.IGNORED
        assert result.source is None
        assert result.unit_cost_usd is None
        assert result.is_self_transfer is False

    def test_returns_rather_than_raises(self):
        """Crashing a long batch over a no-op case is worse than doing nothing."""
        result = classify_transfer(transfer(OTHER, THIRD), WALLET, "UNI", 8.80)
        assert isinstance(result, TransferClassification)


class TestRegistryIntegration:
    """The registry's three states, exercised through the classifier."""

    def test_arb_is_trading_despite_being_an_airdrop_token(self):
        """UNAVAILABLE_BY_DESIGN: the claim was on Arbitrum, so no Ethereum
        distributor exists to match. ADR 0012 decision 4 states this outcome
        explicitly — it is correct behaviour, not a gap to work around, and the
        classifier contains no ARB branch."""
        result = classify_transfer(
            transfer(OTHER, WALLET, token_address=ARB), WALLET, "ARB", 0.1976
        )
        assert result.treatment is TransferTreatment.TRADING_IN
        assert result.source == "trading"

    def test_arb_from_any_address_is_trading(self):
        for sender in (OTHER, THIRD, DISTRIBUTOR):
            result = classify_transfer(
                transfer(sender, WALLET, token_address=ARB), WALLET, "ARB", 0.1976
            )
            assert result.treatment is TransferTreatment.TRADING_IN

    def test_morpho_on_base_is_trading_while_unverified(self):
        """ADR 0001's Week 3 check is still open; UNVERIFIED routes to trading."""
        result = classify_transfer(
            transfer(OTHER, WALLET, token_address=MORPHO_BASE, chain="base"),
            WALLET,
            "MORPHO",
            2.5554,
        )
        assert result.treatment is TransferTreatment.TRADING_IN

    def test_uni_is_trading_while_its_entry_is_unverified(self):
        """UNI has a registry entry but no confirmed distributor, so nothing
        matches — the production state today."""
        result = classify_transfer(transfer(OTHER, WALLET), WALLET, "UNI", 8.80)
        assert result.treatment is TransferTreatment.TRADING_IN

    @pytest.mark.parametrize("symbol", ["AAVE", "LINK", "LDO", "PENDLE", "CRV", "MKR"])
    def test_tokens_absent_from_the_registry_are_trading(self, symbol):
        """No airdrop in V1 scope: get_airdrop_record returns None."""
        result = classify_transfer(transfer(OTHER, WALLET, token_address=AAVE), WALLET, symbol, 1.0)
        assert result.treatment is TransferTreatment.TRADING_IN


class TestPriceHandling:
    def test_none_cost_stays_none_on_trading_in(self):
        """The unpriceable-at-receipt case. The classifier records the absence;
        the calculator flags has_unpriceable_events rather than inventing a cost."""
        result = classify_transfer(transfer(OTHER, WALLET), WALLET, "UNI", None)
        assert result.treatment is TransferTreatment.TRADING_IN
        assert result.unit_cost_usd is None

    def test_zero_cost_is_not_reclassified_as_airdrop(self):
        """A legitimately zero-priced transfer is not free income.

        Source is what distinguishes the two, not the cost value — collapsing
        them would misattribute trading PnL as airdrop PnL.
        """
        result = classify_transfer(transfer(OTHER, WALLET), WALLET, "UNI", 0.0)
        assert result.treatment is TransferTreatment.TRADING_IN
        assert result.source == "trading"
        assert result.unit_cost_usd == 0.0

    def test_airdrop_overrides_a_passed_cost(self, confirmed_uni_distributor):
        """An airdrop lot is zero-cost by definition; honouring the caller's
        price would book a cost that was never paid."""
        result = classify_transfer(transfer(confirmed_uni_distributor, WALLET), WALLET, "UNI", 8.80)
        assert result.unit_cost_usd == 0.0

    def test_self_transfer_keeps_the_passed_cost(self):
        result = classify_transfer(transfer(WALLET, WALLET), WALLET, "UNI", 8.80)
        assert result.unit_cost_usd == 8.80

    def test_self_transfer_with_no_price_is_none(self):
        result = classify_transfer(transfer(WALLET, WALLET), WALLET, "UNI", None)
        assert result.unit_cost_usd is None


class TestAddressNormalization:
    def test_uppercase_wallet_matches_a_lowercase_transfer(self):
        """The wallet is lowercased on entry, matching FIFOEngine's constructor.

        Without this, a checksummed address from a caller would silently produce
        IGNORED — a wallet with real activity reporting no events at all.
        """
        result = classify_transfer(transfer(OTHER, WALLET), WALLET.upper(), "UNI", 8.80)
        assert result.treatment is TransferTreatment.TRADING_IN

    def test_uppercase_wallet_matches_on_the_out_path(self):
        result = classify_transfer(transfer(WALLET, OTHER), WALLET.upper(), "UNI", None)
        assert result.treatment is TransferTreatment.OUT

    def test_uppercase_wallet_still_detects_a_self_transfer(self):
        result = classify_transfer(transfer(WALLET, WALLET), WALLET.upper(), "UNI", 8.80)
        assert result.treatment is TransferTreatment.SELF_TRANSFER

    def test_surrounding_whitespace_tolerated(self):
        result = classify_transfer(transfer(OTHER, WALLET), f"  {WALLET}  ", "UNI", 8.80)
        assert result.treatment is TransferTreatment.TRADING_IN


class TestResultIsFrozen:
    def test_mutation_raises(self):
        result = classify_transfer(transfer(OTHER, WALLET), WALLET, "UNI", 8.80)
        with pytest.raises(FrozenInstanceError):
            result.treatment = TransferTreatment.OUT

    def test_cost_cannot_be_rewritten(self):
        result = classify_transfer(transfer(OTHER, WALLET), WALLET, "UNI", 8.80)
        with pytest.raises(FrozenInstanceError):
            result.unit_cost_usd = 0.0


class TestTreatmentEnum:
    def test_every_treatment_is_reachable(self):
        """A value nothing produces is dead weight; this pins the mapping.

        TRADING_OUT_REALIZING and AIRDROP_IN are covered separately: the first
        needs a pool destination, the second a CONFIRMED registry entry.
        """
        produced = {
            classify_transfer(transfer(OTHER, WALLET), WALLET, "UNI", 8.8).treatment,
            classify_transfer(transfer(WALLET, OTHER), WALLET, "UNI", None).treatment,
            classify_transfer(transfer(WALLET, WALLET), WALLET, "UNI", 8.8).treatment,
            classify_transfer(transfer(OTHER, THIRD), WALLET, "UNI", 8.8).treatment,
        }
        assert produced == {
            TransferTreatment.TRADING_IN,
            TransferTreatment.OUT,
            TransferTreatment.SELF_TRANSFER,
            TransferTreatment.IGNORED,
        }

    def test_airdrop_in_is_reachable_with_a_confirmed_entry(self, confirmed_uni_distributor):
        """The fifth value, which needs a CONFIRMED registry entry to occur."""
        result = classify_transfer(transfer(confirmed_uni_distributor, WALLET), WALLET, "UNI", None)
        assert result.treatment is TransferTreatment.AIRDROP_IN

    def test_source_is_set_exactly_for_the_lot_creating_treatments(self):
        """OUT and IGNORED create no lot, so a source would be meaningless."""
        lot_creating = {
            TransferTreatment.AIRDROP_IN,
            TransferTreatment.TRADING_IN,
            TransferTreatment.SELF_TRANSFER,
        }
        cases = [
            classify_transfer(transfer(OTHER, WALLET), WALLET, "UNI", 8.8),
            classify_transfer(transfer(WALLET, OTHER), WALLET, "UNI", None),
            classify_transfer(transfer(WALLET, WALLET), WALLET, "UNI", 8.8),
            classify_transfer(transfer(OTHER, THIRD), WALLET, "UNI", 8.8),
        ]
        for result in cases:
            assert (result.source is not None) == (result.treatment in lot_creating)


class TestOutToKnownPool:
    """ADR 0014: the destination makes an OUT a realization or leaves it decision 3."""

    def test_ethereum_pool_realizes(self):
        result = classify_transfer(
            transfer(WALLET, UNI_WETH_ETH), WALLET, "UNI", None, unit_sale_usd=12.0
        )
        assert result.treatment is TransferTreatment.TRADING_OUT_REALIZING
        assert result.unit_sale_usd == 12.0

    def test_base_pool_realizes(self):
        result = classify_transfer(
            transfer(WALLET, LINK_WETH_BASE, token_address=LINK_BASE, chain="base"),
            WALLET,
            "LINK",
            None,
            unit_sale_usd=14.0,
        )
        assert result.treatment is TransferTreatment.TRADING_OUT_REALIZING
        assert result.unit_sale_usd == 14.0

    def test_non_pool_destination_is_unchanged(self):
        """A CEX-shaped address keeps decision 3: stack reduced, nothing realized."""
        result = classify_transfer(transfer(WALLET, OTHER), WALLET, "UNI", None, unit_sale_usd=12.0)
        assert result.treatment is TransferTreatment.OUT
        assert result.unit_sale_usd is None

    def test_pool_with_no_price_still_realizes(self):
        """The treatment is decided by the destination, not by price availability.

        A None sale price means the caller could not resolve one; the calculator
        flags the row rather than pricing the sale at a guess, because an invented
        sale price lands in realized PnL indistinguishable from a measured one.
        """
        result = classify_transfer(
            transfer(WALLET, UNI_WETH_ETH), WALLET, "UNI", None, unit_sale_usd=None
        )
        assert result.treatment is TransferTreatment.TRADING_OUT_REALIZING
        assert result.unit_sale_usd is None

    def test_pool_address_on_the_wrong_chain_does_not_realize(self):
        """(chain, address) is the key, not address alone.

        The same hex on another chain is an unrelated account, and realizing
        against it would book a sale that never happened.
        """
        result = classify_transfer(
            transfer(WALLET, UNI_WETH_ETH, chain="base"),
            WALLET,
            "UNI",
            None,
            unit_sale_usd=12.0,
        )
        assert result.treatment is TransferTreatment.OUT
        assert result.unit_sale_usd is None

    def test_realizing_out_carries_no_cost_basis(self):
        """It consumes lots; each Realization carries the source of the lot it
        consumed, so the treatment itself has neither cost nor source."""
        result = classify_transfer(
            transfer(WALLET, UNI_WETH_ETH), WALLET, "UNI", 8.0, unit_sale_usd=12.0
        )
        assert result.unit_cost_usd is None
        assert result.source is None
        assert result.is_self_transfer is False

    def test_incoming_from_a_pool_is_still_trading_in(self):
        """The output leg of a swap: an acquisition at price-at-receipt, not a sale."""
        result = classify_transfer(
            transfer(UNI_WETH_ETH, WALLET), WALLET, "UNI", 8.0, unit_sale_usd=12.0
        )
        assert result.treatment is TransferTreatment.TRADING_IN
        assert result.unit_cost_usd == 8.0
        assert result.unit_sale_usd is None

    def test_self_transfer_wins_over_the_pool_check(self):
        """Ordering from PR #34 is preserved: self-transfer is tested first.

        A pool sending to itself is not a wallet's trade.
        """
        result = classify_transfer(
            transfer(UNI_WETH_ETH, UNI_WETH_ETH),
            UNI_WETH_ETH,
            "UNI",
            8.0,
            unit_sale_usd=12.0,
        )
        assert result.treatment is TransferTreatment.SELF_TRANSFER

    def test_every_configured_pool_realizes(self):
        """Derived from the config rather than spot-checked, so a new pool in
        AW_01 is covered without editing this test."""
        for chain, pools in DEFAULT_POOLS_BY_CHAIN.items():
            for address in pools.values():
                result = classify_transfer(
                    transfer(WALLET, address.lower(), chain=chain),
                    WALLET,
                    "UNI",
                    None,
                    unit_sale_usd=12.0,
                )
                assert result.treatment is TransferTreatment.TRADING_OUT_REALIZING


class TestUnitSaleUsdDefault:
    """The parameter is keyword-only and defaults to None.

    Every call site written before ADR 0014 omits it, so the default is what
    keeps their meaning — a caller that does not know about realizing OUTs
    cannot accidentally supply a sale price for one.
    """

    def test_omitting_it_leaves_a_pool_out_unpriced(self):
        result = classify_transfer(transfer(WALLET, UNI_WETH_ETH), WALLET, "UNI", None)
        assert result.treatment is TransferTreatment.TRADING_OUT_REALIZING
        assert result.unit_sale_usd is None

    def test_omitting_it_is_harmless_for_every_other_treatment(self):
        for tx in (
            transfer(OTHER, WALLET),
            transfer(WALLET, OTHER),
            transfer(WALLET, WALLET),
            transfer(OTHER, THIRD),
        ):
            assert classify_transfer(tx, WALLET, "UNI", 8.0).unit_sale_usd is None

    def test_cannot_be_passed_positionally(self):
        """Keyword-only, so it cannot be mistaken for unit_cost_usd at a call site."""
        with pytest.raises(TypeError):
            classify_transfer(transfer(WALLET, UNI_WETH_ETH), WALLET, "UNI", None, 12.0)
