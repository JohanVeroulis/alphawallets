"""How each ERC-20 transfer enters the FIFO engine (ADR 0012 step 3).

This is where decisions 2, 3 and 4 are enforced. Every transfer the calculator
sees against a tracked wallet has to be classified before it reaches the engine,
because the same event shape — one ERC-20 `Transfer` — means three different
things depending on direction and counterparty:

- **Transfer-IN from a confirmed airdrop distributor** is free income. Zero cost
  basis, airdrop source (decision 4), so selling it later realizes the full
  proceeds as airdrop PnL.
- **Transfer-IN from anything else** is a purchase we cannot see the other half
  of — most often a CEX withdrawal. Cost basis is price-at-receipt (decision 2).
  Treating it as free would inflate PnL for the commonest wallet shape there is.
- **Transfer-OUT to a known Uniswap V3 pool** is a sale at a knowable price
  (ADR 0014). A V3 swap emits two ERC-20 transfers, and this is the input leg —
  the counterparty is known, so the Realizations are kept rather than discarded.
  Without this, every unit of realized trading PnL from on-chain swaps is thrown
  away, which is the signal ADR 0012 calls "the skill signal".
- **Transfer-OUT anywhere else** reduces the balance without realizing anything
  (decision 3). We do not know whether the wallet sold on a CEX, paid a
  counterparty, bridged, or moved to its own cold storage, so inventing a sale
  price would be a fabrication and ignoring the event would leave a phantom
  balance. The engine's `consume()` is called and its Realizations are discarded.

Pure Python: no DB, no network, no price lookups. The caller resolves
price-at-receipt from the price table and passes it in — including passing None
when no price was available, which the calculator surfaces as
`has_unpriceable_events` rather than letting this layer invent a cost.

ARB needs no special case here. Its registry entry is UNAVAILABLE_BY_DESIGN, so
`is_airdrop_distributor` returns False for every address and bridged ARB
classifies as TRADING_IN with price-at-receipt — exactly what ADR 0012 decision 4
says should happen. The registry's three-state design already expresses it; a
branch on the symbol would be duplicating a decision that is already encoded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from alphawallets.fetchers.erc20.models import ERC20Transfer
from alphawallets.pipeline.pnl.airdrop_registry import is_airdrop_distributor
from alphawallets.pipeline.pnl.known_pools import is_known_pool
from alphawallets.pipeline.pnl.models import LotSource

logger = logging.getLogger(__name__)


class TransferTreatment(Enum):
    """What the calculator should do with a transfer."""

    AIRDROP_IN = "airdrop_in"
    """add_lot() at zero cost, source='airdrop'. ADR 0012 decision 4."""

    TRADING_IN = "trading_in"
    """add_lot() at price-at-receipt, source='trading'. ADR 0012 decision 2."""

    OUT = "out"
    """consume() and discard the Realizations. ADR 0012 decision 3 — the
    counterparty is unknown, so no sale can be implied."""

    TRADING_OUT_REALIZING = "trading_out_realizing"
    """consume() and KEEP the Realizations, priced at unit_sale_usd. ADR 0014:
    the destination is a known Uniswap V3 pool, so the counterparty is known and
    the transfer is unambiguously the input leg of a swap."""

    SELF_TRANSFER = "self_transfer"
    """Treated as TRADING_IN, plus a flag. ADR 0012 decision 2's second
    exception: a wallet's own transfers should preserve cost basis rather than
    re-base at the transfer price, but V1 cannot detect that two addresses
    belong to one person. Deferred to V1.5 address clustering."""

    IGNORED = "ignored"
    """The wallet is neither sender nor receiver. Defensive only — the calculator
    pre-filters, so this should never occur in practice."""


@dataclass(frozen=True)
class TransferClassification:
    """The classifier's verdict on one transfer.

    Frozen: a classification is a reading of an event that already happened, and
    a caller rewriting one would be changing what the data says.
    """

    treatment: TransferTreatment

    unit_cost_usd: float | None
    """Cost basis per whole token, for the IN treatments. None for OUT and
    IGNORED, which take no cost basis.

    Also None when the caller passed None for a TRADING_IN — the price lookup
    failed, because the token is unpriceable or the event fell outside the price
    grid. The classifier records the absence rather than substituting a number;
    the calculator flags the row as has_unpriceable_events. Making up a cost here
    would produce exactly the plausible-but-wrong figure ADR 0012 exists to
    prevent."""

    unit_sale_usd: float | None
    """Sale price per whole token, for TRADING_OUT_REALIZING only. None for every
    other treatment, including plain OUT — decision 3 realizes nothing, so a sale
    price there would imply a trade that may not have happened.

    Also None when the caller passed None for a realizing OUT: the price lookup
    failed, and the calculator flags the row rather than pricing the sale at a
    guess. An invented sale price is worse than an unpriced one, because it lands
    in realized PnL and is indistinguishable from a measured figure."""

    source: LotSource | None
    """The source to pass to engine.add_lot(). None for both OUT treatments and
    for IGNORED, which create no lot. A realizing OUT *consumes* lots, and each
    Realization carries the source of the lot it consumed, so the treatment
    itself does not carry one."""

    is_self_transfer: bool
    """True only for SELF_TRANSFER. Recorded for the calculator to propagate; no
    WalletPnL column exists for it yet, and this PR deliberately does not invent
    one."""


def classify_transfer(
    transfer: ERC20Transfer,
    wallet: str,
    token_symbol: str,
    unit_cost_usd: float | None,
    *,
    unit_sale_usd: float | None = None,
) -> TransferClassification:
    """Decide how one transfer should enter the FIFO engine.

    Args:
        transfer: The decoded transfer. Its addresses are already lowercase —
            ERC20Transfer's validator guarantees it.
        wallet: The EOA whose PnL is being computed. Lowercased here, matching
            FIFOEngine's constructor, so a caller cannot produce a spurious
            IGNORED by passing a checksummed address.
        token_symbol: V1 registry symbol ('UNI', 'ARB', …) for the airdrop
            registry lookup, which keys on symbol and chain. Passed in rather
            than reverse-looked-up from the address: the calculator already knows
            it, and a reverse lookup here would add a dependency on the token
            registry for no gain.
        unit_cost_usd: Price-at-receipt per whole token, resolved by the caller.
            May be None when no price was available. Ignored for AIRDROP_IN and
            both OUT treatments.
        unit_sale_usd: Price-at-sale per whole token, resolved by the caller from
            the same price cache. Used only when the destination is a known V3
            pool. Keyword-only and defaulting to None so every existing call site
            keeps its meaning: a caller that does not know about realizing OUTs
            cannot accidentally supply a sale price for one.

    Returns:
        A TransferClassification. Never raises on an unexpected wallet — see
        IGNORED.
    """
    wallet = wallet.strip().lower()
    is_sender = transfer.from_addr == wallet
    is_receiver = transfer.to_addr == wallet

    # Checked first: a self-transfer satisfies both sides, so testing either one
    # in isolation would classify it as a plain IN or OUT and lose the flag.
    if is_sender and is_receiver:
        return TransferClassification(
            treatment=TransferTreatment.SELF_TRANSFER,
            unit_cost_usd=unit_cost_usd,
            unit_sale_usd=None,
            source="trading",
            is_self_transfer=True,
        )

    if is_receiver:
        if is_airdrop_distributor(transfer.from_addr, token_symbol, transfer.chain):
            return TransferClassification(
                treatment=TransferTreatment.AIRDROP_IN,
                # Forced to zero, not taken from the caller. An airdrop lot is
                # zero-cost by definition (CostBasisLot's contract), so a
                # non-None cost here means the caller resolved a price it should
                # not have — honouring it would book a cost that was never paid.
                unit_cost_usd=0.0,
                unit_sale_usd=None,
                source="airdrop",
                is_self_transfer=False,
            )
        return TransferClassification(
            treatment=TransferTreatment.TRADING_IN,
            unit_cost_usd=unit_cost_usd,
            unit_sale_usd=None,
            source="trading",
            is_self_transfer=False,
        )

    if is_sender:
        if is_known_pool(transfer.chain, transfer.to_addr):
            # ADR 0014 narrows decision 3: the destination is a pool AW_01
            # fetches, so this is the input leg of a swap and the sale price is
            # the outgoing token's market price at this timestamp. Keeping the
            # Realizations here is the difference between a leaderboard that
            # ranks trading skill and one that ranks nothing.
            return TransferClassification(
                treatment=TransferTreatment.TRADING_OUT_REALIZING,
                unit_cost_usd=None,
                unit_sale_usd=unit_sale_usd,
                source=None,
                is_self_transfer=False,
            )
        return TransferClassification(
            treatment=TransferTreatment.OUT,
            unit_cost_usd=None,
            unit_sale_usd=None,
            source=None,
            is_self_transfer=False,
        )

    # Defensive. Returned rather than raised: the calculator pre-filters
    # transfers to the wallet, so this means a filter bug — and crashing a long
    # batch over a case that does nothing is worse than doing nothing and
    # letting the caller log it. The calculator logs any IGNORED at DEBUG.
    return TransferClassification(
        treatment=TransferTreatment.IGNORED,
        unit_cost_usd=None,
        unit_sale_usd=None,
        source=None,
        is_self_transfer=False,
    )
