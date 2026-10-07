"""Tests for the PnL calculator's event reader (ADR 0012 step 4, skeleton)."""

import logging
from datetime import UTC, datetime, timedelta

import pytest

from alphawallets.db import connect
from alphawallets.fetchers.erc20.models import ERC20Transfer
from alphawallets.fetchers.erc20.writer import create_tables as create_erc20_tables
from alphawallets.fetchers.prices.writer import create_tables as create_price_tables
from alphawallets.pipeline.pnl.calculator import (
    TRANSFER_ORDER_BY,
    _fetch_transfer_events,
    _load_price_cache,
    _lookup_price,
)

WALLET = "0x" + "1" * 40
OTHER = "0x" + "2" * 40
THIRD = "0x" + "3" * 40

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
