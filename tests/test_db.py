"""Tests for the DuckDB connection helper."""

import time
from datetime import UTC, datetime

import duckdb
import pytest

from alphawallets.db import SESSION_TIMEZONE, connect

# A deliberately awkward zone: UTC+5:30. A half-hour offset is what turns a
# locale-dependent date_trunc('hour', ...) from "harmless" into "matches no
# price row", because the result is no longer on an hour boundary in UTC.
HALF_HOUR_OFFSET_TZ = "Asia/Kolkata"


@pytest.fixture
def host_tz_kolkata(monkeypatch):
    """Run the test as if the host machine were in Asia/Kolkata."""
    monkeypatch.setenv("TZ", HALF_HOUR_OFFSET_TZ)
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class TestConnect:
    def test_memory_connection(self):
        with connect(":memory:") as conn:
            result = conn.execute("SELECT 42").fetchone()
            assert result[0] == 42

    def test_explicit_path(self, tmp_path):
        db_path = tmp_path / "test.duckdb"
        assert not db_path.exists()

        with connect(db_path) as conn:
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.execute("INSERT INTO t VALUES (1)")

        assert db_path.exists()

        # Re-open the same file — persistence proved
        with connect(db_path) as conn:
            result = conn.execute("SELECT x FROM t").fetchone()
            assert result[0] == 1

    def test_creates_parent_directory(self, tmp_path):
        deep_path = tmp_path / "a" / "b" / "c" / "test.duckdb"
        assert not deep_path.parent.exists()

        with connect(deep_path) as conn:
            conn.execute("SELECT 1")

        assert deep_path.parent.exists()
        assert deep_path.exists()

    def test_default_path_uses_config(self, tmp_path, monkeypatch):
        # Redirect the default cache path to tmp_path via env var
        default_target = tmp_path / "default_cache.duckdb"
        monkeypatch.setenv("ALPHAWALLETS_CACHE_DB", str(default_target))

        with connect() as conn:
            conn.execute("SELECT 1")

        assert default_target.exists()

    def test_connection_closed_after_exit(self, tmp_path):
        db_path = tmp_path / "test.duckdb"
        with connect(db_path) as conn:
            conn.execute("SELECT 1")

        # After exit, calling anything on the closed connection should raise
        with pytest.raises(Exception):  # noqa: B017 — duckdb raises ConnectionException
            conn.execute("SELECT 1")


class TestSessionTimezone:
    """The session timezone must be UTC regardless of the host locale.

    Regression tests for a silent integration failure found while building the
    wallet activity proof: the PnL join is
    date_trunc('hour', block_timestamp) = token_price.ts, token_price.ts is
    always hour-aligned UTC, and date_trunc on a TIMESTAMPTZ truncates in the
    session timezone. On a half-hour-offset host the truncated value is not on a
    UTC hour boundary, so the join matches nothing and reports zero rows rather
    than an error.
    """

    def test_session_timezone_is_utc(self):
        with connect(":memory:") as conn:
            assert conn.execute("SELECT current_setting('TimeZone')").fetchone()[0] == "UTC"

    def test_session_timezone_is_utc_on_half_hour_offset_host(self, host_tz_kolkata):
        with connect(":memory:") as conn:
            assert conn.execute("SELECT current_setting('TimeZone')").fetchone()[0] == "UTC"

    def test_unpinned_connection_would_not_be_utc(self, host_tz_kolkata):
        """Control: prove the fixture actually changes what DuckDB would do.

        Without this, the test above could pass simply because the host is
        already UTC, and the pin would be untested.
        """
        conn = duckdb.connect(":memory:")
        try:
            assert conn.execute("SELECT current_setting('TimeZone')").fetchone()[0] != "UTC"
        finally:
            conn.close()

    def test_hour_truncation_lands_on_utc_hour_boundary(self, host_tz_kolkata):
        """The join key itself, on the host that would break it."""
        # 14:53:11Z — mid-hour, so truncation has somewhere wrong to land.
        instant = datetime(2026, 9, 27, 14, 53, 11, tzinfo=UTC)

        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE e (ts TIMESTAMPTZ)")
            conn.execute("INSERT INTO e VALUES (?)", [instant])
            truncated = conn.execute("SELECT date_trunc('hour', ts) FROM e").fetchone()[0]

        assert truncated.astimezone(UTC) == datetime(2026, 9, 27, 14, 0, 0, tzinfo=UTC)
        assert (truncated.minute, truncated.second) == (0, 0)

    def test_hour_truncation_join_matches_a_price_row(self, host_tz_kolkata):
        """End-to-end shape of the real join, including the half-hour trap.

        Asia/Kolkata truncation would produce 14:30Z, so a match here means the
        pin is doing its job; a zero-row result is the silent failure mode.
        """
        event_ts = datetime(2026, 9, 27, 14, 53, 11, tzinfo=UTC)
        price_ts = datetime(2026, 9, 27, 14, 0, 0, tzinfo=UTC)

        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE e (ts TIMESTAMPTZ)")
            conn.execute("CREATE TABLE p (ts TIMESTAMPTZ, price DOUBLE)")
            conn.execute("INSERT INTO e VALUES (?)", [event_ts])
            conn.execute("INSERT INTO p VALUES (?, ?)", [price_ts, 8.94])

            matched = conn.execute(
                "SELECT p.price FROM e LEFT JOIN p ON date_trunc('hour', e.ts) = p.ts"
            ).fetchall()

        assert matched == [(8.94,)]

    def test_session_timezone_constant_is_utc(self):
        """The constant is the documented contract, not an incidental default."""
        assert SESSION_TIMEZONE == "UTC"
