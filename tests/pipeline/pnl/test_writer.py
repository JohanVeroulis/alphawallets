"""Tests for the wallet_pnl writer (ADR 0012 step 5)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alphawallets.db import SchemaDriftError, _canonical_type, connect, live_columns
from alphawallets.pipeline.pnl.models import WalletPnL
from alphawallets.pipeline.pnl.writer import (
    EXPECTED_COLUMNS,
    create_tables,
    write_wallet_pnl,
)

WALLET = "0x" + "1" * 40
OTHER = "0x" + "2" * 40
UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"

AS_OF = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)
COMPUTED_AT = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


@pytest.fixture
def conn():
    with connect(":memory:") as c:
        create_tables(c)
        yield c


def row(
    *,
    wallet: str = WALLET,
    token_address: str = UNI,
    days: int = 30,
    chain: str = "ethereum",
    realized: float = 10.0,
    balance: Decimal = Decimal(10**18),
    avg_cost: float | None = 8.0,
    pre_window: bool = False,
    unpriceable: bool = False,
) -> WalletPnL:
    return WalletPnL(
        chain=chain,
        wallet=wallet,
        token_address=token_address,
        window_start=AS_OF - timedelta(days=days),
        window_end=AS_OF,
        realized_pnl_usd=realized,
        realized_pnl_trading_usd=realized,
        realized_pnl_airdrop_usd=0.0,
        unrealized_pnl_usd=None,
        bought_usd=100.0,
        sold_usd=110.0,
        realization_count=1,
        balance_token=balance,
        avg_cost_basis_usd=avg_cost,
        has_pre_window_activity=pre_window,
        has_unpriceable_events=unpriceable,
        has_smart_wallet_signal=False,
        computed_at=COMPUTED_AT,
    )


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM wallet_pnl").fetchone()[0]


class TestSchema:
    def test_table_created(self, conn):
        names = [
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        ]
        assert "wallet_pnl" in names

    def test_idempotent(self, conn):
        create_tables(conn)
        create_tables(conn)

    def test_expected_columns_agree_with_the_ddl(self, conn):
        """Both are hand-written; this is what keeps them in step."""
        assert live_columns(conn, "wallet_pnl") == [
            (name, _canonical_type(type_name)) for name, type_name in EXPECTED_COLUMNS
        ]

    def test_drifted_table_raises(self):
        """ADR 0009's guard: an older-shaped table fails loudly at startup."""
        with connect(":memory:") as c:
            c.execute(
                "CREATE TABLE wallet_pnl ("
                "chain VARCHAR, wallet VARCHAR, token_address VARCHAR, "
                "window_start TIMESTAMPTZ, window_end TIMESTAMPTZ)"
            )
            with pytest.raises(SchemaDriftError, match="realized_pnl_usd"):
                create_tables(c)

    def test_balance_token_is_varchar(self, conn):
        """uint256 quantities overflow HUGEINT (same reason as value_raw)."""
        types = dict(live_columns(conn, "wallet_pnl"))
        assert types["balance_token"] == "VARCHAR"

    def test_nullable_columns_accept_null(self, conn):
        write_wallet_pnl(conn, [row(avg_cost=None)])
        stored = conn.execute(
            "SELECT unrealized_pnl_usd, avg_cost_basis_usd FROM wallet_pnl"
        ).fetchone()
        assert stored == (None, None)


