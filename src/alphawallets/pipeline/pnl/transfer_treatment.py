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
- **Transfer-OUT** reduces the balance without realizing anything (decision 3).
  We do not know whether the wallet sold on a CEX, paid a counterparty, bridged,
  or moved to its own cold storage, so inventing a sale price would be a
  fabrication and ignoring the event would leave a phantom balance. The engine's
  `consume()` is called and its Realizations are discarded by the calculator.

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
from alphawallets.pipeline.pnl.models import LotSource

logger = logging.getLogger(__name__)


class TransferTreatment(Enum):
    """What the calculator should do with a transfer."""

    AIRDROP_IN = "airdrop_in"
    """add_lot() at zero cost, source='airdrop'. ADR 0012 decision 4."""

    TRADING_IN = "trading_in"
    """add_lot() at price-at-receipt, source='trading'. ADR 0012 decision 2."""

    OUT = "out"
    """consume() and discard the Realizations. ADR 0012 decision 3."""

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

    source: LotSource | None
    """The source to pass to engine.add_lot(). None for OUT and IGNORED, which
    do not create a lot."""

    is_self_transfer: bool
    """True only for SELF_TRANSFER. Recorded for the calculator to propagate; no
    WalletPnL column exists for it yet, and this PR deliberately does not invent
    one."""


def classify_transfer(
    transfer: ERC20Transfer,
    wallet: str,
    token_symbol: str,
    unit_cost_usd: float | None,
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
            the OUT treatments.

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
                source="airdrop",
                is_self_transfer=False,
            )
        return TransferClassification(
            treatment=TransferTreatment.TRADING_IN,
            unit_cost_usd=unit_cost_usd,
            source="trading",
            is_self_transfer=False,
        )

    if is_sender:
        return TransferClassification(
            treatment=TransferTreatment.OUT,
            unit_cost_usd=None,
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
        source=None,
        is_self_transfer=False,
    )
