"""Tests for the PnL calculator's event reader (ADR 0012 step 4, skeleton)."""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alphawallets.db import connect
from alphawallets.fetchers.erc20.models import ERC20Transfer
from alphawallets.fetchers.erc20.writer import create_tables as create_erc20_tables
from alphawallets.fetchers.prices.writer import create_tables as create_price_tables
from alphawallets.pipeline.pnl import airdrop_registry
from alphawallets.pipeline.pnl.airdrop_registry import (
    AirdropAttributionStatus,
    AirdropRecord,
)
from alphawallets.pipeline.pnl.calculator import (
    TRANSFER_ORDER_BY,
    _fetch_transfer_events,
    _load_price_cache,
    _lookup_price,
    _process_events,
    compute_wallet_pnl,
)
from alphawallets.pipeline.pnl.models import WalletPnL

WALLET = "0x" + "1" * 40
OTHER = "0x" + "2" * 40
THIRD = "0x" + "3" * 40
DISTRIBUTOR = "0x" + "d" * 40
# UNI/WETH 0.3% on Ethereum — a configured pool (ADR 0014, verified in PR #28).
UNI_WETH_POOL = "0x1d42064fc4beb5f8aaf85f4617ae8b3b5b8bd801"

UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
AAVE = "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9"

T0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
WEI = 10**18


def _insert(
    conn,
    *,
    unique_id: str,
    block_timestamp: datetime,
    block_number: int,
    log_index: int | None,
    from_addr: str = OTHER,
    to_addr: str = WALLET,
    token_address: str = UNI,
    chain: str = "ethereum",
    token_decimals: int | None = 18,
) -> None:
    conn.execute(
        "INSERT INTO erc20_transfer VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            chain,
            block_number,
            block_timestamp,
            "0x" + "a" * 64,
            log_index,
            unique_id,
            token_address,
            from_addr,
            to_addr,
            str(WEI),
            token_decimals,
        ],
    )


def _insert_qty(
    conn,
    *,
    unique_id: str,
    block_timestamp: datetime,
    block_number: int,
    qty: int,
    from_addr: str = OTHER,
    to_addr: str = WALLET,
    token_address: str = UNI,
    chain: str = "ethereum",
) -> None:
    """Like _insert but with an explicit quantity, for PnL arithmetic tests."""
    conn.execute(
        "INSERT INTO erc20_transfer VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            chain,
            block_number,
            block_timestamp,
            "0x" + "a" * 64,
            0,
            unique_id,
            token_address,
            from_addr,
            to_addr,
            str(qty),
            18,
        ],
    )


@pytest.fixture
def cache():
    """An empty cache with the erc20_transfer table created."""
    with connect(":memory:") as c:
        create_erc20_tables(c)
        yield c


@pytest.fixture
def ordered_cache(cache):
    """Nine rows inserted in deliberately scrambled order.

    Designed so each ORDER BY clause is load-bearing:
      - rows a/b/c share a timestamp and differ by block_number
      - rows c/d/e share timestamp AND block_number, differ by log_index
      - rows e/f share timestamp, block_number and a NULL log_index, so only
        unique_id can separate them
    """
    _insert(cache, unique_id="f", block_timestamp=T0, block_number=100, log_index=None)
    _insert(cache, unique_id="d", block_timestamp=T0, block_number=100, log_index=1)
    _insert(
        cache, unique_id="g", block_timestamp=T0 + timedelta(hours=2), block_number=300, log_index=0
    )
    _insert(cache, unique_id="b", block_timestamp=T0, block_number=99, log_index=0)
    _insert(cache, unique_id="e", block_timestamp=T0, block_number=100, log_index=2)
    _insert(cache, unique_id="a", block_timestamp=T0, block_number=98, log_index=0)
    _insert(
        cache, unique_id="h", block_timestamp=T0 + timedelta(hours=3), block_number=400, log_index=0
    )
    _insert(cache, unique_id="c", block_timestamp=T0, block_number=100, log_index=0)
    _insert(
        cache, unique_id="i", block_timestamp=T0 + timedelta(hours=1), block_number=200, log_index=0
    )
    return cache


class TestOrdering:
    def test_returns_rows_in_deterministic_chronological_order(self, ordered_cache):
        ids = [t.unique_id for t in _fetch_transfer_events(ordered_cache)]
        assert ids == ["a", "b", "c", "d", "e", "f", "i", "g", "h"]

    def test_timestamps_are_non_decreasing(self, ordered_cache):
        stamps = [t.block_timestamp for t in _fetch_transfer_events(ordered_cache)]
        assert stamps == sorted(stamps)

    def test_block_number_breaks_a_timestamp_tie(self, ordered_cache):
        """Same-second blocks are routine on Base at ~2s block time."""
        same_second = [t for t in _fetch_transfer_events(ordered_cache) if t.block_timestamp == T0]
        numbers = [t.block_number for t in same_second]
        assert numbers == sorted(numbers)

    def test_log_index_orders_within_a_block(self, ordered_cache):
        in_block = [
            t
            for t in _fetch_transfer_events(ordered_cache)
            if t.block_timestamp == T0 and t.block_number == 100
        ]
        assert [t.unique_id for t in in_block] == ["c", "d", "e", "f"]
        assert [t.log_index for t in in_block] == [0, 1, 2, None]

    def test_null_log_index_sorts_last_within_a_block(self, ordered_cache):
        """AW_02's Transfers API rows carry None (ADR 0007); NULLS LAST is explicit
        rather than leaving the position to DuckDB."""
        in_block = [
            t
            for t in _fetch_transfer_events(ordered_cache)
            if t.block_timestamp == T0 and t.block_number == 100
        ]
        assert in_block[-1].log_index is None

    def test_unique_id_is_the_total_order_tiebreak(self, cache):
        """Two rows identical on every other clause must still have a fixed order.

        Without this the sort has residual freedom and the FIFO cost basis could
        differ between two runs over the same cache.
        """
        for uid in ("zz", "aa", "mm"):
            _insert(cache, unique_id=uid, block_timestamp=T0, block_number=1, log_index=None)
        ids = [t.unique_id for t in _fetch_transfer_events(cache)]
        assert ids == ["aa", "mm", "zz"]

    def test_order_is_stable_across_repeated_reads(self, ordered_cache):
        first = [t.unique_id for t in _fetch_transfer_events(ordered_cache)]
        second = [t.unique_id for t in _fetch_transfer_events(ordered_cache)]
        assert first == second

    def test_order_by_clause_names_all_four_columns(self):
        """A regression guard: dropping a clause silently weakens determinism."""
        for column in ("block_timestamp", "block_number", "log_index", "unique_id"):
            assert column in TRANSFER_ORDER_BY
        assert "NULLS LAST" in TRANSFER_ORDER_BY


class TestNullDecimals:
    def test_rows_with_null_decimals_are_skipped(self, cache):
        _insert(cache, unique_id="ok", block_timestamp=T0, block_number=1, log_index=0)
        _insert(
            cache,
            unique_id="bad",
            block_timestamp=T0 + timedelta(minutes=1),
            block_number=2,
            log_index=0,
            token_decimals=None,
        )
        ids = [t.unique_id for t in _fetch_transfer_events(cache)]
        assert ids == ["ok"]

    def test_skip_is_logged_with_chain_token_and_count(self, cache, caplog):
        for i in range(3):
            _insert(
                cache,
                unique_id=f"bad{i}",
                block_timestamp=T0 + timedelta(minutes=i),
                block_number=i,
                log_index=0,
                token_decimals=None,
            )
        with caplog.at_level(logging.WARNING):
            list(_fetch_transfer_events(cache))

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        combined = "\n".join(warnings)
        assert "Skipped 3 transfer(s)" in combined
        assert "ethereum" in combined
        assert UNI in combined

    def test_counts_are_grouped_per_chain_and_token(self, cache, caplog):
        """Per-row warnings would bury the signal under thousands of lines."""
        _insert(
            cache,
            unique_id="u1",
            block_timestamp=T0,
            block_number=1,
            log_index=0,
            token_address=UNI,
            token_decimals=None,
        )
        _insert(
            cache,
            unique_id="u2",
            block_timestamp=T0,
            block_number=2,
            log_index=0,
            token_address=UNI,
            token_decimals=None,
        )
        _insert(
            cache,
            unique_id="a1",
            block_timestamp=T0,
            block_number=3,
            log_index=0,
            token_address=AAVE,
            token_decimals=None,
        )

        with caplog.at_level(logging.WARNING):
            list(_fetch_transfer_events(cache))

        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "Skipped 2 transfer(s)" in combined  # UNI
        assert "Skipped 1 transfer(s)" in combined  # AAVE
        assert "2 (chain, token) pair(s)" in combined

    def test_no_warning_when_every_row_has_decimals(self, cache, caplog):
        _insert(cache, unique_id="ok", block_timestamp=T0, block_number=1, log_index=0)
        with caplog.at_level(logging.WARNING):
            list(_fetch_transfer_events(cache))
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_zero_decimals_is_kept(self, cache):
        """0 is a valid value — some tokens have no fractional part. Only NULL skips."""
        _insert(
            cache,
            unique_id="zero",
            block_timestamp=T0,
            block_number=1,
            log_index=0,
            token_decimals=0,
        )
        events = list(_fetch_transfer_events(cache))
        assert len(events) == 1
        assert events[0].token_decimals == 0

    def test_skipping_does_not_disturb_the_order_of_the_rest(self, cache):
        _insert(cache, unique_id="a", block_timestamp=T0, block_number=1, log_index=0)
        _insert(
            cache,
            unique_id="b",
            block_timestamp=T0,
            block_number=2,
            log_index=0,
            token_decimals=None,
        )
        _insert(cache, unique_id="c", block_timestamp=T0, block_number=3, log_index=0)
        assert [t.unique_id for t in _fetch_transfer_events(cache)] == ["a", "c"]