class TestWriteWalletPnl:
    def test_empty_iterable_writes_nothing(self, conn):
        assert write_wallet_pnl(conn, []) == 0
        assert _count(conn) == 0

    def test_writes_five_rows(self, conn):
        rows = [row(wallet=f"0x{i:040x}") for i in range(5)]
        assert write_wallet_pnl(conn, rows) == 5
        assert _count(conn) == 5

    def test_rows_are_retrievable(self, conn):
        write_wallet_pnl(conn, [row(realized=42.5)])
        stored = conn.execute(
            "SELECT chain, wallet, token_address, realized_pnl_usd, realization_count "
            "FROM wallet_pnl"
        ).fetchone()
        assert stored == ("ethereum", WALLET, UNI, 42.5, 1)

    def test_both_windows_coexist(self, conn):
        """The PK includes both bounds, so 30d and 90d are separate rows."""
        write_wallet_pnl(conn, [row(days=30), row(days=90)])
        assert _count(conn) == 2

    def test_flags_round_trip(self, conn):
        write_wallet_pnl(conn, [row(pre_window=True, unpriceable=True)])
        stored = conn.execute(
            "SELECT has_pre_window_activity, has_unpriceable_events, "
            "has_smart_wallet_signal FROM wallet_pnl"
        ).fetchone()
        assert stored == (True, True, False)

    def test_timestamps_round_trip_as_utc(self, conn):
        write_wallet_pnl(conn, [row()])
        start, end, computed = conn.execute(
            "SELECT window_start, window_end, computed_at FROM wallet_pnl"
        ).fetchone()
        assert end == AS_OF
        assert start == AS_OF - timedelta(days=30)
        assert computed == COMPUTED_AT

    def test_consumes_a_generator(self, conn):
        """The writer streams rather than materialising compute_wallet_pnl's output."""
        assert write_wallet_pnl(conn, (row(wallet=f"0x{i:040x}") for i in range(3))) == 3
        assert _count(conn) == 3

    def test_logs_the_per_window_breakdown(self, conn, caplog):
        import logging

        with caplog.at_level(logging.INFO):
            write_wallet_pnl(conn, [row(days=30), row(days=90), row(days=30, wallet=OTHER)])
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "3 row(s) written" in combined
        assert "'30d': 2" in combined
        assert "'90d': 1" in combined


class TestReplaceSemantics:
    """INSERT OR REPLACE, unlike every other writer here.

    ADR 0012's final consequence: a running cost basis means a window's PnL
    legitimately changes when earlier events are re-fetched. Ignoring the second
    write would leave a stale figure and make computed_at a lie about which data
    produced the row.
    """

    def test_rerun_keeps_the_row_count_stable(self, conn):
        write_wallet_pnl(conn, [row()])
        write_wallet_pnl(conn, [row()])
        assert _count(conn) == 1

    def test_rerun_updates_the_value(self, conn):
        write_wallet_pnl(conn, [row(realized=10.0)])
        write_wallet_pnl(conn, [row(realized=99.0)])
        assert conn.execute("SELECT realized_pnl_usd FROM wallet_pnl").fetchone()[0] == 99.0

    def test_rerun_updates_computed_at(self, conn):
        """So a changed number can be explained rather than disputed."""
        write_wallet_pnl(conn, [row()])
        later = COMPUTED_AT + timedelta(hours=3)
        updated = row().model_copy(update={"computed_at": later, "realized_pnl_usd": 55.0})
        write_wallet_pnl(conn, [updated])
        stored = conn.execute("SELECT computed_at, realized_pnl_usd FROM wallet_pnl").fetchone()
        assert stored == (later, 55.0)

    def test_replacement_counts_as_written(self, conn):
        write_wallet_pnl(conn, [row()])
        assert write_wallet_pnl(conn, [row(realized=1.0)]) == 1

    def test_a_different_window_is_not_a_replacement(self, conn):
        write_wallet_pnl(conn, [row(days=30)])
        write_wallet_pnl(conn, [row(days=90)])
        assert _count(conn) == 2

    def test_a_different_token_is_not_a_replacement(self, conn):
        write_wallet_pnl(conn, [row()])
        write_wallet_pnl(conn, [row(token_address="0x" + "7" * 40)])
        assert _count(conn) == 2

    def test_a_different_chain_is_not_a_replacement(self, conn):
        write_wallet_pnl(conn, [row()])
        write_wallet_pnl(conn, [row(chain="base")])
        assert _count(conn) == 2


class TestBalanceTokenPrecision:
    def test_uint256_scale_round_trips(self, conn):
        """A 20+ digit quantity would lose digits through float or HUGEINT."""
        huge = Decimal("123456789012345678901234567890")
        write_wallet_pnl(conn, [row(balance=huge)])
        stored = conn.execute("SELECT balance_token FROM wallet_pnl").fetchone()[0]
        assert stored == str(huge)
        assert Decimal(stored) == huge

    def test_str_of_decimal_is_exact(self, conn):
        """str() on a Decimal does not go through float, so no digits are lost."""
        awkward = Decimal("333333333333333333")
        write_wallet_pnl(conn, [row(balance=awkward)])
        stored = conn.execute("SELECT balance_token FROM wallet_pnl").fetchone()[0]
        assert Decimal(stored) == awkward

    def test_zero_balance_round_trips(self, conn):
        write_wallet_pnl(conn, [row(balance=Decimal(0), avg_cost=None)])
        stored = conn.execute("SELECT balance_token FROM wallet_pnl").fetchone()[0]
        assert Decimal(stored) == Decimal(0)
