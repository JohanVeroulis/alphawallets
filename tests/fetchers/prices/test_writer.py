"""Tests for the token_price DuckDB writer. In-memory DB, no network."""

from datetime import UTC, datetime, timedelta

import pytest

from alphawallets.db import connect
from alphawallets.fetchers.prices.models import TokenPrice
from alphawallets.fetchers.prices.writer import create_tables, write_token_prices

UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
HOUR = datetime(2026, 9, 30, 5, 0, tzinfo=UTC)
FETCHED = datetime(2026, 9, 30, 6, 30, tzinfo=UTC)


# ---------- Fixtures ----------


@pytest.fixture
def conn():
    """In-memory DuckDB with token_price created."""
    with connect(":memory:") as c:
        create_tables(c)
        yield c


@pytest.fixture
def price() -> TokenPrice:
    return TokenPrice(
        chain="ethereum",
        token_address=UNI,
        ts=HOUR,
        price_usd=8.87,
        confidence=0.99,
        source="defillama",
        fetched_at=FETCHED,
    )


# ---------- Table creation ----------


class TestCreateTables:
    def test_table_created(self, conn):
        names = [
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        ]
        assert "token_price" in names

    def test_idempotent(self, conn):
        create_tables(conn)
        create_tables(conn)

    def test_pk_is_four_columns_in_order(self, conn):
        """Pin the PK so a schema edit surfaces loudly rather than silently."""
        row = conn.execute(
            "SELECT constraint_column_names FROM duckdb_constraints() "
            "WHERE table_name = 'token_price' AND constraint_type = 'PRIMARY KEY'"
        ).fetchone()
        assert row is not None
        assert list(row[0]) == ["chain", "token_address", "ts", "source"]

    def test_no_secondary_indexes(self, conn):
        """AW_02's EXPLAIN finding: DuckDB ignores them at V1 scale."""
        indexes = conn.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'token_price'"
        ).fetchall()
        # Only PK-backing structures, no CREATE INDEX of ours
        assert all("idx_" not in row[0] for row in indexes)

    def test_confidence_is_nullable(self, conn, price):
        write_token_prices(conn, [price.model_copy(update={"confidence": None})])
        assert conn.execute("SELECT confidence FROM token_price").fetchone()[0] is None


# ---------- Writes ----------


class TestWriteTokenPrices:
    def test_empty_list_returns_zero(self, conn):
        assert write_token_prices(conn, []) == 0
        assert _count(conn) == 0

    def test_single_insert(self, conn, price):
        assert write_token_prices(conn, [price]) == 1
        assert _count(conn) == 1

    def test_values_round_trip(self, conn, price):
        write_token_prices(conn, [price])
        row = conn.execute(
            "SELECT chain, token_address, ts, price_usd, confidence, source, fetched_at "
            "FROM token_price"
        ).fetchone()
        assert row[0] == "ethereum"
        assert row[1] == UNI
        assert row[2] == HOUR
        assert row[3] == pytest.approx(8.87)
        assert row[4] == pytest.approx(0.99)
        assert row[5] == "defillama"
        assert row[6] == FETCHED

    def test_batch_insert(self, conn, price):
        prices = [price.model_copy(update={"ts": HOUR + timedelta(hours=i)}) for i in range(720)]
        assert write_token_prices(conn, prices) == 720

    def test_idempotent_reinsert(self, conn, price):
        assert write_token_prices(conn, [price]) == 1
        assert write_token_prices(conn, [price]) == 0
        assert _count(conn) == 1

    def test_overlapping_batches(self, conn, price):
        """Two spans sharing hours — the overlap must not duplicate."""
        batch_a = [price.model_copy(update={"ts": HOUR + timedelta(hours=i)}) for i in range(5)]
        batch_b = [price.model_copy(update={"ts": HOUR + timedelta(hours=i)}) for i in range(3, 8)]

        assert write_token_prices(conn, batch_a) == 5
        # batch_a covered hours 0-4; batch_b adds 5, 6, 7 and repeats 3, 4
        assert write_token_prices(conn, batch_b) == 3
        assert _count(conn) == 8

    def test_second_overlapping_batch_adds_nothing_when_fully_contained(self, conn, price):
        batch = [price.model_copy(update={"ts": HOUR + timedelta(hours=i)}) for i in range(5)]
        write_token_prices(conn, batch)
        assert write_token_prices(conn, batch[1:3]) == 0

    def test_different_hours_coexist(self, conn, price):
        write_token_prices(
            conn,
            [price, price.model_copy(update={"ts": HOUR + timedelta(hours=1)})],
        )
        assert _count(conn) == 2

    def test_different_chains_coexist(self, conn, price):
        write_token_prices(conn, [price, price.model_copy(update={"chain": "base"})])
        assert _count(conn) == 2

    def test_different_tokens_coexist(self, conn, price):
        other = "0x" + "b" * 40
        write_token_prices(conn, [price, price.model_copy(update={"token_address": other})])
        assert _count(conn) == 2