class TestFilters:
    def test_no_filters_reads_everything(self, cache):
        _insert(cache, unique_id="eth", block_timestamp=T0, block_number=1, log_index=0)
        _insert(
            cache, unique_id="base", block_timestamp=T0, block_number=2, log_index=0, chain="base"
        )
        assert len(list(_fetch_transfer_events(cache))) == 2

    def test_chain_filter_restricts(self, cache):
        _insert(cache, unique_id="eth", block_timestamp=T0, block_number=1, log_index=0)
        _insert(
            cache, unique_id="base", block_timestamp=T0, block_number=2, log_index=0, chain="base"
        )
        ids = [t.unique_id for t in _fetch_transfer_events(cache, chains=["base"])]
        assert ids == ["base"]

    def test_empty_chain_list_returns_nothing(self, cache):
        """[] means "no chains", which is a different request from None.

        Falling through would silently widen the query to every chain.
        """
        _insert(cache, unique_id="eth", block_timestamp=T0, block_number=1, log_index=0)
        assert list(_fetch_transfer_events(cache, chains=[])) == []

    def test_wallet_filter_matches_sender_or_receiver(self, cache):
        _insert(
            cache,
            unique_id="in",
            block_timestamp=T0,
            block_number=1,
            log_index=0,
            from_addr=OTHER,
            to_addr=WALLET,
        )
        _insert(
            cache,
            unique_id="out",
            block_timestamp=T0,
            block_number=2,
            log_index=0,
            from_addr=WALLET,
            to_addr=OTHER,
        )
        _insert(
            cache,
            unique_id="none",
            block_timestamp=T0,
            block_number=3,
            log_index=0,
            from_addr=OTHER,
            to_addr=THIRD,
        )
        ids = [t.unique_id for t in _fetch_transfer_events(cache, wallet_filter=[WALLET])]
        assert ids == ["in", "out"]

    def test_wallet_filter_is_case_insensitive(self, cache):
        """A checksummed address must not silently match nothing — that would look
        like "no data" for a wallet with real activity."""
        _insert(cache, unique_id="in", block_timestamp=T0, block_number=1, log_index=0)
        ids = [t.unique_id for t in _fetch_transfer_events(cache, wallet_filter=[WALLET.upper()])]
        assert ids == ["in"]

    def test_empty_wallet_list_returns_nothing(self, cache):
        _insert(cache, unique_id="in", block_timestamp=T0, block_number=1, log_index=0)
        assert list(_fetch_transfer_events(cache, wallet_filter=[])) == []

    def test_multiple_wallets(self, cache):
        _insert(
            cache, unique_id="w1", block_timestamp=T0, block_number=1, log_index=0, to_addr=WALLET
        )
        _insert(
            cache, unique_id="w2", block_timestamp=T0, block_number=2, log_index=0, to_addr=OTHER
        )
        _insert(
            cache,
            unique_id="w3",
            block_timestamp=T0,
            block_number=3,
            log_index=0,
            from_addr=THIRD,
            to_addr=THIRD,
        )
        ids = [t.unique_id for t in _fetch_transfer_events(cache, wallet_filter=[WALLET, OTHER])]
        assert ids == ["w1", "w2"]

    def test_filters_combine(self, cache):
        _insert(
            cache,
            unique_id="keep",
            block_timestamp=T0,
            block_number=1,
            log_index=0,
            chain="base",
            to_addr=WALLET,
        )
        _insert(
            cache,
            unique_id="wrong_chain",
            block_timestamp=T0,
            block_number=2,
            log_index=0,
            chain="ethereum",
            to_addr=WALLET,
        )
        _insert(
            cache,
            unique_id="wrong_wallet",
            block_timestamp=T0,
            block_number=3,
            log_index=0,
            chain="base",
            from_addr=OTHER,
            to_addr=THIRD,
        )
        ids = [
            t.unique_id
            for t in _fetch_transfer_events(cache, chains=["base"], wallet_filter=[WALLET])
        ]
        assert ids == ["keep"]

    def test_filters_preserve_ordering(self, ordered_cache):
        ids = [t.unique_id for t in _fetch_transfer_events(ordered_cache, chains=["ethereum"])]
        assert ids == ["a", "b", "c", "d", "e", "f", "i", "g", "h"]


class TestShape:
    def test_yields_erc20transfer_models(self, cache):
        _insert(cache, unique_id="ok", block_timestamp=T0, block_number=1, log_index=0)
        events = list(_fetch_transfer_events(cache))
        assert all(isinstance(e, ERC20Transfer) for e in events)

    def test_fields_round_trip(self, cache):
        _insert(cache, unique_id="ok", block_timestamp=T0, block_number=42, log_index=7)
        event = next(iter(_fetch_transfer_events(cache)))
        assert event.chain == "ethereum"
        assert event.block_number == 42
        assert event.block_timestamp == T0
        assert event.log_index == 7
        assert event.unique_id == "ok"
        assert event.token_address == UNI
        assert event.from_addr == OTHER
        assert event.to_addr == WALLET
        assert event.value_raw == str(WEI)
        assert event.token_decimals == 18

    def test_returns_a_generator_not_a_list(self, cache):
        """A universe-wide read is large; the consumer processes one event at a time."""
        _insert(cache, unique_id="ok", block_timestamp=T0, block_number=1, log_index=0)
        result = _fetch_transfer_events(cache)
        assert not isinstance(result, list)
        assert next(iter(result)).unique_id == "ok"

    def test_empty_table_yields_nothing(self, cache):
        assert list(_fetch_transfer_events(cache)) == []

    def test_timestamps_are_utc(self, cache):
        _insert(cache, unique_id="ok", block_timestamp=T0, block_number=1, log_index=0)
        event = next(iter(_fetch_transfer_events(cache)))
        assert event.block_timestamp.utcoffset().total_seconds() == 0


# ---------- Price layer ----------


H0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
H1 = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
H2 = datetime(2026, 10, 1, 2, 0, tzinfo=UTC)


def _insert_price(
    conn,
    *,
    ts: datetime,
    price_usd: float,
    chain: str = "ethereum",
    token_address: str = UNI,
    source: str = "defillama",
) -> None:
    conn.execute(
        "INSERT INTO token_price VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [chain, token_address, ts, price_usd, 0.99, source, "chart", H0],
    )


@pytest.fixture
def price_cache_db():
    """Eight price rows: two tokens on Ethereum, one on Base, across three hours."""
    with connect(":memory:") as c:
        create_price_tables(c)
        _insert_price(c, ts=H0, price_usd=8.80, token_address=UNI)
        _insert_price(c, ts=H1, price_usd=8.85, token_address=UNI)
        _insert_price(c, ts=H2, price_usd=8.90, token_address=UNI)
        _insert_price(c, ts=H0, price_usd=179.40, token_address=AAVE)
        _insert_price(c, ts=H1, price_usd=179.50, token_address=AAVE)
        _insert_price(c, ts=H0, price_usd=8.81, token_address=UNI, chain="base")
        _insert_price(c, ts=H1, price_usd=8.86, token_address=UNI, chain="base")
        _insert_price(c, ts=H2, price_usd=179.60, token_address=AAVE)
        yield c


