"""Tests for the ERC-20 DuckDB writer. In-memory DB, no network."""

from datetime import UTC, datetime

import pytest

from alphawallets.db import connect
from alphawallets.fetchers.erc20.models import ERC20Transfer, RawAssetTransfer
from alphawallets.fetchers.erc20.writer import (
    create_tables,
    write_decoded_transfers,
    write_raw_transfers,
)

UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"


# ---------- Fixtures ----------


@pytest.fixture
def conn():
    """In-memory DuckDB connection with both tables created."""
    with connect(":memory:") as c:
        create_tables(c)
        yield c


@pytest.fixture
def raw() -> RawAssetTransfer:
    return RawAssetTransfer(
        chain="ethereum",
        unique_id="0x" + "a" * 64 + ":log:42",
        block_num=26_060_468,
        tx_hash="0x" + "a" * 64,
        from_addr="0x" + "1" * 40,
        to_addr="0x" + "2" * 40,
        value_raw="100000000000000000000",
        value_decimal=100.0,
        asset="UNI",
        category="erc20",
        contract_address=UNI,
        contract_decimal=18,
        log_index=42,
        block_timestamp=datetime(2026, 9, 29, 12, 34, 56, tzinfo=UTC),
    )


@pytest.fixture
def decoded() -> ERC20Transfer:
    return ERC20Transfer(
        chain="ethereum",
        block_number=26_060_468,
        block_timestamp=datetime(2026, 9, 29, 12, 34, 56, tzinfo=UTC),
        tx_hash="0x" + "a" * 64,
        log_index=42,
        unique_id="0x" + "a" * 64 + ":log:42",
        token_address=UNI,
        from_addr="0x" + "1" * 40,
        to_addr="0x" + "2" * 40,
        value_raw="100000000000000000000",
        token_decimals=18,
    )


# ---------- Table creation ----------


class TestCreateTables:
    def test_both_tables_created(self, conn):
        names = [
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' ORDER BY table_name"
            ).fetchall()
        ]
        assert "raw_erc20_transfer" in names
        assert "erc20_transfer" in names

    def test_idempotent(self, conn):
        create_tables(conn)
        create_tables(conn)

    def test_decoded_pk_is_chain_unique_id(self, conn):
        cols = conn.execute(
            "SELECT constraint_column_names FROM duckdb_constraints() "
            "WHERE table_name = 'erc20_transfer' AND constraint_type = 'PRIMARY KEY'"
        ).fetchone()
        assert cols is not None
        assert list(cols[0]) == ["chain", "unique_id"]

    def test_raw_pk_is_chain_unique_id(self, conn):
        cols = conn.execute(
            "SELECT constraint_column_names FROM duckdb_constraints() "
            "WHERE table_name = 'raw_erc20_transfer' AND constraint_type = 'PRIMARY KEY'"
        ).fetchone()
        assert cols is not None
        assert list(cols[0]) == ["chain", "unique_id"]


# ---------- Raw writes ----------