# ---------- The same-hour collision the mapper is allowed to produce ----------


class TestSameHourCollision:
    def test_one_inserted_one_skipped(self, conn, price):
        """Two observations inside one hour map to identical ts; PK absorbs the second."""
        first = price.model_copy(update={"price_usd": 8.80})
        second = price.model_copy(update={"price_usd": 8.95})

        assert write_token_prices(conn, [first, second]) == 1
        assert _count(conn) == 1

    def test_first_price_wins(self, conn, price):
        """INSERT OR IGNORE keeps the earlier row — verified, not assumed."""
        first = price.model_copy(update={"price_usd": 8.80})
        second = price.model_copy(update={"price_usd": 8.95})

        write_token_prices(conn, [first, second])
        stored = conn.execute("SELECT price_usd FROM token_price").fetchone()[0]
        assert stored == pytest.approx(8.80), "later same-hour observation must not overwrite"

    def test_first_price_wins_across_separate_calls(self, conn, price):
        write_token_prices(conn, [price.model_copy(update={"price_usd": 8.80})])
        assert write_token_prices(conn, [price.model_copy(update={"price_usd": 8.95})]) == 0
        stored = conn.execute("SELECT price_usd FROM token_price").fetchone()[0]
        assert stored == pytest.approx(8.80)

    def test_collision_count_is_visible_to_caller(self, conn, price):
        """The return value is how the orchestrator computes duplicate_after_alignment."""
        rows = [
            price.model_copy(update={"price_usd": 8.80}),
            price.model_copy(update={"price_usd": 8.95}),
            price.model_copy(update={"ts": HOUR + timedelta(hours=1)}),
        ]
        inserted = write_token_prices(conn, rows)
        assert inserted == 2
        assert len(rows) - inserted == 1


# ---------- source column behaviour ----------


class TestSourceColumn:
    def test_model_default_is_defillama(self, conn):
        """Constructing without source yields 'defillama', which is what gets written."""
        price = TokenPrice(
            chain="ethereum",
            token_address=UNI,
            ts=HOUR,
            price_usd=8.87,
            fetched_at=FETCHED,
        )
        write_token_prices(conn, [price])
        assert conn.execute("SELECT source FROM token_price").fetchone()[0] == "defillama"

    def test_ddl_default_applies_on_direct_sql_insert(self, conn):
        """The column default backs up the model default for raw SQL paths."""
        conn.execute(
            "INSERT INTO token_price (chain, token_address, ts, price_usd, fetched_at) "
            "VALUES ('ethereum', ?, ?, 8.87, ?)",
            [UNI, HOUR, FETCHED],
        )
        assert conn.execute("SELECT source FROM token_price").fetchone()[0] == "defillama"

    def test_two_sources_coexist_at_same_hour(self, conn, price):
        """The reason source is in the PK: a fallback provider must not collide."""
        defillama = price
        coingecko = price.model_copy(update={"source": "coingecko", "price_usd": 8.91})

        assert write_token_prices(conn, [defillama, coingecko]) == 2
        rows = conn.execute("SELECT source, price_usd FROM token_price ORDER BY source").fetchall()
        assert [r[0] for r in rows] == ["coingecko", "defillama"]
        assert rows[0][1] == pytest.approx(8.91)
        assert rows[1][1] == pytest.approx(8.87)

    def test_same_source_still_collides(self, conn, price):
        assert write_token_prices(conn, [price, price.model_copy(update={"price_usd": 1.0})]) == 1

    def test_pipeline_can_select_one_source(self, conn, price):
        write_token_prices(
            conn,
            [price, price.model_copy(update={"source": "coingecko", "price_usd": 8.91})],
        )
        row = conn.execute(
            "SELECT price_usd FROM token_price WHERE source = 'defillama'"
        ).fetchone()
        assert row[0] == pytest.approx(8.87)


# ---------- Helpers ----------


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM token_price").fetchone()[0]