class TestLoadPriceCache:
    def test_key_shape_and_values(self, price_cache_db):
        cache = _load_price_cache(price_cache_db)
        assert cache[("ethereum", UNI, H0)] == 8.80
        assert cache[("ethereum", AAVE, H1)] == 179.50
        assert cache[("base", UNI, H0)] == 8.81

    def test_loads_every_row_when_unfiltered(self, price_cache_db):
        assert len(_load_price_cache(price_cache_db)) == 8

    def test_keys_are_hour_truncated_utc(self, price_cache_db):
        cache = _load_price_cache(price_cache_db)
        for _chain, _token, hour in cache:
            assert hour.utcoffset().total_seconds() == 0
            assert (hour.minute, hour.second, hour.microsecond) == (0, 0, 0)

    def test_token_addresses_are_lowercase_in_keys(self, price_cache_db):
        cache = _load_price_cache(price_cache_db)
        for _chain, token, _hour in cache:
            assert token == token.lower()

    def test_chain_filter(self, price_cache_db):
        cache = _load_price_cache(price_cache_db, chains=["base"])
        assert {c for c, _t, _h in cache} == {"base"}
        assert len(cache) == 2

    def test_token_filter(self, price_cache_db):
        cache = _load_price_cache(price_cache_db, tokens=[AAVE])
        assert {t for _c, t, _h in cache} == {AAVE}
        assert len(cache) == 3

    def test_token_filter_is_case_insensitive(self, price_cache_db):
        """A checksummed address must not quietly load an empty cache."""
        cache = _load_price_cache(price_cache_db, tokens=[AAVE.upper()])
        assert len(cache) == 3

    def test_filters_combine(self, price_cache_db):
        cache = _load_price_cache(price_cache_db, chains=["ethereum"], tokens=[UNI])
        assert set(cache) == {
            ("ethereum", UNI, H0),
            ("ethereum", UNI, H1),
            ("ethereum", UNI, H2),
        }

    def test_empty_chain_list_loads_nothing(self, price_cache_db):
        """[] means "none requested" — same semantics as the event reader."""
        assert _load_price_cache(price_cache_db, chains=[]) == {}

    def test_empty_token_list_loads_nothing(self, price_cache_db):
        assert _load_price_cache(price_cache_db, tokens=[]) == {}

    def test_empty_table_gives_an_empty_cache(self):
        with connect(":memory:") as c:
            create_price_tables(c)
            assert _load_price_cache(c) == {}

    def test_logs_size_and_pair_count(self, price_cache_db, caplog):
        with caplog.at_level(logging.INFO):
            _load_price_cache(price_cache_db)
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "Loaded 8 price hour(s)" in combined
        assert "3 (chain, token) pair(s)" in combined


class TestMultiSourceResolution:
    """token_price's PK includes source, so two providers can hold one hour.

    A dict keyed on (chain, token, hour) must choose, and choosing by scan order
    would make PnL depend on DuckDB's row order.
    """

    def test_preferred_source_wins_regardless_of_insert_order(self):
        for order in (("coingecko", "defillama"), ("defillama", "coingecko")):
            with connect(":memory:") as c:
                create_price_tables(c)
                for source in order:
                    price = 99.0 if source == "coingecko" else 8.80
                    _insert_price(c, ts=H0, price_usd=price, source=source)
                cache = _load_price_cache(c)
                assert cache[("ethereum", UNI, H0)] == 8.80

    def test_collision_is_logged(self, caplog):
        with connect(":memory:") as c:
            create_price_tables(c)
            _insert_price(c, ts=H0, price_usd=8.80, source="defillama")
            _insert_price(c, ts=H0, price_usd=99.0, source="coingecko")
            with caplog.at_level(logging.INFO):
                _load_price_cache(c)
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "more than one source" in combined

    def test_one_key_per_hour_after_resolution(self):
        with connect(":memory:") as c:
            create_price_tables(c)
            _insert_price(c, ts=H0, price_usd=8.80, source="defillama")
            _insert_price(c, ts=H0, price_usd=99.0, source="coingecko")
            assert len(_load_price_cache(c)) == 1

    def test_unlisted_source_is_used_when_it_is_the_only_one(self):
        """Deprioritised, not discarded — a lone CoinGecko row still prices the hour."""
        with connect(":memory:") as c:
            create_price_tables(c)
            _insert_price(c, ts=H0, price_usd=99.0, source="coingecko")
            assert _load_price_cache(c)[("ethereum", UNI, H0)] == 99.0

    def test_no_collision_log_when_sources_are_unique(self, price_cache_db, caplog):
        with caplog.at_level(logging.INFO):
            _load_price_cache(price_cache_db)
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "more than one source" not in combined


class TestLookupPrice:
    def test_exact_hour_match(self, price_cache_db):
        cache = _load_price_cache(price_cache_db)
        assert _lookup_price(cache, "ethereum", UNI, H1) == 8.85

    def test_mid_hour_event_truncates_down(self, price_cache_db):
        """A 01:47 event prices at the 01:00 row — truncation, not rounding."""
        cache = _load_price_cache(price_cache_db)
        event = H1 + timedelta(minutes=47, seconds=19)
        assert _lookup_price(cache, "ethereum", UNI, event) == 8.85

    def test_last_second_of_an_hour_still_truncates_down(self, price_cache_db):
        cache = _load_price_cache(price_cache_db)
        event = H1 + timedelta(minutes=59, seconds=59, microseconds=999999)
        assert _lookup_price(cache, "ethereum", UNI, event) == 8.85

    def test_missing_hour_returns_none(self, price_cache_db):
        """The MKR-grid case: a real answer, not an error (ADR 0012 decision 6)."""
        cache = _load_price_cache(price_cache_db)
        assert _lookup_price(cache, "ethereum", AAVE, H2 + timedelta(hours=5)) is None

    def test_hour_inside_the_range_but_absent_returns_none(self, price_cache_db):
        """AAVE has H0, H1 and H2 but nothing later; UNI has no H3 either."""
        cache = _load_price_cache(price_cache_db)
        assert _lookup_price(cache, "ethereum", UNI, H2 + timedelta(hours=1)) is None

    def test_unknown_token_returns_none(self, price_cache_db):
        cache = _load_price_cache(price_cache_db)
        assert _lookup_price(cache, "ethereum", "0x" + "9" * 40, H0) is None

    def test_wrong_chain_returns_none(self, price_cache_db):
        """AAVE is priced on Ethereum only in this fixture."""
        cache = _load_price_cache(price_cache_db)
        assert _lookup_price(cache, "base", AAVE, H0) is None

    def test_chain_scoping_picks_the_right_price(self, price_cache_db):
        """UNI is priced on both chains at H0, at different prices."""
        cache = _load_price_cache(price_cache_db)
        assert _lookup_price(cache, "ethereum", UNI, H0) == 8.80
        assert _lookup_price(cache, "base", UNI, H0) == 8.81

    def test_token_address_is_case_insensitive(self, price_cache_db):
        cache = _load_price_cache(price_cache_db)
        assert _lookup_price(cache, "ethereum", UNI.upper(), H0) == 8.80

    def test_non_utc_event_timestamp_truncates_on_the_utc_grid(self, price_cache_db):
        """A +05:30 timestamp must land on the UTC hour, not the local one.

        The same hazard ADR 0009's UTC session pin addressed in SQL, here in
        Python: 01:47Z rendered as 07:17+05:30 truncates to 07:00 locally, which
        is 01:30Z — an hour no price row can ever have.
        """
        from zoneinfo import ZoneInfo

        cache = _load_price_cache(price_cache_db)
        event = (H1 + timedelta(minutes=47)).astimezone(ZoneInfo("Asia/Kolkata"))
        assert _lookup_price(cache, "ethereum", UNI, event) == 8.85

    def test_empty_cache_returns_none(self):
        assert _lookup_price({}, "ethereum", UNI, H0) is None


# ---------- Event replay ----------


@pytest.fixture
def replay_db():
    """A cache with both tables created and UNI priced for three hours."""
    with connect(":memory:") as c:
        create_erc20_tables(c)
        create_price_tables(c)
        _insert_price(c, ts=H0, price_usd=10.0, token_address=UNI)
        _insert_price(c, ts=H1, price_usd=12.0, token_address=UNI)
        _insert_price(c, ts=H2, price_usd=15.0, token_address=UNI)
        yield c


@pytest.fixture
def confirmed_uni_distributor(monkeypatch):
    """Promote UNI/ethereum to CONFIRMED so the AIRDROP_IN path is reachable.

    No production entry is CONFIRMED yet, so this is the only way to exercise it.
    Patches the registry dict rather than stubbing the lookup, so AirdropRecord's
    own invariants run too.
    """
    record = AirdropRecord(
        token_symbol="UNI",
        chain="ethereum",
        status=AirdropAttributionStatus.CONFIRMED,
        distributors=frozenset({DISTRIBUTOR}),
        note="Test fixture only — not a verified address.",
    )
    monkeypatch.setitem(airdrop_registry._REGISTRY, ("UNI", "ethereum"), record)
    return DISTRIBUTOR


def _key(wallet: str, token: str = UNI, chain: str = "ethereum"):
    return (chain, wallet, token)


