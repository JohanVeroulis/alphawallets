"""Tests for the wallet-universe seed registry (ADR 0017)."""

import json

import pytest

from alphawallets.seeds import (
    SEED_FILE,
    SeedRegistryError,
    is_seeded,
    load_seed_registry,
)

CHECKSUMMED = "0x6420A51576732468eFf4dAffdb168F573cd2b2df"
LOWER = CHECKSUMMED.lower()
OTHER = "0x" + "a" * 40


def _write(tmp_path, entries):
    path = tmp_path / "seeds.json"
    path.write_text(json.dumps(entries))
    load_seed_registry.cache_clear()
    return path


def _entry(**over):
    base = {
        "address": CHECKSUMMED,
        "chain": "ethereum",
        "source": "test-fixture",
        "label": "a wallet",
        "snapshot_date": "2026-10-11",
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _clear_cache():
    """The loader is lru_cached, so fixtures pointing at different files would
    otherwise see the first one's result."""
    load_seed_registry.cache_clear()
    yield
    load_seed_registry.cache_clear()


class TestLoader:
    def test_lowercases_on_load(self, tmp_path):
        """Public label sources publish checksummed addresses; the cache stores
        lowercase. Comparing the two forms directly matches nothing, and
        "matches nothing" here reads as a legitimate exclusion rather than as a
        bug — the same case-drift trap KNOWN_POOLS already guards against."""
        path = _write(tmp_path, [_entry()])
        assert load_seed_registry(path) == frozenset({("ethereum", LOWER)})
        assert is_seeded("ethereum", CHECKSUMMED, path)
        assert is_seeded("ethereum", LOWER, path)

    def test_address_is_chain_scoped(self, tmp_path):
        path = _write(tmp_path, [_entry()])
        assert is_seeded("ethereum", CHECKSUMMED, path)
        assert not is_seeded("base", CHECKSUMMED, path)

    def test_unseeded_address_is_false(self, tmp_path):
        path = _write(tmp_path, [_entry()])
        assert not is_seeded("ethereum", OTHER, path)

    @pytest.mark.parametrize("field", ["address", "chain", "source", "label", "snapshot_date"])
    def test_missing_provenance_raises(self, tmp_path, field):
        """Raised, not skipped. A dropped entry would shrink the ranked universe
        invisibly, and no output could reveal it."""
        path = _write(tmp_path, [_entry(**{field: ""})])
        with pytest.raises(SeedRegistryError, match=field):
            load_seed_registry(path)

    def test_malformed_address_raises(self, tmp_path):
        path = _write(tmp_path, [_entry(address="0xtooshort")])
        with pytest.raises(SeedRegistryError, match="42-character"):
            load_seed_registry(path)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(SeedRegistryError, match="not found"):
            load_seed_registry(tmp_path / "absent.json")

    def test_non_list_raises(self, tmp_path):
        path = tmp_path / "seeds.json"
        path.write_text('{"address": "0x1"}')
        with pytest.raises(SeedRegistryError, match="JSON list"):
            load_seed_registry(path)

    def test_invalid_json_raises(self, tmp_path):
        path = tmp_path / "seeds.json"
        path.write_text("{not json")
        with pytest.raises(SeedRegistryError, match="not valid JSON"):
            load_seed_registry(path)


class TestCommittedRegistry:
    """The shipped file is placeholder data, but it must still be loadable and
    honestly labelled — a registry whose provenance cannot be trusted is worse
    than no registry (see seeds/README.md)."""

    def test_committed_file_loads(self):
        registry = load_seed_registry()
        assert registry, "the committed registry should not be empty"
        assert all(addr == addr.lower() for _, addr in registry)

    def test_every_entry_is_marked_placeholder(self):
        entries = json.loads(SEED_FILE.read_text())
        assert all(e["source"].startswith("placeholder-") for e in entries), (
            "Bootstrap entries must be greppable as placeholders so the real "
            "curation can replace them without guessing which is which."
        )

    def test_chains_are_in_v1_scope(self):
        entries = json.loads(SEED_FILE.read_text())
        assert {e["chain"] for e in entries} <= {"ethereum", "base"}
