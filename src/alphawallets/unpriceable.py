"""Tokens no configured price route can serve.

A token here is not missing a price because a backfill has not run. It is
missing one because the endpoint AW_03 fetches with does not carry the token at
all, so no amount of re-running will produce a row. That is a different fact
from "not fetched yet", and the pipeline classifies it separately —
`unpriceable` rather than `pending` — so an operator is not told to re-run a
fetcher that cannot help.

Why a static set. The authoritative answer belongs in the `token_price_route`
table designed in ADR 0010: a cached per-(chain, token) probe that discovers
route coverage at run time, because the gap is a property of the provider at a
point in time rather than of our token set. That table does not exist yet. Until
it does, a hand-maintained set keeps the classification honest without
pretending to discovery it cannot do, and `is_unpriceable` is the single call
site that has to change when the table lands.

The known entry is MKR on Ethereum. DefiLlama serves it from
`/prices/current` ($2032.3869) and `/prices/historical` ($1939.6375) but returns
no coin at all from `/chart`, at every span tried — plausibly a consequence of
the MakerDAO MKR-to-SKY migration. Measured across all 18 verified
(token, chain) pairs, `/chart` covers 17. See the ADR 0008 amendment
"DefiLlama coverage is endpoint-specific" for the probe and the method.

When to add an entry. Only after confirming the token is absent from the route
the fetcher actually uses, on the endpoint it actually calls — not from a sibling
endpoint that is easier to probe, which is the mistake the ADR 0008 amendment
records. An entry here suppresses a real signal, so a wrong one hides a genuine
backfill gap: if AW_03 fails on a token, establish *why* before listing it.

Removing an entry needs the same care in reverse. A token that gains `/chart`
coverage should come out of this set, and nothing here notices that on its own —
another reason the route cache supersedes this file rather than extending it.
"""

from __future__ import annotations

from alphawallets.config import Chain

# (chain, token_address) pairs with no working price route. Addresses lowercase,
# matching how the cache stores them and how every query compares them.
KNOWN_UNPRICEABLE_TOKENS: frozenset[tuple[Chain, str]] = frozenset(
    {
        # MKR on Ethereum — absent from DefiLlama /chart at every span.
        ("ethereum", "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2"),
    }
)


def is_unpriceable(chain: Chain, token_address: str) -> bool:
    """Return True when no configured price route can serve this token.

    Args:
        chain: Chain the token lives on.
        token_address: Token contract, any case.

    Returns:
        True if the (chain, token) pair is known to have no working price route.
        False for everything else, including tokens that are merely not
        backfilled yet — those are 'pending' or 'unavailable', not unpriceable.
    """
    return (chain, token_address.strip().lower()) in KNOWN_UNPRICEABLE_TOKENS