class TestReceiveThenSend:
    def test_receive_builds_a_lot(self, replay_db):
        _insert(replay_db, unique_id="in", block_timestamp=H0, block_number=1, log_index=0)
        partitions = _process_events(replay_db)

        state = partitions[_key(WALLET)]
        assert state.engine.balance_token() == Decimal(WEI)
        assert state.engine.avg_cost_basis_usd() == pytest.approx(10.0)
        assert state.event_count == 1

    def test_send_consumes_the_lot(self, replay_db):
        _insert(replay_db, unique_id="in", block_timestamp=H0, block_number=1, log_index=0)
        _insert(
            replay_db,
            unique_id="out",
            block_timestamp=H1,
            block_number=2,
            log_index=0,
            from_addr=WALLET,
            to_addr=OTHER,
        )
        partitions = _process_events(replay_db)

        sender = partitions[_key(WALLET)]
        assert sender.engine.balance_token() == Decimal(0)
        assert sender.has_insufficient_balance is False
        # Decision 3: the stack shrank but nothing was realized.
        assert sender.realizations == []

    def test_both_sides_of_a_transfer_get_partitions(self, replay_db):
        """One event, two wallets, each with its own engine."""
        _insert(replay_db, unique_id="in", block_timestamp=H0, block_number=1, log_index=0)
        partitions = _process_events(replay_db)
        assert _key(WALLET) in partitions  # receiver, holds a lot
        assert _key(OTHER) in partitions  # sender, tried to consume

    def test_partial_send_leaves_a_balance(self, replay_db):
        _insert(replay_db, unique_id="in", block_timestamp=H0, block_number=1, log_index=0)
        replay_db.execute(
            "INSERT INTO erc20_transfer VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["ethereum", 2, H1, "0x" + "a" * 64, 0, "out", UNI, WALLET, OTHER, str(WEI // 4), 18],
        )
        partitions = _process_events(replay_db)
        assert partitions[_key(WALLET)].engine.balance_token() == Decimal(WEI - WEI // 4)

    def test_cost_basis_is_priced_at_the_receipt_hour(self, replay_db):
        """Decision 2: two receipts at different hours take different costs."""
        _insert(replay_db, unique_id="a", block_timestamp=H0, block_number=1, log_index=0)
        _insert(replay_db, unique_id="b", block_timestamp=H2, block_number=2, log_index=0)
        state = _process_events(replay_db)[_key(WALLET)]
        # (10 + 15) / 2 across equal quantities.
        assert state.engine.avg_cost_basis_usd() == pytest.approx(12.5)
        assert [lot.unit_cost_usd for lot in state.engine.lots_snapshot()] == [10.0, 15.0]


class TestSelfTransfer:
    def test_applied_once_not_twice(self, replay_db):
        """from_addr == to_addr: iterating a pair would add the lot twice.

        The wallet set collapses to one entry, and classify_transfer returns
        SELF_TRANSFER for it — the single correct treatment.
        """
        _insert(
            replay_db,
            unique_id="self",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=WALLET,
            to_addr=WALLET,
        )
        partitions = _process_events(replay_db)
        state = partitions[_key(WALLET)]
        assert state.event_count == 1
        assert state.engine.balance_token() == Decimal(WEI)
        assert len(state.engine.lots_snapshot()) == 1

    def test_creates_only_one_partition(self, replay_db):
        _insert(
            replay_db,
            unique_id="self",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=WALLET,
            to_addr=WALLET,
        )
        assert len(_process_events(replay_db)) == 1

    def test_treated_as_trading_for_v1(self, replay_db):
        _insert(
            replay_db,
            unique_id="self",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=WALLET,
            to_addr=WALLET,
        )
        state = _process_events(replay_db)[_key(WALLET)]
        assert state.engine.balance_token_by_source()["trading"] == Decimal(WEI)
        assert state.engine.balance_token_by_source()["airdrop"] == Decimal(0)


class TestTwoWalletsOverlapping:
    def test_partitions_are_independent(self, replay_db):
        """A chain of transfers: OTHER -> WALLET -> THIRD."""
        _insert(
            replay_db,
            unique_id="a",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=OTHER,
            to_addr=WALLET,
        )
        _insert(
            replay_db,
            unique_id="b",
            block_timestamp=H1,
            block_number=2,
            log_index=0,
            from_addr=WALLET,
            to_addr=THIRD,
        )
        partitions = _process_events(replay_db)

        assert partitions[_key(WALLET)].engine.balance_token() == Decimal(0)
        assert partitions[_key(THIRD)].engine.balance_token() == Decimal(WEI)
        # THIRD received at H1, so its cost basis is that hour's price.
        assert partitions[_key(THIRD)].engine.avg_cost_basis_usd() == pytest.approx(12.0)

    def test_separate_tokens_get_separate_partitions(self, replay_db):
        """FIFO is only meaningful within one asset (decision 8)."""
        _insert_price(replay_db, ts=H0, price_usd=180.0, token_address=AAVE)
        _insert(replay_db, unique_id="uni", block_timestamp=H0, block_number=1, log_index=0)
        _insert(
            replay_db,
            unique_id="aave",
            block_timestamp=H0,
            block_number=2,
            log_index=0,
            token_address=AAVE,
        )
        partitions = _process_events(replay_db)
        assert _key(WALLET, UNI) in partitions
        assert _key(WALLET, AAVE) in partitions
        assert partitions[_key(WALLET, AAVE)].engine.avg_cost_basis_usd() == pytest.approx(180.0)

    def test_separate_chains_get_separate_partitions(self, replay_db):
        _insert_price(replay_db, ts=H0, price_usd=9.0, token_address=UNI, chain="base")
        _insert(replay_db, unique_id="eth", block_timestamp=H0, block_number=1, log_index=0)
        _insert(
            replay_db,
            unique_id="base",
            block_timestamp=H0,
            block_number=2,
            log_index=0,
            chain="base",
        )
        partitions = _process_events(replay_db)
        assert partitions[_key(WALLET, UNI, "ethereum")].engine.avg_cost_basis_usd() == (
            pytest.approx(10.0)
        )
        assert partitions[_key(WALLET, UNI, "base")].engine.avg_cost_basis_usd() == (
            pytest.approx(9.0)
        )


class TestUnpriceableFlag:
    def test_missing_price_flips_the_flag(self, replay_db):
        """H0+5h has no price row — the MKR-grid shape (decision 6)."""
        _insert(
            replay_db,
            unique_id="unpriced",
            block_timestamp=H0 + timedelta(hours=5),
            block_number=1,
            log_index=0,
        )
        state = _process_events(replay_db)[_key(WALLET)]
        assert state.has_unpriceable_events is True
        assert state.unpriced_event_count == 1

    def test_unpriced_receipt_creates_no_lot(self, replay_db):
        """No cost is invented, so no lot — the event is counted and flagged."""
        _insert(
            replay_db,
            unique_id="unpriced",
            block_timestamp=H0 + timedelta(hours=5),
            block_number=1,
            log_index=0,
        )
        state = _process_events(replay_db)[_key(WALLET)]
        assert state.engine.balance_token() == Decimal(0)
        assert state.event_count == 1

    def test_flag_is_false_when_everything_is_priced(self, replay_db):
        _insert(replay_db, unique_id="in", block_timestamp=H0, block_number=1, log_index=0)
        state = _process_events(replay_db)[_key(WALLET)]
        assert state.has_unpriceable_events is False
        assert state.unpriced_event_count == 0

    def test_counts_distinguish_one_gap_from_many(self, replay_db):
        """A bare boolean cannot tell 1-in-500 from 480-in-500."""
        for i in range(3):
            _insert(
                replay_db,
                unique_id=f"u{i}",
                block_timestamp=H0 + timedelta(hours=5 + i),
                block_number=i,
                log_index=0,
            )
        _insert(replay_db, unique_id="ok", block_timestamp=H0, block_number=99, log_index=0)
        state = _process_events(replay_db)[_key(WALLET)]
        assert state.unpriced_event_count == 3
        assert state.event_count == 4

    def test_an_unpriced_send_does_not_flag(self, replay_db):
        """OUT takes no cost basis (decision 3), so a missing price is not a gap."""
        _insert(replay_db, unique_id="in", block_timestamp=H0, block_number=1, log_index=0)
        _insert(
            replay_db,
            unique_id="out",
            block_timestamp=H0 + timedelta(hours=5),
            block_number=2,
            log_index=0,
            from_addr=WALLET,
            to_addr=OTHER,
        )
        state = _process_events(replay_db)[_key(WALLET)]
        assert state.has_unpriceable_events is False
        assert state.engine.balance_token() == Decimal(0)


class TestAirdropEntry:
    def test_distributor_receipt_is_zero_cost_airdrop(self, replay_db, confirmed_uni_distributor):
        _insert(
            replay_db,
            unique_id="drop",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=confirmed_uni_distributor,
            to_addr=WALLET,
        )
        state = _process_events(replay_db)[_key(WALLET)]
        assert state.engine.balance_token_by_source()["airdrop"] == Decimal(WEI)
        assert state.engine.avg_cost_basis_usd() == 0.0

    def test_airdrop_ignores_the_resolved_price(self, replay_db, confirmed_uni_distributor):
        """H0 has a $10 price; an airdrop lot must still be zero-cost."""
        _insert(
            replay_db,
            unique_id="drop",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=confirmed_uni_distributor,
            to_addr=WALLET,
        )
        state = _process_events(replay_db)[_key(WALLET)]
        assert state.engine.lots_snapshot()[0].unit_cost_usd == 0.0
        assert state.engine.lots_snapshot()[0].source == "airdrop"

    def test_mixed_airdrop_and_trading_lots(self, replay_db, confirmed_uni_distributor):
        _insert(
            replay_db,
            unique_id="drop",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=confirmed_uni_distributor,
            to_addr=WALLET,
        )
        _insert(
            replay_db,
            unique_id="buy",
            block_timestamp=H1,
            block_number=2,
            log_index=0,
            from_addr=OTHER,
            to_addr=WALLET,
        )
        state = _process_events(replay_db)[_key(WALLET)]
        assert state.engine.balance_token_by_source() == {
            "airdrop": Decimal(WEI),
            "trading": Decimal(WEI),
        }
        # (0 + 12) / 2 — airdrop lots pull the average down proportionally.
        assert state.engine.avg_cost_basis_usd() == pytest.approx(6.0)

    def test_non_distributor_sender_of_the_same_token_is_trading(
        self, replay_db, confirmed_uni_distributor
    ):
        _insert(
            replay_db,
            unique_id="buy",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=OTHER,
            to_addr=WALLET,
        )
        state = _process_events(replay_db)[_key(WALLET)]
        assert state.engine.balance_token_by_source()["trading"] == Decimal(WEI)


class TestInsufficientBalance:
    def test_caught_and_flagged_not_raised(self, replay_db):
        """A wallet sending tokens the stack never received — the pre-window shape."""
        _insert(
            replay_db,
            unique_id="out",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=WALLET,
            to_addr=OTHER,
        )
        partitions = _process_events(replay_db)
        state = partitions[_key(WALLET)]
        assert state.has_insufficient_balance is True
        assert state.insufficient_balance_count == 1

    def test_processing_continues_after_the_error(self, replay_db):
        """One bad event must not abandon the rest of the replay."""
        _insert(
            replay_db,
            unique_id="bad",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=WALLET,
            to_addr=OTHER,
        )
        _insert(
            replay_db,
            unique_id="good",
            block_timestamp=H1,
            block_number=2,
            log_index=0,
            from_addr=OTHER,
            to_addr=WALLET,
        )
        state = _process_events(replay_db)[_key(WALLET)]
        assert state.has_insufficient_balance is True
        # The later receipt still landed.
        assert state.engine.balance_token() == Decimal(WEI)
        assert state.event_count == 2

    def test_logged_with_the_diagnostic_values(self, replay_db, caplog):
        _insert(
            replay_db,
            unique_id="out",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=WALLET,
            to_addr=OTHER,
        )
        with caplog.at_level(logging.WARNING):
            _process_events(replay_db)
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "Insufficient balance" in combined
        assert WALLET in combined
        assert UNI in combined
        assert "ethereum" in combined

    def test_other_partitions_are_unaffected(self, replay_db):
        _insert(
            replay_db,
            unique_id="bad",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=WALLET,
            to_addr=OTHER,
        )
        partitions = _process_events(replay_db)
        assert partitions[_key(OTHER)].has_insufficient_balance is False
        assert partitions[_key(OTHER)].engine.balance_token() == Decimal(WEI)

    def test_counts_accumulate(self, replay_db):
        for i in range(3):
            _insert(
                replay_db,
                unique_id=f"out{i}",
                block_timestamp=H0,
                block_number=i,
                log_index=i,
                from_addr=WALLET,
                to_addr=OTHER,
            )
        state = _process_events(replay_db)[_key(WALLET)]
        assert state.insufficient_balance_count == 3


class TestWalletFilter:
    def test_restricts_which_partitions_are_built(self, replay_db):
        _insert(replay_db, unique_id="in", block_timestamp=H0, block_number=1, log_index=0)
        partitions = _process_events(replay_db, wallet_filter=[WALLET])
        assert set(partitions) == {_key(WALLET)}

    def test_is_case_insensitive(self, replay_db):
        _insert(replay_db, unique_id="in", block_timestamp=H0, block_number=1, log_index=0)
        partitions = _process_events(replay_db, wallet_filter=[WALLET.upper()])
        assert set(partitions) == {_key(WALLET)}

    def test_empty_filter_builds_nothing(self, replay_db):
        _insert(replay_db, unique_id="in", block_timestamp=H0, block_number=1, log_index=0)
        assert _process_events(replay_db, wallet_filter=[]) == {}

    def test_none_builds_every_wallet(self, replay_db):
        _insert(replay_db, unique_id="in", block_timestamp=H0, block_number=1, log_index=0)
        assert len(_process_events(replay_db)) == 2

    def test_chain_filter_restricts(self, replay_db):
        _insert_price(replay_db, ts=H0, price_usd=9.0, token_address=UNI, chain="base")
        _insert(replay_db, unique_id="eth", block_timestamp=H0, block_number=1, log_index=0)
        _insert(
            replay_db,
            unique_id="base",
            block_timestamp=H0,
            block_number=2,
            log_index=0,
            chain="base",
        )
        partitions = _process_events(replay_db, chains=["base"])
        assert {c for c, _w, _t in partitions} == {"base"}


class TestOrderingMatters:
    def test_fifo_consumes_the_older_lot_first(self, replay_db):
        """The engine does not sort; the reader's order is what makes this right."""
        _insert(replay_db, unique_id="a", block_timestamp=H0, block_number=1, log_index=0)
        _insert(replay_db, unique_id="b", block_timestamp=H1, block_number=2, log_index=0)
        _insert(
            replay_db,
            unique_id="out",
            block_timestamp=H2,
            block_number=3,
            log_index=0,
            from_addr=WALLET,
            to_addr=OTHER,
        )
        state = _process_events(replay_db)[_key(WALLET)]
        # The $10 lot went first, leaving the $12 one.
        assert state.engine.avg_cost_basis_usd() == pytest.approx(12.0)

    def test_replay_is_deterministic(self, replay_db):
        _insert(replay_db, unique_id="a", block_timestamp=H0, block_number=1, log_index=0)
        _insert(replay_db, unique_id="b", block_timestamp=H1, block_number=2, log_index=0)
        first = _process_events(replay_db)[_key(WALLET)].engine.avg_cost_basis_usd()
        second = _process_events(replay_db)[_key(WALLET)].engine.avg_cost_basis_usd()
        assert first == second


class TestEmptyAndEdgeCases:
    def test_empty_cache_yields_no_partitions(self, replay_db):
        assert _process_events(replay_db) == {}

    def test_token_outside_the_v1_registry_is_trading(self, replay_db):
        """_token_symbol_for returns a sentinel that matches no registry entry."""
        unknown = "0x" + "7" * 40
        _insert_price(replay_db, ts=H0, price_usd=1.0, token_address=unknown)
        _insert(
            replay_db,
            unique_id="in",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            token_address=unknown,
        )
        state = _process_events(replay_db)[_key(WALLET, unknown)]
        assert state.engine.balance_token_by_source()["trading"] == Decimal(WEI)

    def test_decimals_conflict_warns_and_keeps_the_first(self, replay_db, caplog):
        """Two rows reporting different decimals for one token: one set is misscaled."""
        _insert(
            replay_db,
            unique_id="a",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            token_decimals=18,
        )
        _insert(
            replay_db,
            unique_id="b",
            block_timestamp=H1,
            block_number=2,
            log_index=0,
            token_decimals=6,
        )
        with caplog.at_level(logging.WARNING):
            state = _process_events(replay_db)[_key(WALLET)]
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "token_decimals conflict" in combined
        assert state.engine.token_decimals == 18

    def test_logs_the_partition_summary(self, replay_db, caplog):
        _insert(replay_db, unique_id="in", block_timestamp=H0, block_number=1, log_index=0)
        with caplog.at_level(logging.INFO):
            _process_events(replay_db)
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "Replayed into 2 partition(s)" in combined


class TestPoolDestinationRealization:
    """ADR 0014: an OUT to a known V3 pool books a realization."""

    def test_buy_then_sell_to_pool_books_pnl(self, replay_db):
        """Bought at $10 (H0), sold to the pool at $12 (H1) → $2 on one token."""
        _insert(replay_db, unique_id="buy", block_timestamp=H0, block_number=1, log_index=0)
        _insert(
            replay_db,
            unique_id="sell",
            block_timestamp=H1,
            block_number=2,
            log_index=0,
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )
        state = _process_events(replay_db)[_key(WALLET)]

        assert len(state.realizations) == 1
        realization = state.realizations[0]
        assert realization.qty_token == Decimal(WEI)
        assert realization.unit_cost_usd == 10.0
        assert realization.unit_sale_usd == 12.0
        assert realization.pnl_usd == pytest.approx(2.0)
        assert realization.source == "trading"
        assert state.engine.balance_token() == Decimal(0)

    def test_realization_timestamp_is_the_sale_not_the_purchase(self, replay_db):
        """Decision 9 slices windows on realized_at, so this must be the sell."""
        _insert(replay_db, unique_id="buy", block_timestamp=H0, block_number=1, log_index=0)
        _insert(
            replay_db,
            unique_id="sell",
            block_timestamp=H1,
            block_number=2,
            log_index=0,
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )
        state = _process_events(replay_db)[_key(WALLET)]
        assert state.realizations[0].realized_at == H1

    def test_partial_lot_realization(self, replay_db):
        """Buy 100 @ $8, sell 60 to the pool @ $12 → $240, 40 left at $8."""
        _insert_price(replay_db, ts=H0, price_usd=8.0, token_address=AAVE)
        _insert_price(replay_db, ts=H1, price_usd=12.0, token_address=AAVE)
        _insert_qty(
            replay_db,
            unique_id="buy",
            block_timestamp=H0,
            block_number=1,
            qty=100 * WEI,
            token_address=AAVE,
            from_addr=OTHER,
            to_addr=WALLET,
        )
        _insert_qty(
            replay_db,
            unique_id="sell",
            block_timestamp=H1,
            block_number=2,
            qty=60 * WEI,
            token_address=AAVE,
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )
        state = _process_events(replay_db)[_key(WALLET, AAVE)]

        assert len(state.realizations) == 1
        assert state.realizations[0].qty_token == Decimal(60 * WEI)
        assert state.realizations[0].pnl_usd == pytest.approx(240.0)
        assert state.engine.balance_token() == Decimal(40 * WEI)
        assert state.engine.avg_cost_basis_usd() == pytest.approx(8.0)

    def test_cross_source_consumption_splits_by_lot(self, replay_db, confirmed_uni_distributor):
        """Buy 50 UNI @ $10 (H0), airdrop 50 (H1, zero cost), sell 80 @ $15 (H2).

        FIFO is unified oldest-first (PR #33), so the trading lot goes first:
        50 at trading -> (15-10)*50 = $250, then 30 at airdrop -> (15-0)*30 = $450.
        This is ADR 0012 decision 4's split surviving into realized PnL, carried
        by each lot's source rather than by the transfer that realized it.
        """
        _insert_qty(
            replay_db,
            unique_id="buy",
            block_timestamp=H0,
            block_number=1,
            qty=50 * WEI,
            from_addr=OTHER,
            to_addr=WALLET,
        )
        _insert_qty(
            replay_db,
            unique_id="drop",
            block_timestamp=H1,
            block_number=2,
            qty=50 * WEI,
            from_addr=confirmed_uni_distributor,
            to_addr=WALLET,
        )
        _insert_qty(
            replay_db,
            unique_id="sell",
            block_timestamp=H2,
            block_number=3,
            qty=80 * WEI,
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )
        state = _process_events(replay_db)[_key(WALLET)]

        assert len(state.realizations) == 2
        first, second = state.realizations
        assert (first.source, first.qty_token) == ("trading", Decimal(50 * WEI))
        assert first.pnl_usd == pytest.approx(250.0)
        assert (second.source, second.qty_token) == ("airdrop", Decimal(30 * WEI))
        assert second.pnl_usd == pytest.approx(450.0)
        # 20 airdrop tokens remain; the trading sub-stack is exhausted.
        assert state.engine.balance_token_by_source() == {
            "trading": Decimal(0),
            "airdrop": Decimal(20 * WEI),
        }

    def test_no_price_books_nothing_but_still_reduces_the_stack(self, replay_db, caplog):
        """A pool OUT at an unpriced hour.

        The stack must still shrink: the wallet genuinely sent the tokens, and
        leaving them would overstate the balance and let a later realization
        consume lots that were already gone.
        """
        _insert(replay_db, unique_id="buy", block_timestamp=H0, block_number=1, log_index=0)
        _insert(
            replay_db,
            unique_id="sell",
            block_timestamp=H0 + timedelta(hours=9),
            block_number=2,
            log_index=0,
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )

        with caplog.at_level(logging.WARNING):
            state = _process_events(replay_db)[_key(WALLET)]

        assert state.realizations == []
        assert state.has_unpriceable_events is True
        assert state.unpriced_event_count == 1
        assert state.engine.balance_token() == Decimal(0)

        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "Pool-destination transfer with no price" in combined
        assert UNI_WETH_POOL in combined
        assert "PnL is understated" in combined

    def test_insufficient_balance_on_a_pool_out(self, replay_db, caplog):
        """Same handling as the sentinel path — both route through consume()."""
        _insert(
            replay_db,
            unique_id="sell",
            block_timestamp=H1,
            block_number=1,
            log_index=0,
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )
        with caplog.at_level(logging.WARNING):
            state = _process_events(replay_db)[_key(WALLET)]

        assert state.has_insufficient_balance is True
        assert state.insufficient_balance_count == 1
        assert state.realizations == []
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "Insufficient balance" in combined

    def test_replay_continues_after_an_insufficient_pool_sale(self, replay_db):
        _insert(
            replay_db,
            unique_id="bad",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )
        _insert(replay_db, unique_id="buy", block_timestamp=H1, block_number=2, log_index=0)
        state = _process_events(replay_db)[_key(WALLET)]
        assert state.has_insufficient_balance is True
        assert state.engine.balance_token() == Decimal(WEI)

    def test_non_pool_out_books_nothing_even_when_priced(self, replay_db):
        """Decision 3 is unchanged: the price is irrelevant to an unknown counterparty."""
        _insert(replay_db, unique_id="buy", block_timestamp=H0, block_number=1, log_index=0)
        _insert(
            replay_db,
            unique_id="send",
            block_timestamp=H1,
            block_number=2,
            log_index=0,
            from_addr=WALLET,
            to_addr=OTHER,
        )
        state = _process_events(replay_db)[_key(WALLET)]

        assert state.realizations == []
        assert state.has_unpriceable_events is False
        # The stack still shrank — decision 3 reduces without realizing.
        assert state.engine.balance_token() == Decimal(0)

    def test_pool_on_the_wrong_chain_books_nothing(self, replay_db):
        """(chain, address) is the key; an Ethereum pool address on Base is not a pool."""
        _insert_price(replay_db, ts=H0, price_usd=10.0, token_address=UNI, chain="base")
        _insert_price(replay_db, ts=H1, price_usd=12.0, token_address=UNI, chain="base")
        _insert(
            replay_db,
            unique_id="buy",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            chain="base",
        )
        _insert(
            replay_db,
            unique_id="send",
            block_timestamp=H1,
            block_number=2,
            log_index=0,
            chain="base",
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )
        state = _process_events(replay_db)[_key(WALLET, UNI, "base")]
        assert state.realizations == []

    def test_incoming_from_a_pool_is_an_acquisition(self, replay_db):
        """The output leg of a swap builds a lot rather than booking a sale."""
        _insert(
            replay_db,
            unique_id="swap_out",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=UNI_WETH_POOL,
            to_addr=WALLET,
        )
        state = _process_events(replay_db)[_key(WALLET)]
        assert state.realizations == []
        assert state.engine.balance_token() == Decimal(WEI)
        assert state.engine.avg_cost_basis_usd() == pytest.approx(10.0)

    def test_multiple_sales_accumulate_in_order(self, replay_db):
        _insert_qty(
            replay_db,
            unique_id="buy",
            block_timestamp=H0,
            block_number=1,
            qty=2 * WEI,
            from_addr=OTHER,
            to_addr=WALLET,
        )
        _insert_qty(
            replay_db,
            unique_id="s1",
            block_timestamp=H1,
            block_number=2,
            qty=WEI,
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )
        _insert_qty(
            replay_db,
            unique_id="s2",
            block_timestamp=H2,
            block_number=3,
            qty=WEI,
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )
        state = _process_events(replay_db)[_key(WALLET)]
        assert [r.realized_at for r in state.realizations] == [H1, H2]
        assert [r.unit_sale_usd for r in state.realizations] == [12.0, 15.0]

    def test_loss_is_booked_as_a_negative(self, replay_db):
        """Bought at $15 (H2), sold at $10 (H0) is impossible chronologically, so
        buy at H2 and sell later at an hour priced lower."""
        _insert_price(replay_db, ts=H2 + timedelta(hours=1), price_usd=5.0, token_address=UNI)
        _insert(replay_db, unique_id="buy", block_timestamp=H2, block_number=1, log_index=0)
        _insert(
            replay_db,
            unique_id="sell",
            block_timestamp=H2 + timedelta(hours=1),
            block_number=2,
            log_index=0,
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )
        state = _process_events(replay_db)[_key(WALLET)]
        assert state.realizations[0].pnl_usd == pytest.approx(-10.0)

    def test_pool_partition_also_exists_and_books_nothing(self, replay_db):
        """The pool is the receiver, so it gets a partition of its own — a lot,
        not a realization. Harmless, and filtered out by wallet_filter in practice."""
        _insert(replay_db, unique_id="buy", block_timestamp=H0, block_number=1, log_index=0)
        _insert(
            replay_db,
            unique_id="sell",
            block_timestamp=H1,
            block_number=2,
            log_index=0,
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )
        partitions = _process_events(replay_db)
        pool_state = partitions[_key(UNI_WETH_POOL)]
        assert pool_state.realizations == []
        assert pool_state.engine.balance_token() == Decimal(WEI)


class TestWalletPnLEmission:
    """Step D: per-partition engine state sliced into per-window rows."""

    def test_empty_cache_yields_nothing(self, replay_db):
        assert list(compute_wallet_pnl(replay_db)) == []

    def test_trading_in_only_gives_two_open_rows(self, replay_db):
        _insert(replay_db, unique_id="buy", block_timestamp=H0, block_number=1, log_index=0)
        rows = [r for r in compute_wallet_pnl(replay_db, as_of=H2) if r.wallet == WALLET]

        assert len(rows) == 2
        assert {r.window_end - r.window_start for r in rows} == {
            timedelta(days=30),
            timedelta(days=90),
        }
        for row in rows:
            assert row.realized_pnl_usd == 0.0
            assert row.realization_count == 0
            assert row.bought_usd == pytest.approx(10.0)
            assert row.balance_token == Decimal(WEI)
            assert row.avg_cost_basis_usd == pytest.approx(10.0)
            assert row.unrealized_pnl_usd is None

    def test_full_sale_closes_the_position(self, replay_db):
        _insert(replay_db, unique_id="buy", block_timestamp=H0, block_number=1, log_index=0)
        _insert(
            replay_db,
            unique_id="sell",
            block_timestamp=H1,
            block_number=2,
            log_index=0,
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )
        rows = [r for r in compute_wallet_pnl(replay_db, as_of=H2) if r.wallet == WALLET]

        assert len(rows) == 2
        for row in rows:
            assert row.realized_pnl_usd == pytest.approx(2.0)
            assert row.realized_pnl_trading_usd == pytest.approx(2.0)
            assert row.realized_pnl_airdrop_usd == 0.0
            assert row.realization_count == 1
            assert row.sold_usd == pytest.approx(12.0)
            assert row.balance_token == Decimal(0)
            # None, not 0.0 — an empty stack has no cost basis to average.
            assert row.avg_cost_basis_usd is None


class TestWindowSlicing:
    """ADR 0012 decision 9: one engine state, windows differ by what they count."""

    @pytest.fixture
    def spanning_db(self):
        """Buy at day -60 @ $10, sell at day -20 @ $12, as_of = day 0."""
        as_of = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)
        buy_at = as_of - timedelta(days=60)
        sell_at = as_of - timedelta(days=20)
        with connect(":memory:") as c:
            create_erc20_tables(c)
            create_price_tables(c)
            _insert_price(c, ts=buy_at, price_usd=10.0, token_address=UNI)
            _insert_price(c, ts=sell_at, price_usd=12.0, token_address=UNI)
            _insert(c, unique_id="buy", block_timestamp=buy_at, block_number=1, log_index=0)
            _insert(
                c,
                unique_id="sell",
                block_timestamp=sell_at,
                block_number=2,
                log_index=0,
                from_addr=WALLET,
                to_addr=UNI_WETH_POOL,
            )
            yield c, as_of

    @staticmethod
    def _by_window(rows):
        return {(r.window_end - r.window_start).days: r for r in rows}

    def test_both_windows_see_the_realization(self, spanning_db):
        conn, as_of = spanning_db
        rows = self._by_window(
            r for r in compute_wallet_pnl(conn, as_of=as_of) if r.wallet == WALLET
        )
        assert rows[30].realized_pnl_trading_usd == pytest.approx(2.0)
        assert rows[90].realized_pnl_trading_usd == pytest.approx(2.0)

    def test_bought_usd_differs_by_window(self, spanning_db):
        """The day-60 acquisition is outside 30d and inside 90d."""
        conn, as_of = spanning_db
        rows = self._by_window(
            r for r in compute_wallet_pnl(conn, as_of=as_of) if r.wallet == WALLET
        )
        assert rows[30].bought_usd == 0.0
        assert rows[90].bought_usd == pytest.approx(10.0)

    def test_cost_basis_is_not_reset_at_the_window_boundary(self, spanning_db):
        """The 30d row's PnL uses the real $10 cost despite the lot predating it.

        A window-isolated basis would see zero cost and book the whole $12 sale
        as profit — the concrete failure ADR 0012 rejects window isolation over.
        """
        conn, as_of = spanning_db
        rows = self._by_window(
            r for r in compute_wallet_pnl(conn, as_of=as_of) if r.wallet == WALLET
        )
        assert rows[30].realized_pnl_usd == pytest.approx(2.0)
        assert rows[30].realized_pnl_usd != pytest.approx(12.0)
        assert rows[30].sold_usd == pytest.approx(12.0)

    def test_pre_window_flag_differs_by_window(self, spanning_db):
        """Day -60 is before the 30d window and inside the 90d one."""
        conn, as_of = spanning_db
        rows = self._by_window(
            r for r in compute_wallet_pnl(conn, as_of=as_of) if r.wallet == WALLET
        )
        assert rows[30].has_pre_window_activity is True
        assert rows[90].has_pre_window_activity is False

    def test_window_bounds_are_set_correctly(self, spanning_db):
        conn, as_of = spanning_db
        rows = self._by_window(
            r for r in compute_wallet_pnl(conn, as_of=as_of) if r.wallet == WALLET
        )
        for days, row in rows.items():
            assert row.window_end == as_of
            assert row.window_start == as_of - timedelta(days=days)

    def test_window_with_no_activity_is_omitted(self):
        """A row of zeroes and "nothing happened here" are the same fact."""
        as_of = datetime(2026, 12, 1, tzinfo=UTC)
        old = as_of - timedelta(days=200)
        with connect(":memory:") as c:
            create_erc20_tables(c)
            create_price_tables(c)
            _insert_price(c, ts=old, price_usd=10.0, token_address=UNI)
            _insert(c, unique_id="buy", block_timestamp=old, block_number=1, log_index=0)
            rows = [r for r in compute_wallet_pnl(c, as_of=as_of) if r.wallet == WALLET]
        # Outside both windows, so no rows at all.
        assert rows == []


