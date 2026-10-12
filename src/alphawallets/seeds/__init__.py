"""Wallet-universe seed registry (ADR 0017).

V1 does not rank every wallet it has indexed. It ranks a curated universe, and
this module is where that universe is defined: a JSON file committed next to the
code, loaded once, consulted as an eligibility predicate.

**This is a filter, not a fetcher argument.** ADR 0017 fixes the architecture:
AW_01 and AW_02 stay universe-agnostic and the cache remains a complete record,
so a non-seeded wallet's PnL is still computed and still written — it is simply
not ranked. Three reasons that matters:

- Provider cost is unchanged. A seed list cannot reduce `eth_getLogs` cost at
  all (`tx_from` is not a log topic) and would multiply `getAssetTransfers`
  calls by the address count, since its filter is one address per call.
- The V1.5 custody work needs the transfer legs of *non-seeded* contracts, so a
  fetcher that skipped them would have to re-fetch.
- Refreshing the universe becomes a recompute rather than a backfill, which
  makes a bad snapshot revertible.

The registry is committed rather than fetched at runtime because the universe is
an input to every figure the project produces. A leaderboard whose population
changes silently between runs cannot be compared with itself.

Example:
    >>> registry = load_seed_registry()
    >>> is_seeded("ethereum", "0x33EeCc48943aAEaBb5328A25fF28eb85f67945c2")
    True
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from alphawallets.config import Chain

logger = logging.getLogger(__name__)

SEED_FILE = Path(__file__).parent / "smart_money.json"

REQUIRED_FIELDS = ("address", "chain", "source", "label", "snapshot_date")

SeedKey = tuple[str, str]
"""(chain, lowercase address). Keyed on both because an address is only
meaningful on its own chain — the same hex string elsewhere is an unrelated
account, and seeding it there would rank a wallet that does not exist."""


class SeedRegistryError(RuntimeError):
    """The seed file is missing, malformed, or has an entry we cannot trust.

    Raised rather than skipped. A seed registry that silently drops a malformed
    entry would quietly shrink the ranked universe, and the resulting
    leaderboard would be wrong in a way no output could reveal.
    """


def _validate(entry: dict[str, Any], index: int) -> None:
    """Check one entry, naming the index so a 500-line file is navigable."""
    missing = [f for f in REQUIRED_FIELDS if not entry.get(f)]
    if missing:
        raise SeedRegistryError(
            f"{SEED_FILE.name} entry {index} is missing {', '.join(missing)}. "
            "Every seed carries its provenance (ADR 0017) so a stale snapshot is "
            "visible rather than inferred."
        )

    address = entry["address"]
    if not isinstance(address, str) or not address.startswith("0x") or len(address) != 42:
        raise SeedRegistryError(
            f"{SEED_FILE.name} entry {index} has address {address!r}, which is not "
            "a 0x-prefixed 42-character address."
        )


@lru_cache(maxsize=1)
def load_seed_registry(path: Path | None = None) -> frozenset[SeedKey]:
    """Load the seed registry as (chain, lowercase address) pairs.

    Args:
        path: Override the registry location. Defaults to the committed
            `smart_money.json`. Tests pass their own fixture.

    Returns:
        Every seeded (chain, address) pair, addresses lowercased.

    Raises:
        SeedRegistryError: If the file is absent, is not a JSON list, or holds an
            entry without full provenance.

    Notes:
        Addresses are lowercased on load, not at every comparison site. Public
        label sources publish checksummed addresses while the cache stores
        lowercase, so comparing the two forms directly matches nothing — and
        "matches nothing" here means "this wallet is not seeded", which reads as
        a legitimate exclusion rather than as a bug. The same case-drift trap
        `KNOWN_POOLS` already guards against.

        Cached, because the file is read once per process and the result is
        immutable. `load_seed_registry.cache_clear()` resets it, which tests
        need when they point `path` at different fixtures.
    """
    target = path or SEED_FILE
    if not target.exists():
        raise SeedRegistryError(
            f"Seed registry not found at {target}. ADR 0017 makes the wallet "
            "universe an explicit input; running without one would silently rank "
            "every wallet in the cache."
        )

    try:
        raw = json.loads(target.read_text())
    except json.JSONDecodeError as e:
        raise SeedRegistryError(f"{target} is not valid JSON: {e}") from e

    if not isinstance(raw, list):
        raise SeedRegistryError(
            f"{target} must hold a JSON list of entries, got {type(raw).__name__}"
        )

    registry: set[SeedKey] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise SeedRegistryError(f"{target} entry {index} is not an object")
        _validate(entry, index)
        registry.add((entry["chain"], entry["address"].strip().lower()))

    by_chain: dict[str, int] = {}
    for chain, _ in registry:
        by_chain[chain] = by_chain.get(chain, 0) + 1
    logger.info(
        "Seed registry: %d address(es) from %d entr(ies); per chain %s",
        len(registry),
        len(raw),
        dict(sorted(by_chain.items())),
    )
    return frozenset(registry)


def is_seeded(chain: Chain, wallet: str, path: Path | None = None) -> bool:
    """Return True when this wallet is in the ranked universe for this chain.

    Args:
        chain: Chain the wallet is being ranked on.
        wallet: Wallet address, any case.
        path: Registry override, for tests.

    Returns:
        True for a seeded (chain, address) pair. False for everything else,
        including a seeded address on a different chain.
    """
    return (chain, wallet.strip().lower()) in load_seed_registry(path)
