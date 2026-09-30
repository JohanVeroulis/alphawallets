"""Pure mapping functions for Alchemy Transfers API entries.

No I/O, no RPC — takes a raw `alchemy_getAssetTransfers` result entry and
produces validated models:

    entry (dict) -> RawAssetTransfer -> ERC20Transfer

Both functions return None rather than raising when an entry can't be mapped,
so a single malformed entry doesn't abort a whole backfill page. Skipped
entries are logged at WARNING with their uniqueId so they can be investigated
later.

Field quirks this module absorbs:
- `blockNum` and `rawContract.decimal` are hex strings ("0x18da6b4", "0x12").
- `rawContract.value` is a hex string; stored as a DECIMAL string so DuckDB
  can CAST it and the pipeline can compare magnitudes without re-parsing hex.
- `metadata.blockTimestamp` is ISO 8601 with a trailing "Z".
- `logIndex` is absent from most Transfers API responses; when present it may
  be hex or int.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from alphawallets.config import Chain
from alphawallets.fetchers.erc20.models import ERC20Transfer, RawAssetTransfer

logger = logging.getLogger(__name__)


def _hex_to_int(value: Any) -> int | None:
    """Parse a hex string ('0x18da6b4'), an int, or None.

    Returns None for anything unparseable — callers decide whether the field
    is required.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16)
        except ValueError:
            return None
    return None


def _parse_iso_timestamp(value: Any) -> datetime | None:
    """Parse Alchemy's ISO 8601 blockTimestamp ('2026-09-29T12:34:56.000Z')."""
    if not isinstance(value, str) or not value:
        return None
    try:
        # fromisoformat handles '+00:00' but not the 'Z' suffix before 3.11
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Normalize naive timestamps to UTC; Alchemy always reports UTC
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def to_raw_asset_transfer(entry: dict[str, Any], chain: Chain) -> RawAssetTransfer | None:
    """Map one Transfers API entry to a validated RawAssetTransfer.

    Args:
        entry: A single element of the API's `transfers` array.
        chain: The chain the entry was fetched from.

    Returns:
        A RawAssetTransfer, or None when the entry lacks the fields that make
        it usable (rawContract.value, rawContract.address) or fails model
        validation. Skips are logged at WARNING.
    """
    unique_id = entry.get("uniqueId")
    raw_contract = entry.get("rawContract") or {}
    raw_value = raw_contract.get("value")
    contract_address = raw_contract.get("address")

    if raw_value is None or contract_address is None:
        logger.warning(
            "Skipping transfer %s: missing rawContract.%s",
            unique_id,
            "value" if raw_value is None else "address",
        )
        return None

    value_int = _hex_to_int(raw_value)
    if value_int is None:
        logger.warning(
            "Skipping transfer %s: unparseable rawContract.value %r", unique_id, raw_value
        )
        return None

    block_num = _hex_to_int(entry.get("blockNum"))
    if block_num is None:
        logger.warning(
            "Skipping transfer %s: unparseable blockNum %r", unique_id, entry.get("blockNum")
        )
        return None

    metadata = entry.get("metadata") or {}

    try:
        return RawAssetTransfer(
            chain=chain,
            unique_id=unique_id,
            block_num=block_num,
            tx_hash=entry["hash"],
            from_addr=entry["from"],
            to_addr=entry["to"],
            # Decimal string, not hex: DuckDB can CAST it and the pipeline can
            # compare magnitudes without re-parsing.
            value_raw=str(value_int),
            value_decimal=entry.get("value"),
            asset=entry.get("asset"),
            category=entry.get("category", "erc20"),
            contract_address=contract_address,
            contract_decimal=_hex_to_int(raw_contract.get("decimal")),
            log_index=_hex_to_int(entry.get("logIndex")),
            block_timestamp=_parse_iso_timestamp(metadata.get("blockTimestamp")),
        )
    except (KeyError, ValueError, TypeError) as e:
        logger.warning("Skipping transfer %s: %s", unique_id, e)
        return None


def to_erc20_transfer(raw: RawAssetTransfer) -> ERC20Transfer | None:
    """Normalize a RawAssetTransfer into the pipeline-facing ERC20Transfer.

    Args:
        raw: A validated RawAssetTransfer.

    Returns:
        An ERC20Transfer, or None when the raw record has no block_timestamp.
        The decoded table requires a timestamp because every downstream
        consumer (PnL cost basis, activity windows, price joins) keys on time;
        a row without one cannot be used. The raw record is still persisted,
        so a re-map is possible if Alchemy backfills the metadata.
    """
    if raw.block_timestamp is None:
        logger.warning(
            "Skipping decoded transfer %s: no block_timestamp (raw row is still written)",
            raw.unique_id,
        )
        return None

    return ERC20Transfer(
        chain=raw.chain,
        block_number=raw.block_num,
        block_timestamp=raw.block_timestamp,
        tx_hash=raw.tx_hash,
        log_index=raw.log_index,
        unique_id=raw.unique_id,
        token_address=raw.contract_address,
        from_addr=raw.from_addr,
        to_addr=raw.to_addr,
        value_raw=raw.value_raw,
        token_decimals=raw.contract_decimal,
    )
