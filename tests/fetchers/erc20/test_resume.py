"""Tests for AW_02's --resume mode."""

import sys
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from alphawallets.db import connect
from alphawallets.fetchers.erc20.aw_02_erc20_transfers import (
    _build_arg_parser,
    get_resume_point,
    main,
)
from alphawallets.fetchers.erc20.writer import create_tables

UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


@pytest.fixture
def conn():
    with connect(":memory:") as c:
        create_tables(c)
        yield c


def _add_transfer(conn, block: int, token: str = UNI, chain: str = "ethereum") -> None:
    conn.execute(
        "INSERT INTO erc20_transfer VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            chain,
            block,
            datetime(2026, 10, 1, 5, 0, tzinfo=UTC),
            f"0x{block:064x}",
            1,
            f"0x{block:064x}:log:0",
            token,
            "0x" + "f" * 40,
            "0x" + "e" * 40,
            "100",
            18,
        ],
    )


class TestGetResumePoint:
    def test_returns_highest_stored_block(self, conn):
        for block in (26094728, 26095727, 26095000):
            _add_transfer(conn, block)
        assert get_resume_point(conn, "ethereum", UNI) == 26095727

    def test_empty_table_returns_none(self, conn):
        assert get_resume_point(conn, "ethereum", UNI) is None

    def test_isolated_by_token(self, conn):
        """Another token's progress must not be mistaken for this token's."""
        _add_transfer(conn, 26095727, token=WETH)
        assert get_resume_point(conn, "ethereum", UNI) is None

    def test_isolated_by_chain(self, conn):
        _add_transfer(conn, 26095727, chain="base")
        assert get_resume_point(conn, "ethereum", UNI) is None

    def test_scoped_query_returns_the_right_tokens_max(self, conn):
        """Both tokens present: each resumes from its own highest block."""
        _add_transfer(conn, 26095727, token=WETH)
        _add_transfer(conn, 26094728, token=UNI)
        assert get_resume_point(conn, "ethereum", UNI) == 26094728
        assert get_resume_point(conn, "ethereum", WETH) == 26095727

    def test_accepts_checksummed_token(self, conn):
        """The cache stores lowercase; a checksummed argument must still match."""
        _add_transfer(conn, 26094728)
        assert get_resume_point(conn, "ethereum", UNI.upper()) == 26094728

    def test_reads_the_decoded_table_not_the_raw_one(self, conn):
        """Raw rows can include transfers the mapper dropped, so decoded is truth."""
        conn.execute(
            "INSERT INTO raw_erc20_transfer VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "ethereum",
                "0xdead:log:0",
                99999999,
                "0x" + "b" * 64,
                "0x" + "f" * 40,
                "0x" + "e" * 40,
                "100",
                1.0,
                "UNI",
                "erc20",
                UNI,
                18,
                0,
                datetime(2026, 10, 1, 5, 0, tzinfo=UTC),
            ],
        )
        assert get_resume_point(conn, "ethereum", UNI) is None


class TestResumeMutex:
    def test_resume_with_blocks_errors(self, capsys):
        with pytest.raises(SystemExit):
            _run_main(["--chain", "ethereum", "--contract", UNI, "--resume", "--blocks", "500"])
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
                    UNI,
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
        args = _build_arg_parser().parse_args(
            ["--chain", "ethereum", "--contract", UNI, "--resume"]
        )
        assert args.resume is True
        assert args.blocks is None

    def test_resume_defaults_to_false(self):
        assert (
            _build_arg_parser().parse_args(["--chain", "ethereum", "--contract", UNI]).resume
            is False
        )


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
            _add_transfer(c, 26095727)
        return path

    def test_resumes_from_stored_block_plus_one(self, db_path, capsys):
        with (
            patch("alphawallets.fetchers.erc20.aw_02_erc20_transfers.make_web3") as mock_w3,
            patch(
                "alphawallets.fetchers.erc20.aw_02_erc20_transfers.fetch_and_persist_erc20_transfers"
            ) as mock_fetch,
        ):
            mock_w3.return_value.eth.block_number = 26096000
            _run_main(
                ["--chain", "ethereum", "--contract", UNI, "--resume", "--db-path", str(db_path)]
            )

        kwargs = mock_fetch.call_args.kwargs
        assert kwargs["from_block"] == 26095728  # stored max + 1
        assert kwargs["to_block"] == 26096000  # current head
        assert "resume from stored block 26,095,727" in capsys.readouterr().out

    def test_falls_back_to_default_window_when_empty(self, tmp_path, capsys):
        empty = tmp_path / "empty.duckdb"
        with (
            patch("alphawallets.fetchers.erc20.aw_02_erc20_transfers.make_web3") as mock_w3,
            patch(
                "alphawallets.fetchers.erc20.aw_02_erc20_transfers.fetch_and_persist_erc20_transfers"
            ) as mock_fetch,
        ):
            mock_w3.return_value.eth.block_number = 26096000
            _run_main(
                ["--chain", "ethereum", "--contract", UNI, "--resume", "--db-path", str(empty)]
            )

        kwargs = mock_fetch.call_args.kwargs
        assert kwargs["from_block"] == 26095001  # head - 1000 + 1
        assert kwargs["to_block"] == 26096000
        out = capsys.readouterr().out
        assert "no previous data" in out

    def test_resume_ignores_another_tokens_progress(self, tmp_path):
        """A token with no rows of its own must not resume off another token."""
        path = tmp_path / "other.duckdb"
        with connect(path) as c:
            create_tables(c)
            _add_transfer(c, 26095727, token=WETH)

        with (
            patch("alphawallets.fetchers.erc20.aw_02_erc20_transfers.make_web3") as mock_w3,
            patch(
                "alphawallets.fetchers.erc20.aw_02_erc20_transfers.fetch_and_persist_erc20_transfers"
            ) as mock_fetch,
        ):
            mock_w3.return_value.eth.block_number = 26096000
            _run_main(
                ["--chain", "ethereum", "--contract", UNI, "--resume", "--db-path", str(path)]
            )

        assert mock_fetch.call_args.kwargs["from_block"] == 26095001  # defaulted

    def test_without_resume_the_mode_line_says_head_relative(self, db_path, capsys):
        with (
            patch("alphawallets.fetchers.erc20.aw_02_erc20_transfers.make_web3") as mock_w3,
            patch(
                "alphawallets.fetchers.erc20.aw_02_erc20_transfers.fetch_and_persist_erc20_transfers"
            ),
        ):
            mock_w3.return_value.eth.block_number = 26096000
            _run_main(
                [
                    "--chain",
                    "ethereum",
                    "--contract",
                    UNI,
                    "--blocks",
                    "10",
                    "--db-path",
                    str(db_path),
                ]
            )
        assert "head-relative" in capsys.readouterr().out
