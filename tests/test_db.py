"""Tests for the DuckDB connection helper."""

import pytest

from alphawallets.db import connect


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