class TestEmissionFlags:
    def test_unpriceable_flag_set_per_window(self, replay_db):
        _insert(
            replay_db,
            unique_id="unpriced",
            block_timestamp=H0 + timedelta(hours=9),
            block_number=1,
            log_index=0,
        )
        rows = [
            r
            for r in compute_wallet_pnl(replay_db, as_of=H0 + timedelta(hours=10))
            if r.wallet == WALLET
        ]
        assert rows
        assert all(r.has_unpriceable_events is True for r in rows)

    def test_unpriceable_flag_false_when_all_priced(self, replay_db):
        _insert(replay_db, unique_id="buy", block_timestamp=H0, block_number=1, log_index=0)
        rows = [r for r in compute_wallet_pnl(replay_db, as_of=H2) if r.wallet == WALLET]
        assert all(r.has_unpriceable_events is False for r in rows)

    def test_insufficient_balance_implies_pre_window_activity(self, replay_db):
        """Sending tokens the stack never received can only mean the wallet
        acquired them before our data starts — evidence of pre-window activity
        even when every visible event falls inside the window.

        Paired with an in-window acquisition so a row is emitted at all; see
        test_sell_only_wallet_emits_no_row for why that pairing is needed.
        """
        _insert(
            replay_db,
            unique_id="sell",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )
        _insert(
            replay_db,
            unique_id="buy",
            block_timestamp=H1,
            block_number=2,
            log_index=0,
            from_addr=OTHER,
            to_addr=WALLET,
        )
        rows = [r for r in compute_wallet_pnl(replay_db, as_of=H2) if r.wallet == WALLET]
        assert rows
        assert all(r.has_pre_window_activity is True for r in rows)

    def test_sell_only_wallet_emits_no_row(self, replay_db):
        """A wallet whose entire visible history is selling a pre-window position
        produces NO row, so neither its PnL nor its caveat flag is visible.

        The emission rule is "any Realization OR any acquisition in-window", and
        a sale against an empty stack raises before producing either — so the
        partition exists and carries has_insufficient_balance, but nothing is
        emitted to carry it outward. Decision 5 excludes such wallets from the
        leaderboard anyway, so the ranking is unaffected; what is lost is the
        flag itself, which step 6's flag-distribution report would need.
        Documented here rather than worked around.
        """
        _insert(
            replay_db,
            unique_id="sell",
            block_timestamp=H1,
            block_number=1,
            log_index=0,
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )
        rows = [r for r in compute_wallet_pnl(replay_db, as_of=H2) if r.wallet == WALLET]
        assert rows == []

    def test_smart_wallet_signal_is_always_false_in_v1(self, replay_db):
        _insert(replay_db, unique_id="buy", block_timestamp=H0, block_number=1, log_index=0)
        rows = list(compute_wallet_pnl(replay_db, as_of=H2))
        assert rows
        assert all(r.has_smart_wallet_signal is False for r in rows)

    def test_airdrop_pnl_lands_in_its_own_column(self, replay_db, confirmed_uni_distributor):
        _insert(
            replay_db,
            unique_id="drop",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=confirmed_uni_distributor,
            to_addr=WALLET,
        )
        _insert(
            replay_db,
            unique_id="sell",
            block_timestamp=H1,
            block_number=2,
            log_index=0,
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )
        rows = [r for r in compute_wallet_pnl(replay_db, as_of=H2) if r.wallet == WALLET]
        for row in rows:
            assert row.realized_pnl_airdrop_usd == pytest.approx(12.0)
            assert row.realized_pnl_trading_usd == 0.0
            assert row.realized_pnl_usd == pytest.approx(12.0)
            # Free tokens, so nothing was bought.
            assert row.bought_usd == 0.0


