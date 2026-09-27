"""Configuration loading for AlphaWallets.

Loads environment variables from .env and exposes typed accessors.
Kept minimal for V1 — a full Pydantic Settings class arrives if/when
config surface grows.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

Chain = Literal["ethereum", "base"]

# Alchemy RPC endpoint hosts per chain
_ALCHEMY_HOSTS: dict[Chain, str] = {
    "ethereum": "eth-mainnet.g.alchemy.com",
    "base": "base-mainnet.g.alchemy.com",
}


def _load_env_once() -> None:
    """Load .env from the repo root, idempotent."""
    load_dotenv(_find_repo_root() / ".env", override=False)


def _find_repo_root() -> Path:
    """Walk up from this file until a .env or .git is found."""
    current = Path(__file__).resolve().parent
    for parent in [current, *current.parents]:
        if (parent / ".env").exists() or (parent / ".git").exists():
            return parent
    return current


def get_alchemy_api_key() -> str:
    """Return the Alchemy API key from env.

    Raises RuntimeError if ALCHEMY_API_KEY is not set — fetchers should
    fail loudly at startup rather than mid-fetch.
    """
    _load_env_once()
    key = os.getenv("ALCHEMY_API_KEY")
    if not key:
        raise RuntimeError("ALCHEMY_API_KEY is not set. Copy .env.example to .env and fill it in.")
    return key


def get_alchemy_rpc_url(chain: Chain) -> str:
    """Return the full Alchemy RPC URL for a chain."""
    if chain not in _ALCHEMY_HOSTS:
        raise ValueError(f"Unsupported chain: {chain}. Supported: {list(_ALCHEMY_HOSTS)}")
    return f"https://{_ALCHEMY_HOSTS[chain]}/v2/{get_alchemy_api_key()}"


def get_cache_db_path() -> Path:
    """Return the DuckDB cache path.

    Defaults to data/cache.duckdb under the repo root. Overridable via
    ALPHAWALLETS_CACHE_DB env var (already reserved in .env.example).
    """
    _load_env_once()
    override = os.getenv("ALPHAWALLETS_CACHE_DB")
    if override:
        return Path(override).expanduser().resolve()
    return _find_repo_root() / "data" / "cache.duckdb"
