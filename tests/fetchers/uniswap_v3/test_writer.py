"""Tests for the Uniswap V3 DuckDB writer."""

from datetime import UTC, datetime

import pytest

from alphawallets.db import connect
from alphawallets.fetchers.uniswap_v3.models import RawSwapLog, UniswapV3Swap
from alphawallets.fetchers.uniswap_v3.writer import (
    create_tables,
    write_decoded_swaps,
    write_raw_logs,
)

# ---------- Fixtures ----------


@pytest.fixture
def conn():
    """In-memory DuckDB connection, tables created."""
    with connect(":memory:") as c:
        create_tables(c)
        yield c


@pytest.fixture
def raw_log() -> RawSwapLog:
    return RawSwapLog(
        chain="ethereum",
        block_number=26066990,
        block_hash="0x" + "a" * 64,
        tx_hash="0x" + "b" * 64,
        log_index=73,
        transaction_index=49,
        address="0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
        topics=["0x" + "c" * 64, "0x" + "d" * 64, "0x" + "e" * 64],
        data="0x" + "f" * 320,
        removed=False,
    )


@pytest.fixture
def decoded_swap() -> UniswapV3Swap:
    return UniswapV3Swap(
        chain="ethereum",
        block_number=26066990,
        block_timestamp=datetime(2026, 9, 27, 6, 25, 47, tzinfo=UTC),
        tx_hash="0x" + "b" * 64,
        log_index=73,
        pool_address="0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
        tx_from="0x" + "9" * 40,
        sender="0x" + "1" * 40,
        recipient="0x" + "2" * 40,
        amount0=-55_923_401,
        amount1=20_651_704_797_864_673,
        sqrt_price_x96=1_522_131_675_505_538_340_261_346_218_082_348,
        liquidity=3_004_629_811_743_908_228,
        tick=197275,
    )


# ---------- Table creation ----------


class TestCreateTables:
    def test_both_tables_created(self, conn):
        result = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
        names = [row[0] for row in result]
        assert "raw_uniswap_v3_swap" in names
        assert "uniswap_v3_swap" in names

    def test_idempotent(self, conn):
        # Second call must not fail
        create_tables(conn)
        create_tables(conn)


# ---------- Raw log writes ----------


class TestWriteRawLogs:
    def test_empty_list_returns_zero(self, conn):
        assert write_raw_logs(conn, []) == 0

    def test_single_insert(self, conn, raw_log):
        assert write_raw_logs(conn, [raw_log]) == 1
        rows = conn.execute("SELECT COUNT(*) FROM raw_uniswap_v3_swap").fetchone()
        assert rows[0] == 1

    def test_idempotent_reinsert(self, conn, raw_log):
        assert write_raw_logs(conn, [raw_log]) == 1
        # Second write of the same log inserts nothing
        assert write_raw_logs(conn, [raw_log]) == 0

    def test_different_chains_do_not_collide(self, conn, raw_log):
        eth_log = raw_log
        base_log = raw_log.model_copy(update={"chain": "base"})
        write_raw_logs(conn, [eth_log, base_log])
        rows = conn.execute("SELECT COUNT(*) FROM raw_uniswap_v3_swap").fetchone()
        assert rows[0] == 2

    def test_reorged_variant_coexists(self, conn, raw_log):
        # Same log_index and chain, different block_hash — a reorg case
        original = raw_log
        reorged = raw_log.model_copy(update={"block_hash": "0x" + "9" * 64})
        write_raw_logs(conn, [original, reorged])
        rows = conn.execute("SELECT COUNT(*) FROM raw_uniswap_v3_swap").fetchone()
        assert rows[0] == 2

    def test_topics_stored_as_array(self, conn, raw_log):
        write_raw_logs(conn, [raw_log])
        result = conn.execute("SELECT topics FROM raw_uniswap_v3_swap").fetchone()
        assert isinstance(result[0], list)
        assert len(result[0]) == 3

    def test_removed_flag_persisted(self, conn, raw_log):
        removed_log = raw_log.model_copy(update={"removed": True, "block_hash": "0x" + "0" * 64})
        write_raw_logs(conn, [raw_log, removed_log])
        result = conn.execute(
            "SELECT block_hash, removed FROM raw_uniswap_v3_swap ORDER BY block_hash"
        ).fetchall()
        # Two rows with different removed flags
        removed_values = {row[1] for row in result}
        assert True in removed_values
        assert False in removed_values


# ---------- Decoded swap writes ----------


class TestWriteDecodedSwaps:
    def test_empty_list_returns_zero(self, conn):
        assert write_decoded_swaps(conn, []) == 0

    def test_single_insert(self, conn, decoded_swap):
        assert write_decoded_swaps(conn, [decoded_swap]) == 1

    def test_idempotent_reinsert(self, conn, decoded_swap):
        assert write_decoded_swaps(conn, [decoded_swap]) == 1
        assert write_decoded_swaps(conn, [decoded_swap]) == 0

    def test_big_integers_stored_as_strings(self, conn, decoded_swap):
        write_decoded_swaps(conn, [decoded_swap])
        result = conn.execute(
            "SELECT amount0, amount1, sqrt_price_x96, liquidity FROM uniswap_v3_swap"
        ).fetchone()
        # All four are strings, all round-trip to the original int
        for stored, original in zip(
            result,
            (
                decoded_swap.amount0,
                decoded_swap.amount1,
                decoded_swap.sqrt_price_x96,
                decoded_swap.liquidity,
            ),
            strict=True,
        ):
            assert isinstance(stored, str)
            assert int(stored) == original

    def test_timestamp_roundtrip_utc(self, conn, decoded_swap):
        write_decoded_swaps(conn, [decoded_swap])
        result = conn.execute("SELECT block_timestamp FROM uniswap_v3_swap").fetchone()
        stored = result[0]
        assert stored == decoded_swap.block_timestamp

    def test_negative_amount0_preserved(self, conn, decoded_swap):
        write_decoded_swaps(conn, [decoded_swap])
        result = conn.execute("SELECT amount0 FROM uniswap_v3_swap").fetchone()
        assert int(result[0]) < 0

    def test_batch_insert(self, conn, decoded_swap):
        swaps = [decoded_swap.model_copy(update={"log_index": i}) for i in range(50)]
        assert write_decoded_swaps(conn, swaps) == 50
