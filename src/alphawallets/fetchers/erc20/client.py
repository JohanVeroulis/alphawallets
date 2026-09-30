"""Alchemy Transfers API client for ERC-20 transfers.

Wraps `alchemy_getAssetTransfers`, which returns pre-decoded ERC-20 transfers
without the 10-block `eth_getLogs` window cap (ADR 0006). One call per page;
the caller loops on the returned page key.

The request goes through the Web3 instance's provider rather than a fresh HTTP
client, so it inherits `_RedactingHTTPProvider` — any exception has the API key
scrubbed before it reaches a log (see alchemy_client.py).
"""

from __future__ import annotations

import logging
from typing import Any

from web3 import Web3

logger = logging.getLogger(__name__)

# Alchemy caps a single Transfers API page at 1000 entries.
MAX_COUNT_PER_PAGE = "0x3e8"

RPC_METHOD = "alchemy_getAssetTransfers"


def fetch_asset_transfers(
    w3: Web3,
    contract_address: str,
    from_block: int,
    to_block: int,
    page_key: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch one page of ERC-20 transfers for a token contract.

    Args:
        w3: Live Web3 instance from make_web3(); its provider carries the
            Alchemy endpoint and API-key redaction.
        contract_address: Token contract to filter on (0x-prefixed).
        from_block: Start block, inclusive.
        to_block: End block, inclusive.
        page_key: Continuation token from a previous call. None for the
            first page.

    Returns:
        A (transfers, next_page_key) tuple. next_page_key is None when the
        response is the last page, which is the caller's loop-exit signal.

    Raises:
        ValueError: If from_block > to_block, or the RPC returns an error.
    """
    if from_block > to_block:
        raise ValueError(f"from_block ({from_block}) > to_block ({to_block})")

    params: dict[str, Any] = {
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
        "category": ["erc20"],
        "contractAddresses": [contract_address],
        "withMetadata": True,
        "excludeZeroValue": True,
        "maxCount": MAX_COUNT_PER_PAGE,
    }
    if page_key is not None:
        params["pageKey"] = page_key

    response = w3.provider.make_request(RPC_METHOD, [params])

    if "error" in response:
        # Surfaced as ValueError rather than a bare dict so callers get a
        # stack trace pointing at the failing block range.
        raise ValueError(
            f"{RPC_METHOD} failed for blocks {from_block}-{to_block}: {response['error']}"
        )

    result = response.get("result") or {}
    transfers: list[dict[str, Any]] = result.get("transfers", [])
    next_page_key: str | None = result.get("pageKey")

    logger.info(
        "Fetched %d ERC-20 transfers for %s, blocks %d-%d (page_key=%s, next=%s)",
        len(transfers),
        contract_address,
        from_block,
        to_block,
        "yes" if page_key else "no",
        "yes" if next_page_key else "no",
    )
    return transfers, next_page_key