class TestAsOf:
    def test_defaults_to_just_past_the_newest_indexed_block(self, replay_db):
        """One microsecond past the newest event, not the event's own instant.

        window_end is exclusive, so defaulting to MAX(block_timestamp) exactly
        would exclude every event in the newest block — and in a cache whose
        events share one timestamp, exclude all of them and report no activity.
        """
        _insert(replay_db, unique_id="buy", block_timestamp=H0, block_number=1, log_index=0)
        _insert(replay_db, unique_id="late", block_timestamp=H2, block_number=2, log_index=0)
        rows = list(compute_wallet_pnl(replay_db))
        assert rows
        assert {r.window_end for r in rows} == {H2 + timedelta(microseconds=1)}

    def test_default_includes_the_newest_event(self, replay_db):
        """The regression this guards: a single-event cache must not be empty."""
        _insert(replay_db, unique_id="only", block_timestamp=H0, block_number=1, log_index=0)
        rows = [r for r in compute_wallet_pnl(replay_db) if r.wallet == WALLET]
        assert rows
        assert all(r.bought_usd == pytest.approx(10.0) for r in rows)

    def test_default_is_the_data_edge_not_wall_clock(self, replay_db):
        """Using now() would open a gap between the last indexed block and the
        window end, and make the result depend on when it was run."""
        _insert(replay_db, unique_id="buy", block_timestamp=H0, block_number=1, log_index=0)
        rows = list(compute_wallet_pnl(replay_db))
        assert rows
        assert all(r.window_end == H0 + timedelta(microseconds=1) for r in rows)

    def test_explicit_as_of_excludes_later_events(self, replay_db):
        """Not applied-then-filtered: a future event must not enter the stack,
        or it would change which lots an in-window sale consumes."""
        _insert(replay_db, unique_id="buy", block_timestamp=H0, block_number=1, log_index=0)
        _insert(replay_db, unique_id="later", block_timestamp=H2, block_number=2, log_index=0)
        rows = [r for r in compute_wallet_pnl(replay_db, as_of=H1) if r.wallet == WALLET]
        assert rows
        for row in rows:
            # Only the H0 lot — the H2 one was never replayed.
            assert row.balance_token == Decimal(WEI)
            assert row.bought_usd == pytest.approx(10.0)

    def test_as_of_boundary_is_exclusive(self, replay_db):
        _insert(replay_db, unique_id="at_edge", block_timestamp=H1, block_number=1, log_index=0)
        assert list(compute_wallet_pnl(replay_db, as_of=H1)) == []

    def test_empty_cache_resolves_no_as_of(self, replay_db):
        assert list(compute_wallet_pnl(replay_db)) == []


