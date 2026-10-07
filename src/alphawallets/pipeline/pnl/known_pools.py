"""Uniswap V3 pools whose address identifies a transfer as a trade.

ADR 0014: a transfer-OUT to a known V3 pool is a realization, because the
counterparty is known and its economic meaning is unambiguous. Every other
transfer-OUT keeps ADR 0012 decision 3's treatment — stack reduced, nothing
realized — since an unknown counterparty cannot imply a sale.

The set is the fetcher's configured pool list, not every V3 pool that exists.
Per ADR 0014, trading in non-primary fee tiers that AW_01 does not fetch
produces no realization — a documented V1 gap.

Two consequences of resolving the set from AW_01's configuration:

- **This is accounting-relevant configuration now.** Adding a pool to AW_01
  changes PnL for any wallet that traded there, so a pool addition is no longer
  a purely additive data decision and the two must move together.
- **It is deliberately static.** Computed once at import from the fetcher dicts
  rather than queried per run, because the same input must produce the same PnL
  on every run. ADR 0014 measured the alternative: `uniswap_v3_swap` is
  populated *by* AW_01 from these same dicts, so expanding the set from the
  table is a closed loop that can never surface a pool we did not configure
  (19 configured, 17 observed, zero observed-but-not-configured).

Keyed on `(chain, address)` rather than address alone. A pool address is only
meaningful on its own chain, and the same hex string on the wrong chain is an
unrelated account — treating it as a pool there would realize a sale that never
happened.
"""

from __future__ import annotations

from alphawallets.config import Chain
from alphawallets.fetchers.uniswap_v3.aw_01_uniswap_v3_swaps import DEFAULT_POOLS_BY_CHAIN

KNOWN_POOLS: frozenset[tuple[Chain, str]] = frozenset(
    (chain, address.lower())  # type: ignore[misc]
    for chain, pools in DEFAULT_POOLS_BY_CHAIN.items()
    for address in pools.values()
)
"""Every configured pool as a (chain, lowercase address) pair.

Lowercased at construction because the fetcher config carries checksummed
addresses for readability while the cache stores lowercase — comparing the two
forms directly would match nothing, which here would silently mean "no trade".
"""


def is_known_pool(chain: Chain, address: str) -> bool:
    """Return True when (chain, address) is a V3 pool AW_01 fetches.

    Args:
        chain: Chain the address lives on.
        address: Contract address, any case.

    Returns:
        True for a configured pool on that chain. False for everything else,
        including a configured pool address on a different chain.
    """
    return (chain, address.strip().lower()) in KNOWN_POOLS
