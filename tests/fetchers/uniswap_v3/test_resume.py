"""Tests for AW_01's --resume mode."""

import sys
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from alphawallets.db import connect
from alphawallets.fetchers.uniswap_v3.aw_01_uniswap_v3_swaps import (
    _build_arg_parser,
    get_resume_point,
    main,
)
from alphawallets.fetchers.uniswap_v3.writer import create_tables

POOL = "0x1d42064fc4beb5f8aaf85f4617ae8b3b5b8bd801"
OTHER_POOL = "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"


@pytest.fixture
def conn():
    with connect(":memory:") as c:
        create_tables(c)
        yield c


def _add_swap(conn, block: int, pool: str = POOL, chain: str = "ethereum") -> None:
    conn.execute(
        "INSERT INTO uniswap_v3_swap VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            chain,
            block,
            datetime(2026, 10, 1, 5, 0, tzinfo=UTC),
            f"0x{block:064x}",
            1,
            pool,
            "0x" + "9" * 40,
            "0x" + "f" * 40,
            "0x" + "e" * 40,
            "100",
            "-100",
            "1",
            "2",
            1,
        ],
    )


class TestGetResumePoint:
    def test_returns_highest_stored_block(self, conn):
        for block in (26094728, 26095727, 26095000):
            _add_swap(conn, block)
        assert get_resume_point(conn, "ethereum", POOL) == 26095727

    def test_empty_table_returns_none(self, conn):
        assert get_resume_point(conn, "ethereum", POOL) is None

    def test_isolated_by_pool(self, conn):
        """Another pool's progress must not be mistaken for this pool's."""
        _add_swap(conn, 26095727, pool=OTHER_POOL)
        assert get_resume_point(conn, "ethereum", POOL) is None

    def test_isolated_by_chain(self, conn):
        _add_swap(conn, 26095727, chain="base")
        assert get_resume_point(conn, "ethereum", POOL) is None

    def test_scoped_query_returns_the_right_pools_max(self, conn):
        """Both pools present: each resumes from its own highest block."""
        _add_swap(conn, 26095727, pool=OTHER_POOL)
        _add_swap(conn, 26094728, pool=POOL)
        assert get_resume_point(conn, "ethereum", POOL) == 26094728
        assert get_resume_point(conn, "ethereum", OTHER_POOL) == 26095727

    def test_accepts_checksummed_pool(self, conn):
        """The cache stores lowercase; a checksummed argument must still match."""
        _add_swap(conn, 26094728)
        assert get_resume_point(conn, "ethereum", POOL.upper()) == 26094728

    def test_reads_the_decoded_table_not_the_raw_one(self, conn):
        """Raw rows can include logs the pipeline excluded as reorged."""
        conn.execute(
            "INSERT INTO raw_uniswap_v3_swap VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "ethereum",
                99999999,
                "0x" + "a" * 64,
                "0x" + "b" * 64,
                1,
                1,
                POOL,
                ["0x" + "c" * 64],
                "0x00",
                False,
            ],
        )
        assert get_resume_point(conn, "ethereum", POOL) is None


class TestResumeMutex:
    def test_resume_with_blocks_errors(self, capsys):
        with pytest.raises(SystemExit):
            _run_main(["--chain", "ethereum", "--pool", POOL, "--resume", "--blocks", "500"])
        err = capsys.readouterr().err
        assert "--resume" in err
        assert "--blocks" in err

    def test_resume_with_explicit_range_errors(self, capsys):
        with pytest.raises(SystemExit):
            _run_main(
                [
                    "--chain",
                    "ethereum",
                    "--pool",
                    POOL,
                    "--resume",
                    "--from-block",
                    "100",
                    "--to-block",
                    "200",
                ]
            )
        err = capsys.readouterr().err
        assert "--resume" in err
        assert "--from-block" in err

    def test_resume_alone_parses(self):
        args = _build_arg_parser().parse_args(["--chain", "ethereum", "--pool", POOL, "--resume"])
        assert args.resume is True
        assert args.blocks is None

    def test_resume_defaults_to_false(self):
        args = _build_arg_parser().parse_args(["--chain", "ethereum", "--pool", POOL])
        assert args.resume is False