class TestEmissionScope:
    def test_two_wallets_give_four_rows(self, replay_db):
        """Two independent wallets, two windows each, no cross-contamination."""
        _insert(
            replay_db,
            unique_id="a",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=OTHER,
            to_addr=WALLET,
        )
        _insert(
            replay_db,
            unique_id="b",
            block_timestamp=H1,
            block_number=2,
            log_index=0,
            from_addr=OTHER,
            to_addr=THIRD,
        )
        rows = [
            r
            for r in compute_wallet_pnl(replay_db, as_of=H2, wallet_filter=[WALLET, THIRD])
            if True
        ]
        assert len(rows) == 4
        assert {r.wallet for r in rows} == {WALLET, THIRD}
        for row in rows:
            assert row.balance_token == Decimal(WEI)

    def test_separate_tokens_get_separate_rows(self, replay_db):
        _insert_price(replay_db, ts=H0, price_usd=180.0, token_address=AAVE)
        _insert(replay_db, unique_id="uni", block_timestamp=H0, block_number=1, log_index=0)
        _insert(
            replay_db,
            unique_id="aave",
            block_timestamp=H0,
            block_number=2,
            log_index=0,
            token_address=AAVE,
        )
        rows = [r for r in compute_wallet_pnl(replay_db, as_of=H2, wallet_filter=[WALLET])]
        assert len(rows) == 4  # 2 tokens x 2 windows
        assert {r.token_address for r in rows} == {UNI, AAVE}

    def test_wallet_filter_restricts_emission(self, replay_db):
        _insert(replay_db, unique_id="buy", block_timestamp=H0, block_number=1, log_index=0)
        rows = list(compute_wallet_pnl(replay_db, as_of=H2, wallet_filter=[WALLET]))
        assert {r.wallet for r in rows} == {WALLET}

    def test_chain_filter_restricts_emission(self, replay_db):
        _insert_price(replay_db, ts=H0, price_usd=9.0, token_address=UNI, chain="base")
        _insert(replay_db, unique_id="eth", block_timestamp=H0, block_number=1, log_index=0)
        _insert(
            replay_db,
            unique_id="base",
            block_timestamp=H0,
            block_number=2,
            log_index=0,
            chain="base",
        )
        rows = list(compute_wallet_pnl(replay_db, as_of=H2, chains=["base"]))
        assert {r.chain for r in rows} == {"base"}

    def test_returns_a_generator(self, replay_db):
        _insert(replay_db, unique_id="buy", block_timestamp=H0, block_number=1, log_index=0)
        result = compute_wallet_pnl(replay_db, as_of=H2)
        assert not isinstance(result, list)
        assert isinstance(next(iter(result)), WalletPnL)


