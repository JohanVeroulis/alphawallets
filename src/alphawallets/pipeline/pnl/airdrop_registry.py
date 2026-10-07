"""Hand-verified airdrop distribution contracts for V1 tracked tokens.

ADR 0012 decision 4: a transfer **from** a known distribution contract is
tagged as airdrop income and routed into the zero-cost airdrop sub-stack of
the FIFO engine. Any other transfer-IN takes price-at-receipt as cost basis
per decision 2. The classification happens once, at ingestion into the engine,
so an unknown distributor silently reclassifies real airdrops as trading
income — the correctness of this file is load-bearing.

Verification policy mirrors the token registry (src/alphawallets/tokens.py):
a hardcoded dict beats a runtime fetch because the set is small, changes
rarely, and a wrong address would silently pollute the airdrop-vs-trading
split for every wallet that received that drop. Every CONFIRMED entry below
cites a verifiable public source and should be re-checked on a block explorer
before it ships.

Scope notes from ADR 0001, carried through ADR 0012 decision 4:

- **ARB** has no Ethereum distribution contract. The claim happened on
  Arbitrum and V1 tracks ARB only after it bridged to Ethereum. Bridged ARB
  is indistinguishable from any other transfer-IN, so airdrop/trading
  separation is UNAVAILABLE for ARB in V1. Recorded explicitly rather than
  omitted so a reader does not wonder.

- **MORPHO on Base** was flagged by ADR 0001 as a Week 3 check; distribution
  may have happened on Base rather than only on Ethereum. Status is
  UNVERIFIED pending that check; until then, Base MORPHO transfers-IN will
  classify as trading.

- **MKR** has no airdrop in V1 scope — it predates the airdrop era and is
  tracked purely as a DeFi token. No entry.

- **WETH and USDC** are quote assets (tokens.QUOTE_ONLY_SYMBOLS), tracked only
  so swaps can anchor on their price (ADR 0015). They have no airdrop history
  and `get_airdrop_record` returns None for both, which is correct: a
  quote-asset transfer-IN classifies as trading, because receiving WETH from a
  swap *is* a purchase. Reading their absence as a missing entry would be the
  wrong conclusion — there is nothing to add.

The remaining five airdrop tokens (UNI, ENA, EIGEN, MORPHO on Ethereum, ETHFI)
require their distribution contract address(es) to be verified on-chain and
listed below. The scaffolding and types are in place; adding a confirmed
address is a one-line change. Entries land in a follow-up PR after on-chain
verification, tracked as the ADR 0012 implementation plan step 1 completion
item.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from alphawallets.config import Chain

ADDRESS_PATTERN_LEN = 42  # "0x" + 40 hex chars


class AirdropAttributionStatus(Enum):
    """Why airdrop attribution is or isn't available for a (token, chain)."""

    CONFIRMED = "confirmed"
    """One or more distribution contracts verified on-chain and listed below."""

    UNVERIFIED = "unverified"
    """Known-pending check (e.g. MORPHO on Base). Transfers-IN classify as
    trading until this is resolved. Not a bug — a documented hold."""

    UNAVAILABLE_BY_DESIGN = "unavailable_by_design"
    """No on-chain distributor to match (e.g. ARB bridged from Arbitrum).
    V1 cannot attribute these; recorded explicitly so a reader does not
    read the empty set as "forgot to add"."""


@dataclass(frozen=True)
class AirdropRecord:
    """One token's airdrop attribution record on one chain.

    `distributors` holds the contract addresses that originate airdrop
    transfers — multiple when a token's drop ran across several distribution
    contracts (seasons, Merkle redeployments). Empty for every status other
    than CONFIRMED.
    """

    token_symbol: str
    chain: Chain
    status: AirdropAttributionStatus
    distributors: frozenset[str]
    note: str

    def __post_init__(self) -> None:
        """Enforce invariants a mutable dict could never express cleanly."""
        if self.status is AirdropAttributionStatus.CONFIRMED and not self.distributors:
            raise ValueError(
                f"{self.token_symbol} on {self.chain}: CONFIRMED status requires "
                f"at least one distributor address"
            )
        if self.status is not AirdropAttributionStatus.CONFIRMED and self.distributors:
            raise ValueError(
                f"{self.token_symbol} on {self.chain}: distributors are only valid "
                f"with CONFIRMED status, got {self.status.value}"
            )
        for addr in self.distributors:
            is_bad_shape = (
                len(addr) != ADDRESS_PATTERN_LEN
                or not addr.startswith("0x")
                or addr != addr.lower()
            )
            if is_bad_shape:
                raise ValueError(
                    f"{self.token_symbol} on {self.chain}: distributor {addr!r} "
                    f"must be a 42-char lowercase 0x-prefixed hex address"
                )


# ---------- Registry ----------
#
# Keys: (token_symbol, chain). Only tracked airdrop tokens from ADR 0001 appear.
# A token in the V1 registry but NOT in this map (e.g. AAVE, LINK, LDO, PENDLE,
# CRV, MKR) has no airdrop in V1 scope — every transfer-IN classifies as
# trading, which is correct by design.
#
# To promote an UNVERIFIED or placeholder entry to CONFIRMED: verify the
# contract on-chain (etherscan / basescan), confirm its role as the
# distribution contract, and replace the empty frozenset with the lowercase
# address(es). Entries land one PR at a time with a verification note.