class TestWriteRawTransfers:
    def test_empty_list_returns_zero(self, conn):
        assert write_raw_transfers(conn, []) == 0

    def test_single_insert(self, conn, raw):
        assert write_raw_transfers(conn, [raw]) == 1
        assert conn.execute("SELECT COUNT(*) FROM raw_erc20_transfer").fetchone()[0] == 1

    def test_idempotent_reinsert(self, conn, raw):
        assert write_raw_transfers(conn, [raw]) == 1
        assert write_raw_transfers(conn, [raw]) == 0

    def test_overlapping_batches(self, conn, raw):
        """Two pages sharing one entry — the overlap must not duplicate."""
        a = raw
        b = raw.model_copy(update={"unique_id": "uid-b"})
        c = raw.model_copy(update={"unique_id": "uid-c"})

        assert write_raw_transfers(conn, [a, b]) == 2
        # Second batch re-sends b, adds c
        assert write_raw_transfers(conn, [b, c]) == 1
        assert conn.execute("SELECT COUNT(*) FROM raw_erc20_transfer").fetchone()[0] == 3

    def test_different_chains_do_not_collide(self, conn, raw):
        write_raw_transfers(conn, [raw, raw.model_copy(update={"chain": "base"})])
        assert conn.execute("SELECT COUNT(*) FROM raw_erc20_transfer").fetchone()[0] == 2

    def test_null_log_index_persisted(self, conn, raw):
        write_raw_transfers(conn, [raw.model_copy(update={"log_index": None})])
        assert conn.execute("SELECT log_index FROM raw_erc20_transfer").fetchone()[0] is None

    def test_null_timestamp_persisted(self, conn, raw):
        write_raw_transfers(conn, [raw.model_copy(update={"block_timestamp": None})])
        assert conn.execute("SELECT block_timestamp FROM raw_erc20_transfer").fetchone()[0] is None

    def test_multiple_null_log_index_rows_coexist(self, conn, raw):
        """The reason the PK is (chain, unique_id): NULL log_index must not block rows."""
        rows = [
            raw.model_copy(update={"unique_id": f"uid-{i}", "log_index": None}) for i in range(5)
        ]
        assert write_raw_transfers(conn, rows) == 5

    def test_value_raw_round_trips_as_string(self, conn, raw):
        big = str(2**200)  # far beyond HUGEINT
        write_raw_transfers(conn, [raw.model_copy(update={"value_raw": big})])
        stored = conn.execute("SELECT value_raw FROM raw_erc20_transfer").fetchone()[0]
        assert isinstance(stored, str)
        assert int(stored) == 2**200


# ---------- Decoded writes ----------


class TestWriteDecodedTransfers:
    def test_empty_list_returns_zero(self, conn):
        assert write_decoded_transfers(conn, []) == 0

    def test_single_insert(self, conn, decoded):
        assert write_decoded_transfers(conn, [decoded]) == 1

    def test_idempotent_reinsert(self, conn, decoded):
        assert write_decoded_transfers(conn, [decoded]) == 1
        assert write_decoded_transfers(conn, [decoded]) == 0

    def test_overlapping_batches(self, conn, decoded):
        a = decoded
        b = decoded.model_copy(update={"unique_id": "uid-b"})
        c = decoded.model_copy(update={"unique_id": "uid-c"})

        assert write_decoded_transfers(conn, [a, b]) == 2
        assert write_decoded_transfers(conn, [b, c]) == 1
        assert conn.execute("SELECT COUNT(*) FROM erc20_transfer").fetchone()[0] == 3

    def test_null_log_index_allowed(self, conn, decoded):
        assert write_decoded_transfers(conn, [decoded.model_copy(update={"log_index": None})]) == 1
        assert conn.execute("SELECT log_index FROM erc20_transfer").fetchone()[0] is None

    def test_timestamp_round_trips(self, conn, decoded):
        write_decoded_transfers(conn, [decoded])
        stored = conn.execute("SELECT block_timestamp FROM erc20_transfer").fetchone()[0]
        assert stored == decoded.block_timestamp

    def test_value_raw_castable_to_decimal(self, conn, decoded):
        """The point of storing a decimal string: SQL can compare magnitudes."""
        write_decoded_transfers(conn, [decoded])
        result = conn.execute(
            "SELECT CAST(value_raw AS DECIMAL(38, 0)) FROM erc20_transfer"
        ).fetchone()
        assert int(result[0]) == 100 * 10**18

    def test_batch_insert(self, conn, decoded):
        rows = [decoded.model_copy(update={"unique_id": f"uid-{i}"}) for i in range(50)]
        assert write_decoded_transfers(conn, rows) == 50

    def test_raw_and_decoded_are_independent(self, conn, raw, decoded):
        """Same unique_id in both tables is expected, not a collision."""
        write_raw_transfers(conn, [raw])
        write_decoded_transfers(conn, [decoded])
        assert conn.execute("SELECT COUNT(*) FROM raw_erc20_transfer").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM erc20_transfer").fetchone()[0] == 1