def _run_main(argv: list[str]) -> None:
    with patch.object(sys, "argv", ["prog", *argv]):
        main()


class TestResumeCliIntegration:
    """--resume must hand the orchestrator the range the stored data implies."""

    @pytest.fixture
    def db_path(self, tmp_path):
        path = tmp_path / "cache.duckdb"
        with connect(path) as c:
            create_tables(c)
            _add_swap(c, 26095727)
        return path

    def test_resumes_from_stored_block_plus_one(self, db_path, capsys):
        with (
            patch("alphawallets.fetchers.uniswap_v3.aw_01_uniswap_v3_swaps.make_web3") as mock_w3,
            patch(
                "alphawallets.fetchers.uniswap_v3.aw_01_uniswap_v3_swaps.fetch_and_persist_swaps"
            ) as mock_fetch,
        ):
            mock_w3.return_value.eth.block_number = 26096000
            _run_main(
                ["--chain", "ethereum", "--pool", POOL, "--resume", "--db-path", str(db_path)]
            )

        kwargs = mock_fetch.call_args.kwargs
        assert kwargs["from_block"] == 26095728  # stored max + 1
        assert kwargs["to_block"] == 26096000  # current head
        assert "resume from stored block 26,095,727" in capsys.readouterr().out

    def test_falls_back_to_default_window_when_empty(self, tmp_path, capsys):
        empty = tmp_path / "empty.duckdb"
        with (
            patch("alphawallets.fetchers.uniswap_v3.aw_01_uniswap_v3_swaps.make_web3") as mock_w3,
            patch(
                "alphawallets.fetchers.uniswap_v3.aw_01_uniswap_v3_swaps.fetch_and_persist_swaps"
            ) as mock_fetch,
        ):
            mock_w3.return_value.eth.block_number = 26096000
            _run_main(["--chain", "ethereum", "--pool", POOL, "--resume", "--db-path", str(empty)])

        kwargs = mock_fetch.call_args.kwargs
        assert kwargs["from_block"] == 26095001  # head - 1000 + 1
        assert kwargs["to_block"] == 26096000
        out = capsys.readouterr().out
        assert "no previous data" in out

    def test_resume_ignores_another_pools_progress(self, tmp_path):
        """A pool with no rows of its own must not resume off a sibling pool."""
        path = tmp_path / "other.duckdb"
        with connect(path) as c:
            create_tables(c)
            _add_swap(c, 26095727, pool=OTHER_POOL)

        with (
            patch("alphawallets.fetchers.uniswap_v3.aw_01_uniswap_v3_swaps.make_web3") as mock_w3,
            patch(
                "alphawallets.fetchers.uniswap_v3.aw_01_uniswap_v3_swaps.fetch_and_persist_swaps"
            ) as mock_fetch,
        ):
            mock_w3.return_value.eth.block_number = 26096000
            _run_main(["--chain", "ethereum", "--pool", POOL, "--resume", "--db-path", str(path)])

        assert mock_fetch.call_args.kwargs["from_block"] == 26095001  # defaulted

    def test_without_resume_the_mode_line_says_head_relative(self, db_path, capsys):
        with (
            patch("alphawallets.fetchers.uniswap_v3.aw_01_uniswap_v3_swaps.make_web3") as mock_w3,
            patch(
                "alphawallets.fetchers.uniswap_v3.aw_01_uniswap_v3_swaps.fetch_and_persist_swaps"
            ),
        ):
            mock_w3.return_value.eth.block_number = 26096000
            _run_main(
                ["--chain", "ethereum", "--pool", POOL, "--blocks", "10", "--db-path", str(db_path)]
            )
        assert "head-relative" in capsys.readouterr().out