_REGISTRY: dict[tuple[str, Chain], AirdropRecord] = {
    ("ARB", "ethereum"): AirdropRecord(
        token_symbol="ARB",
        chain="ethereum",
        status=AirdropAttributionStatus.UNAVAILABLE_BY_DESIGN,
        distributors=frozenset(),
        note=(
            "The ARB claim happened on Arbitrum; V1 tracks ARB only after it "
            "bridged to Ethereum. No Ethereum distributor to match. ADR 0001."
        ),
    ),
    ("MORPHO", "base"): AirdropRecord(
        token_symbol="MORPHO",
        chain="base",
        status=AirdropAttributionStatus.UNVERIFIED,
        distributors=frozenset(),
        note=(
            "ADR 0001 Week 3 check: distribution may have run on Base. Until "
            "verified, Base MORPHO transfers-IN classify as trading."
        ),
    ),
    # ----- Ethereum distributors, UNVERIFIED pending on-chain confirmation -----
    #
    # Each of the entries below needs its distribution contract address verified
    # on-chain before being promoted to CONFIRMED. The scaffolding is here so
    # the engine can call registry lookups without special-casing missing keys;
    # UNVERIFIED routes transfers-IN into the trading sub-stack (the safe
    # default when no address is known) and surfaces the gap when the engine
    # reports its flag distribution.
    ("UNI", "ethereum"): AirdropRecord(
        token_symbol="UNI",
        chain="ethereum",
        status=AirdropAttributionStatus.UNVERIFIED,
        distributors=frozenset(),
        note=(
            "September 2020 Merkle distribution. Address to verify on Etherscan "
            "and promote to CONFIRMED in a follow-up PR."
        ),
    ),
    ("ENA", "ethereum"): AirdropRecord(
        token_symbol="ENA",
        chain="ethereum",
        status=AirdropAttributionStatus.UNVERIFIED,
        distributors=frozenset(),
        note=(
            "April 2024 Season 1 distribution. Address to verify on Etherscan "
            "and promote to CONFIRMED in a follow-up PR."
        ),
    ),
    ("EIGEN", "ethereum"): AirdropRecord(
        token_symbol="EIGEN",
        chain="ethereum",
        status=AirdropAttributionStatus.UNVERIFIED,
        distributors=frozenset(),
        note=(
            "October 2024 Season 1 distribution; may include multiple season "
            "contracts. Addresses to verify on Etherscan and promote to "
            "CONFIRMED in a follow-up PR."
        ),
    ),
    ("MORPHO", "ethereum"): AirdropRecord(
        token_symbol="MORPHO",
        chain="ethereum",
        status=AirdropAttributionStatus.UNVERIFIED,
        distributors=frozenset(),
        note=(
            "November 2024 distribution via Universal Rewards Distributor(s). "
            "Address(es) to verify on Etherscan and promote to CONFIRMED. "
            "Note: this is the TRANSFERABLE MORPHO (see tokens.py)."
        ),
    ),
    ("ETHFI", "ethereum"): AirdropRecord(
        token_symbol="ETHFI",
        chain="ethereum",
        status=AirdropAttributionStatus.UNVERIFIED,
        distributors=frozenset(),
        note=(
            "March 2024 Season 1 distribution. Address to verify on Etherscan "
            "and promote to CONFIRMED in a follow-up PR."
        ),
    ),
}


# ---------- Lookups ----------


def get_airdrop_record(token_symbol: str, chain: Chain) -> AirdropRecord | None:
    """Return the airdrop record for a (token, chain) pair, or None.

    None means "not an airdrop token in V1 scope" — AAVE, LINK, MKR, LDO,
    PENDLE, CRV return None, and every transfer-IN of those tokens classifies
    as trading, which is correct.

    Returning None vs returning an UNVERIFIED record is a deliberate split:
    None says "no airdrop expected for this token", UNVERIFIED says "airdrop
    exists but we haven't confirmed the distributor yet". Both route to the
    same engine behaviour (trade bucket) today, but a report counting
    UNVERIFIED cases is the signal for which TODO to pick up next.
    """
    return _REGISTRY.get((token_symbol.strip().upper(), chain))


def is_airdrop_distributor(from_addr: str, token_symbol: str, chain: Chain) -> bool:
    """Return True when `from_addr` is a confirmed airdrop distributor for the token.

    Case-insensitive on the symbol; expects a lowercase hex address on
    `from_addr` to match the project-wide normalization. A non-lowercase
    input matches nothing (not an exception), because every address the
    engine passes in comes from a model with the lowercase_hex validator —
    seeing an uppercase one here signals an upstream bug the engine should
    surface in tests rather than paper over at lookup time.
    """
    record = get_airdrop_record(token_symbol, chain)
    if record is None or record.status is not AirdropAttributionStatus.CONFIRMED:
        return False
    return from_addr in record.distributors


def airdrop_attribution_available(token_symbol: str, chain: Chain) -> bool:
    """True when the engine can distinguish airdrop income from trading income.

    Used by the PnL calculator to decide whether `realized_pnl_airdrop_usd`
    on a wallet is meaningful for this token or a placeholder zero. A wallet
    that only ever held ARB on Ethereum, for example, has
    `realized_pnl_airdrop_usd = 0.0` not because it earned nothing from
    airdrops but because we cannot attribute. Reports should present the two
    cases distinctly rather than letting them read the same.
    """
    record = get_airdrop_record(token_symbol, chain)
    return record is not None and record.status is AirdropAttributionStatus.CONFIRMED


def all_records() -> list[AirdropRecord]:
    """Every record in the registry, sorted by (symbol, chain).

    The reporting surface for ADR 0012 implementation plan step 6: the live
    engine run prints which tokens have CONFIRMED airdrop attribution and
    which are UNVERIFIED, so the flag distribution is visible alongside PnL.
    """
    return sorted(_REGISTRY.values(), key=lambda r: (r.token_symbol, r.chain))
