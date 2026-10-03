"""V1 token registry: symbol to contract address, per chain.

Hardcoded and verified by hand, not auto-discovered. V1 tracks twelve tokens
(CLAUDE.md Section 2) and the set changes rarely, so a dict beats fetching a
token list at runtime on every count that matters here:

- A remote list is a dependency that can change under us. The Superchain token
  list ships the LEGACY, non-transferable MORPHO as canonical for Ethereum, so a
  registry built from it would carry a token that can neither transfer nor be
  priced. See the ADR 0008 amendment.
- Symbol collisions are real. Any chain lets anyone deploy a token called UNI,
  so resolving a symbol at runtime means trusting a list's curation to decide
  what a wallet's balance means. Here the decision is in version control, with a
  verification record attached.
- The lookup is on the hot path of every fetcher invocation and must not fail
  because an endpoint is down.

Every address below was verified on-chain on 2026-10-03 — Ethereum block
26,110,150, Base block 52,110,584 — via symbol(), decimals() and totalSupply(),
and independently confirmed to return a live DefiLlama price. Addresses are
stored lowercase because that is how the cache stores them and how every query
compares them.

Base coverage is deliberately partial. An address is listed only where a trusted
source (the Superchain token list, or Chainlink's own bridge documentation for
LINK) named it AND the on-chain probe confirmed it. Six tokens have no Base entry
because no such source was found, which means "not verified", not "does not
exist" — Base is also where fake-token risk concentrates, and a wrong address
would silently fetch a different token's transfers. See ADR 0008 for the method.
"""

from __future__ import annotations

from alphawallets.config import Chain

# ---------- Registry ----------

V1_TOKENS: dict[str, dict[Chain, str]] = {
    "UNI": {
        "ethereum": "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984",
        "base": "0xc3de830ea07524a0761646a6a4e4be0e114a3c83",
    },
    "AAVE": {
        "ethereum": "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9",
        "base": "0x63706e401c06ac8513145b7687a14804d17f814b",
    },
    "LDO": {
        "ethereum": "0x5a98fcbea516cf06857215779fd812ca3bef1b32",
    },
    "PENDLE": {
        "ethereum": "0x808507121b80c02388fad14726482e061b8da827",
        "base": "0xa99f6e6785da0f5d6fb42495fe424bce029eeb3e",
    },
    "CRV": {
        "ethereum": "0xd533a949740bb3306d119cc777fa900ba034cd52",
        "base": "0x8ee73c484a26e0a5df2ee2a4960b789967dd0415",
    },
    "ENA": {
        "ethereum": "0x57e114b691db790c35207b2e685d4a43181e6061",
    },
    # NOTE: MKR's symbol() returns bytes32, not string — predates the ERC-20
    # metadata standard. Generic code iterating symbol() across V1 tokens must
    # handle this; a standard string ABI raises on decode.
    "MKR": {
        "ethereum": "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2",
    },
    # WARNING: symbol()/decimals() cannot distinguish the two MORPHOs. The
    # transferable version is identified by successful transfer() calls and
    # DefiLlama price coverage. The Superchain token list ships the legacy
    # (non-transferable) address 0x9994e35db50125e0df82e4c2dde62496ce330999.
    # Full method in the ADR 0008 amendment.
    "MORPHO": {
        "ethereum": "0x58d97b57bb95320f9a05dc918aef65434969c2b2",
        "base": "0xbaa5cc21fd487b8fcc2f632f3f4e8d37262a0842",
    },
    "LINK": {
        "ethereum": "0x514910771af9ca656af840dff83e8264ecf986ca",
        # Chainlink-bridged, not a Superchain standard-bridge token, so it does
        # not appear in the Superchain list. Verified on-chain.
        "base": "0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196",
    },
    # ARB is Arbitrum-native; this is the bridged Ethereum form, which ADR 0008
    # measured as priced identically to the native token at every probed date.
    "ARB": {
        "ethereum": "0xb50721bcf8d664c30412cfbc6cf7a15145234ad1",
    },
    "EIGEN": {
        "ethereum": "0xec53bf9167f50cdeb3ae105f56099aaab9061f83",
    },
    "ETHFI": {
        "ethereum": "0xfe0c30065b384f05761f15d0cc899d4f9f9cc0eb",
    },
}

KNOWN_SYMBOLS: frozenset[str] = frozenset(V1_TOKENS)

# Every token V1 tracks has an Ethereum address; Base coverage is partial.
REQUIRED_CHAIN: Chain = "ethereum"


# ---------- Errors ----------


class UnknownTokenError(KeyError):
    """A symbol that is not in the V1 registry.

    A KeyError subclass so `registry[symbol]`-shaped code keeps working, but
    raised with a message naming the valid set — a bare KeyError on a typo'd
    symbol tells the operator nothing about what to type instead.
    """


class TokenNotOnChainError(KeyError):
    """A known symbol with no verified address on the requested chain.

    Distinct from UnknownTokenError on purpose: the symbol is right and the
    chain is the problem, and the two call for different fixes. Raised rather
    than returning None because a silent fallback to the Ethereum address would
    fetch the wrong chain's data under a plausible-looking label.
    """


# ---------- Lookups ----------


def get_token_address(symbol: str, chain: Chain) -> str:
    """Resolve a token symbol to its contract address on a chain.

    Args:
        symbol: Token symbol, case-insensitive ('uni' and 'UNI' both work).
        chain: Target chain.

    Returns:
        The lowercase contract address.

    Raises:
        UnknownTokenError: If the symbol is not in the V1 registry.
        TokenNotOnChainError: If the symbol is known but has no verified address
            on that chain.
    """
    key = symbol.strip().upper()
    if key not in V1_TOKENS:
        raise UnknownTokenError(
            f"Unknown token symbol {symbol!r}. Known V1 symbols: {', '.join(sorted(KNOWN_SYMBOLS))}"
        )

    addresses = V1_TOKENS[key]
    if chain not in addresses:
        raise TokenNotOnChainError(
            f"{key} has no verified address on {chain}. Available for {key}: "
            f"{', '.join(sorted(addresses))}. A Base address is listed only when a "
            f"trusted source named it and an on-chain probe confirmed it — absence "
            f"means unverified, not nonexistent (see src/alphawallets/tokens.py)."
        )
    return addresses[chain]


def chains_for_token(symbol: str) -> list[Chain]:
    """Return the chains a token has a verified address on, sorted.

    Raises:
        UnknownTokenError: If the symbol is not in the V1 registry.
    """
    key = symbol.strip().upper()
    if key not in V1_TOKENS:
        raise UnknownTokenError(
            f"Unknown token symbol {symbol!r}. Known V1 symbols: {', '.join(sorted(KNOWN_SYMBOLS))}"
        )
    return sorted(V1_TOKENS[key])


def all_token_chain_pairs() -> list[tuple[str, Chain]]:
    """Return every verified (symbol, chain) pair, sorted.

    The backfill surface: what a multi-token run iterates over, and what a
    scheduled job will eventually enumerate.
    """
    return sorted((symbol, chain) for symbol, addresses in V1_TOKENS.items() for chain in addresses)