class TestReproducibility:
    def test_two_runs_are_identical_modulo_computed_at(self, replay_db):
        """Same cache, same rows. ADR 0012 consequence 6 says a figure may change
        when earlier events are re-fetched — not when nothing changed at all.
        """
        _insert(replay_db, unique_id="buy", block_timestamp=H0, block_number=1, log_index=0)
        _insert(
            replay_db,
            unique_id="sell",
            block_timestamp=H1,
            block_number=2,
            log_index=0,
            from_addr=WALLET,
            to_addr=UNI_WETH_POOL,
        )

        def snapshot():
            return [
                r.model_dump(exclude={"computed_at"})
                for r in compute_wallet_pnl(replay_db, as_of=H2)
            ]

        assert snapshot() == snapshot()

    def test_row_order_is_deterministic(self, replay_db):
        _insert(
            replay_db,
            unique_id="a",
            block_timestamp=H0,
            block_number=1,
            log_index=0,
            from_addr=OTHER,
            to_addr=WALLET,
        )
        _insert(
            replay_db,
            unique_id="b",
            block_timestamp=H0,
            block_number=2,
            log_index=1,
            from_addr=OTHER,
            to_addr=THIRD,
        )
        first = [(r.wallet, r.window_start) for r in compute_wallet_pnl(replay_db, as_of=H2)]
        second = [(r.wallet, r.window_start) for r in compute_wallet_pnl(replay_db, as_of=H2)]
        assert first == second
