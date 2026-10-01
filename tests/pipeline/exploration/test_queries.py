"""Tests for the wallet activity proof's query and composition layer.

The fixture cache is hand-built and deliberately noisy: it holds rows on the
wrong chain, for the wrong token, in an untracked pool and for other wallets, so
every WHERE clause has something to exclude. A query that dropped its filters
would still return rows here — it would just return the wrong ones.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alphawallets.db import connect
from alphawallets.fetchers.erc20.writer import create_tables as create_erc20_tables
from alphawallets.fetchers.prices.writer import create_tables as create_price_tables
from alphawallets.fetchers.uniswap_v3.writer import create_tables as create_swap_tables
from alphawallets.pipeline.exploration.queries import (
    POOL_TOKEN_LAYOUT,
    _swap_direction,
    _to_whole_units,
    assemble_timeline,
    hour_of,
    pools_holding_token,
    query_most_active_wallet,
    query_prices_for_hours,
    query_wallet_swaps,
    query_wallet_transfers,
    token_slot,
)

UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
UNI_POOL = "0x1d42064fc4beb5f8aaf85f4617ae8b3b5b8bd801"
USDC_POOL = "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"
UNTRACKED_POOL = "0x" + "9" * 40

WALLET = "0x" + "1" * 40
OTHER_WALLET = "0x" + "2" * 40
COUNTERPARTY = "0x" + "3" * 40

WEI = 10**18

# Fixed clock. 06:38Z sits inside hour 06, so hour 06 is the current incomplete
# hour — the shape of the first live run, where the newest price row was 05:00Z.
NOW = datetime(2026, 10, 1, 6, 38, 11, tzinfo=UTC)
H03 = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)
H04 = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
H05 = datetime(2026, 10, 1, 5, 0, tzinfo=UTC)
H06 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)


def _tx(n: int) -> str:
    return "0x" + f"{n:064x}"


def _activity_counts(conn) -> dict[str, int]:
    """Count rows per address the way the auto-pick query defines activity.

    Computed here in Python so the expected winner is derived from the fixture
    instead of guessed — a hardcoded address silently becomes wrong the moment a
    row is added.
    """
    counts: Counter[str] = Counter()
    for (tx_from,) in conn.execute(
        "SELECT lower(tx_from) FROM uniswap_v3_swap "
        "WHERE chain = 'ethereum' AND lower(pool_address) = ?",
        [UNI_POOL],
    ).fetchall():
        counts[tx_from] += 1
    for from_addr, to_addr in conn.execute(
        "SELECT lower(from_addr), lower(to_addr) FROM erc20_transfer "
        "WHERE chain = 'ethereum' AND lower(token_address) = ?",
        [UNI],
    ).fetchall():
        counts[from_addr] += 1
        counts[to_addr] += 1
    return dict(counts)


def _expected_most_active(conn) -> str:
    """The address the auto-pick should return: most rows, ties by address order."""
    counts = _activity_counts(conn)
    return min(counts, key=lambda addr: (-counts[addr], addr))


@pytest.fixture
def cache():
    """An in-memory cache with all five tables and a deliberately mixed dataset.

    Events belonging to WALLET, all on ethereum, all UNI:
      03:17  swap  SELL 500 UNI  -> 1.5 WETH     (hour 03, priced)
      04:17  transfer IN  1234.56 UNI            (hour 04, priced)
      05:17  swap  BUY  200 UNI  <- 0.6 WETH     (hour 05, NO price row: unavailable)
      05:47  transfer OUT 300 UNI                (hour 05, unavailable)
      06:17  transfer IN  50 UNI                 (hour 06, current: pending)

    Noise that every filter must exclude: a Base transfer, a WETH transfer, a
    swap by OTHER_WALLET, a swap in an untracked pool, and a Base price row.
    """
    with connect(":memory:") as conn:
        create_swap_tables(conn)
        create_erc20_tables(conn)
        create_price_tables(conn)

        def add_swap(ts, tx, log_index, pool, amount0, amount1, tx_from=WALLET, chain="ethereum"):
            conn.execute(
                "INSERT INTO uniswap_v3_swap VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    chain,
                    26094728,
                    ts,
                    tx,
                    log_index,
                    pool,
                    tx_from,
                    "0x" + "f" * 40,
                    "0x" + "e" * 40,
                    str(amount0),
                    str(amount1),
                    "1000",
                    "2000",
                    197275,
                ],
            )

        def add_transfer(
            ts,
            tx,
            log_index,
            from_addr,
            to_addr,
            value_raw,
            token=UNI,
            chain="ethereum",
            decimals=18,
        ):
            conn.execute(
                "INSERT INTO erc20_transfer VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    chain,
                    26094728,
                    ts,
                    tx,
                    log_index,
                    f"{tx}:{log_index}",
                    token,
                    from_addr,
                    to_addr,
                    str(value_raw),
                    decimals,
                ],
            )

        def add_price(ts, price, token=UNI, chain="ethereum"):
            conn.execute(
                "INSERT INTO token_price VALUES (?, ?, ?, ?, ?, ?, ?)",
                [chain, token, ts, price, 0.99, "defillama", NOW],
            )

        # --- the wallet's own activity ---
        # UNI is token0 here, so a positive amount0 means UNI went into the pool.
        add_swap(H03 + timedelta(minutes=17), _tx(1), 1, UNI_POOL, 500 * WEI, -15 * 10**17)
        add_transfer(H04 + timedelta(minutes=17), _tx(2), 1, COUNTERPARTY, WALLET, 123456 * 10**16)
        add_swap(H05 + timedelta(minutes=17), _tx(3), 1, UNI_POOL, -200 * WEI, 6 * 10**17)
        add_transfer(H05 + timedelta(minutes=47), _tx(4), 1, WALLET, COUNTERPARTY, 300 * WEI)
        add_transfer(H06 + timedelta(minutes=17), _tx(5), 1, COUNTERPARTY, WALLET, 50 * WEI)

        # --- noise ---
        add_transfer(
            H04 + timedelta(minutes=30), _tx(10), 1, COUNTERPARTY, WALLET, 999 * WEI, chain="base"
        )
        add_transfer(
            H04 + timedelta(minutes=31), _tx(11), 1, COUNTERPARTY, WALLET, 7 * WEI, token=WETH
        )
        add_swap(
            H04 + timedelta(minutes=32), _tx(12), 1, UNI_POOL, 1 * WEI, -1, tx_from=OTHER_WALLET
        )
        add_swap(H04 + timedelta(minutes=33), _tx(13), 1, UNTRACKED_POOL, 1 * WEI, -1)
        add_transfer(H03 + timedelta(minutes=5), _tx(14), 1, COUNTERPARTY, OTHER_WALLET, 2 * WEI)

        # --- prices: hours 03 and 04 only. Hour 05 is a real gap, hour 06 is current.
        add_price(H03, 8.87)
        add_price(H04, 8.91)
        add_price(H05, 99.99, chain="base")  # wrong chain, must not be used

        yield conn


# ---------- Layout helpers ----------


class TestPoolLayout:
    def test_every_entry_is_lowercase(self):
        """The cache stores lowercase; a checksummed key would never match."""
        for pool, layout in POOL_TOKEN_LAYOUT.items():
            assert pool == pool.lower()
            for slot in ("token0", "token1"):
                assert layout[slot]["address"] == layout[slot]["address"].lower()

    def test_pools_holding_uni(self):
        assert pools_holding_token(UNI) == [UNI_POOL]

    def test_pools_holding_weth_is_both(self):
        assert set(pools_holding_token(WETH)) == {UNI_POOL, USDC_POOL}

    def test_pools_holding_unknown_token_is_empty(self):
        assert pools_holding_token("0x" + "7" * 40) == []

    def test_pools_holding_token_accepts_checksummed_input(self):
        assert pools_holding_token(UNI.upper()) == [UNI_POOL]

    def test_token_slot_resolves_both_sides(self):
        assert token_slot(UNI_POOL, UNI) == "token0"
        assert token_slot(UNI_POOL, WETH) == "token1"
        assert token_slot(USDC_POOL, USDC) == "token0"
        assert token_slot(USDC_POOL, WETH) == "token1"

    def test_token_slot_rejects_untracked_pool(self):
        """Guessing a slot would invert every direction for that pool."""
        with pytest.raises(KeyError, match="POOL_TOKEN_LAYOUT"):
            token_slot(UNTRACKED_POOL, UNI)

    def test_token_slot_rejects_token_not_in_pool(self):
        with pytest.raises(KeyError, match="not in pool"):
            token_slot(USDC_POOL, UNI)


class TestSwapDirection:
    """Uniswap signs amounts from the pool's side: positive = into the pool."""

    @pytest.mark.parametrize(
        ("signed_amount", "expected"),
        [
            (500 * WEI, "sell"),  # token went into the pool
            (-500 * WEI, "buy"),  # token came out of the pool
            (1, "sell"),
            (-1, "buy"),
        ],
    )
    def test_direction_follows_sign(self, signed_amount, expected):
        assert _swap_direction(signed_amount) == expected

    @pytest.mark.parametrize("slot", ["token0", "token1"])
    @pytest.mark.parametrize(("sign", "expected"), [(1, "sell"), (-1, "buy")])
    def test_all_four_slot_sign_combinations(self, cache, slot, sign, expected):
        """token-is-token0 x amount-positive, through the real query path.

        The slot matters because the sign has to be read off the *tracked*
        token's amount; reading the other leg would invert every direction.
        """
        token = POOL_TOKEN_LAYOUT[UNI_POOL][slot]["address"]
        other = 100 * WEI if sign < 0 else -100 * WEI
        amounts = {slot: sign * 42 * WEI, "token1" if slot == "token0" else "token0": other}
        cache.execute(
            "INSERT INTO uniswap_v3_swap VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "ethereum",
                26094999,
                H04 + timedelta(minutes=50),
                _tx(99),
                7,
                UNI_POOL,
                WALLET,
                "0x" + "f" * 40,
                "0x" + "e" * 40,
                str(amounts["token0"]),
                str(amounts["token1"]),
                "1",
                "2",
                1,
            ],
        )

        swaps = query_wallet_swaps(cache, WALLET, "ethereum", token)
        added = next(s for s in swaps if s["tx_hash"] == _tx(99))
        assert _swap_direction(added["signed_amount"]) == expected


class TestToWholeUnits:
    def test_eighteen_decimals(self):
        assert _to_whole_units(str(500 * WEI), 18) == Decimal("500")

    def test_six_decimals(self):
        assert _to_whole_units("1500000", 6) == Decimal("1.5")

    def test_negative_becomes_absolute(self):
        """Sign carries direction, not magnitude — the amount is always positive."""
        assert _to_whole_units(str(-500 * WEI), 18) == Decimal("500")

    def test_exact_at_uint256_scale(self):
        """float would lose the low-order digits here; Decimal does not."""
        raw = "123456789012345678901"
        assert _to_whole_units(raw, 18) == Decimal("123.456789012345678901")


# ---------- Queries ----------


class TestQueryWalletSwaps:
    def test_returns_only_the_wallets_swaps(self, cache):
        swaps = query_wallet_swaps(cache, WALLET, "ethereum", UNI)
        assert [s["tx_hash"] for s in swaps] == [_tx(1), _tx(3)]

    def test_excludes_other_wallets(self, cache):
        swaps = query_wallet_swaps(cache, WALLET, "ethereum", UNI)
        assert _tx(12) not in [s["tx_hash"] for s in swaps]

    def test_excludes_untracked_pools(self, cache):
        """A pool with no verified layout cannot be interpreted, so it is skipped."""
        swaps = query_wallet_swaps(cache, WALLET, "ethereum", UNI)
        assert _tx(13) not in [s["tx_hash"] for s in swaps]

    def test_chain_filter_excludes_base(self, cache):
        assert query_wallet_swaps(cache, WALLET, "base", UNI) == []

    def test_uppercase_wallet_matches_lowercase_rows(self, cache):
        lower = query_wallet_swaps(cache, WALLET, "ethereum", UNI)
        upper = query_wallet_swaps(cache, WALLET.upper(), "ethereum", UNI)
        assert [s["tx_hash"] for s in upper] == [s["tx_hash"] for s in lower]

    def test_uppercase_token_matches(self, cache):
        assert len(query_wallet_swaps(cache, WALLET, "ethereum", UNI.upper())) == 2

    def test_unknown_token_returns_empty(self, cache):
        assert query_wallet_swaps(cache, WALLET, "ethereum", "0x" + "7" * 40) == []

    def test_amount_and_paired_leg_resolved(self, cache):
        swaps = query_wallet_swaps(cache, WALLET, "ethereum", UNI)
        sell = swaps[0]
        assert sell["amount_token"] == Decimal("500")
        assert sell["other_amount"] == Decimal("1.5")
        assert sell["other_token"] == "WETH"

    def test_signed_amount_preserves_sign(self, cache):
        swaps = query_wallet_swaps(cache, WALLET, "ethereum", UNI)
        assert swaps[0]["signed_amount"] > 0  # sell
        assert swaps[1]["signed_amount"] < 0  # buy

    def test_ordered_chronologically(self, cache):
        swaps = query_wallet_swaps(cache, WALLET, "ethereum", UNI)
        assert [s["ts"] for s in swaps] == sorted(s["ts"] for s in swaps)


class TestQueryWalletTransfers:
    def test_returns_both_directions(self, cache):
        transfers = query_wallet_transfers(cache, WALLET, "ethereum", UNI)
        assert [(t["tx_hash"], t["direction"]) for t in transfers] == [
            (_tx(2), "in"),
            (_tx(4), "out"),
            (_tx(5), "in"),
        ]

    def test_chain_filter_excludes_base_transfer(self, cache):
        transfers = query_wallet_transfers(cache, WALLET, "ethereum", UNI)
        assert _tx(10) not in [t["tx_hash"] for t in transfers]

    def test_base_chain_returns_only_the_base_row(self, cache):
        transfers = query_wallet_transfers(cache, WALLET, "base", UNI)
        assert [t["tx_hash"] for t in transfers] == [_tx(10)]

    def test_token_filter_excludes_weth(self, cache):
        transfers = query_wallet_transfers(cache, WALLET, "ethereum", UNI)
        assert _tx(11) not in [t["tx_hash"] for t in transfers]

    def test_excludes_other_wallets_transfers(self, cache):
        transfers = query_wallet_transfers(cache, WALLET, "ethereum", UNI)
        assert _tx(14) not in [t["tx_hash"] for t in transfers]

    def test_uppercase_wallet_matches_lowercase_rows(self, cache):
        lower = query_wallet_transfers(cache, WALLET, "ethereum", UNI)
        upper = query_wallet_transfers(cache, WALLET.upper(), "ethereum", UNI)
        assert [t["tx_hash"] for t in upper] == [t["tx_hash"] for t in lower]

    def test_counterparty_is_the_other_side(self, cache):
        transfers = query_wallet_transfers(cache, WALLET, "ethereum", UNI)
        assert all(t["counterparty"] == COUNTERPARTY for t in transfers)

    def test_amount_scaled_by_decimals(self, cache):
        transfers = query_wallet_transfers(cache, WALLET, "ethereum", UNI)
        assert transfers[0]["amount_token"] == Decimal("1234.56")

    def test_null_decimals_falls_back_to_18_and_is_flagged(self, cache):
        """token_decimals is nullable; the fallback must be visible, not silent."""
        cache.execute(
            "INSERT INTO erc20_transfer VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "ethereum",
                26094728,
                H04 + timedelta(minutes=55),
                _tx(20),
                1,
                f"{_tx(20)}:1",
                UNI,
                COUNTERPARTY,
                WALLET,
                str(7 * WEI),
                None,
            ],
        )
        transfers = query_wallet_transfers(cache, WALLET, "ethereum", UNI)
        added = next(t for t in transfers if t["tx_hash"] == _tx(20))
        assert added["amount_token"] == Decimal("7")
        assert added["decimals_assumed"] is True

    def test_decimals_assumed_false_when_present(self, cache):
        transfers = query_wallet_transfers(cache, WALLET, "ethereum", UNI)
        assert all(t["decimals_assumed"] is False for t in transfers)


class TestQueryPricesForHours:
    def test_returns_only_requested_hours(self, cache):
        prices = query_prices_for_hours(cache, "ethereum", UNI, {H03, H04})
        assert prices == {H03: 8.87, H04: 8.91}

    def test_missing_hour_is_absent_not_none(self, cache):
        """An absent key is the signal; a None value would be indistinguishable
        from a row that exists with no price."""
        prices = query_prices_for_hours(cache, "ethereum", UNI, {H03, H05})
        assert H05 not in prices
        assert prices == {H03: 8.87}

    def test_empty_hour_set_skips_the_query(self, cache):
        assert query_prices_for_hours(cache, "ethereum", UNI, set()) == {}

    def test_chain_filter_excludes_base_price(self, cache):
        """The Base row for hour 05 must not satisfy an Ethereum lookup."""
        prices = query_prices_for_hours(cache, "ethereum", UNI, {H05})
        assert prices == {}

    def test_uppercase_token_matches(self, cache):
        assert query_prices_for_hours(cache, "ethereum", UNI.upper(), {H03}) == {H03: 8.87}

    def test_keys_are_utc(self, cache):
        prices = query_prices_for_hours(cache, "ethereum", UNI, {H03})
        assert all(k.tzinfo is not None and k.utcoffset().total_seconds() == 0 for k in prices)


class TestQueryMostActiveWallet:
    def test_picks_the_busiest_wallet(self, cache):
        """WALLET has 5 rows; OTHER_WALLET has 2. Higher count wins."""
        assert query_most_active_wallet(cache, "ethereum", UNI) == WALLET

    def test_counts_swaps_and_transfers_together(self, cache):
        """Every address that moved the token counts, from both sides of a transfer.

        The expectation is computed from the fixture rather than hardcoded: the
        busiest address here is the shared counterparty, not the wallet under
        test, and asserting a guessed winner is how a correct query gets
        "fixed" into a wrong one.
        """
        expected = _expected_most_active(cache)
        assert query_most_active_wallet(cache, "ethereum", UNI) == expected
        # Sanity-check the fixture still exercises a real contest.
        assert _activity_counts(cache)[expected] > 1

    def test_a_dedicated_wallet_can_outrank_the_counterparty(self, cache):
        """Give one wallet enough rows to win outright, and it must be picked."""
        for i in range(20):
            cache.execute(
                "INSERT INTO erc20_transfer VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    "ethereum",
                    26094728,
                    H04 + timedelta(seconds=i),
                    _tx(50 + i),
                    1,
                    f"{_tx(50 + i)}:1",
                    UNI,
                    OTHER_WALLET,
                    "0x" + "4" * 40,
                    str(WEI),
                    18,
                ],
            )
        counts = _activity_counts(cache)
        assert counts[OTHER_WALLET] == max(counts.values())
        assert query_most_active_wallet(cache, "ethereum", UNI) == OTHER_WALLET

    def test_empty_cache_returns_none(self):
        with connect(":memory:") as conn:
            create_swap_tables(conn)
            create_erc20_tables(conn)
            create_price_tables(conn)
            assert query_most_active_wallet(conn, "ethereum", UNI) is None

    def test_unknown_token_returns_none(self, cache):
        assert query_most_active_wallet(cache, "ethereum", "0x" + "7" * 40) is None


# ---------- Composition ----------


class TestHourOf:
    def test_truncates_to_hour(self):
        assert hour_of(datetime(2026, 10, 1, 6, 38, 11, tzinfo=UTC)) == H06

    def test_converts_non_utc_to_utc_first(self):
        """A +05:30 timestamp must truncate on the UTC grid, not the local one."""
        from zoneinfo import ZoneInfo

        kolkata = datetime(2026, 10, 1, 6, 38, 11, tzinfo=UTC).astimezone(ZoneInfo("Asia/Kolkata"))
        assert hour_of(kolkata) == H06


class TestAssembleTimeline:
    @pytest.fixture
    def timeline(self, cache):
        swaps = query_wallet_swaps(cache, WALLET, "ethereum", UNI)
        transfers = query_wallet_transfers(cache, WALLET, "ethereum", UNI)
        hours = {hour_of(r["ts"]) for r in [*swaps, *transfers]}
        prices = query_prices_for_hours(cache, "ethereum", UNI, hours)
        return assemble_timeline(swaps, transfers, prices, now_utc=NOW)

    def test_all_events_included(self, timeline):
        """Unpriced events are still events — none may be dropped."""
        assert len(timeline) == 5

    def test_chronological_order(self, timeline):
        assert [e.ts for e in timeline] == sorted(e.ts for e in timeline)

    def test_swaps_and_transfers_interleaved(self, timeline):
        assert [e.event_type for e in timeline] == [
            "swap",
            "transfer",
            "swap",
            "transfer",
            "transfer",
        ]

    def test_directions(self, timeline):
        assert [e.direction for e in timeline] == ["sell", "in", "buy", "out", "in"]

    def test_price_status_buckets(self, timeline):
        statuses = [e.price_status for e in timeline]
        assert statuses == ["priced", "priced", "unavailable", "unavailable", "pending"]

    def test_priced_events_carry_value(self, timeline):
        sell = timeline[0]
        assert sell.price_usd == 8.87
        assert sell.value_usd == Decimal("500") * Decimal("8.87")

    def test_unavailable_event_has_no_price_or_value(self, timeline):
        """The deliberate hour-05 gap: the event survives, unpriced."""
        gap = timeline[2]
        assert gap.price_status == "unavailable"
        assert gap.price_usd is None
        assert gap.value_usd is None

    def test_pending_event_has_no_price_or_value(self, timeline):
        pending = timeline[-1]
        assert pending.price_status == "pending"
        assert pending.price_usd is None
        assert pending.value_usd is None

    def test_coverage_counters(self, timeline):
        """The summary arithmetic: pending is excluded from the denominator."""
        priced = sum(1 for e in timeline if e.price_status == "priced")
        pending = sum(1 for e in timeline if e.price_status == "pending")
        unavailable = sum(1 for e in timeline if e.price_status == "unavailable")
        assert (priced, pending, unavailable) == (2, 1, 2)
        assert priced + pending + unavailable == len(timeline)

    def test_swap_carries_paired_leg_transfer_does_not(self, timeline):
        swap = timeline[0]
        transfer = timeline[1]
        assert swap.other_token == "WETH"
        assert swap.counterparty is None
        assert transfer.other_token is None
        assert transfer.counterparty == COUNTERPARTY

    def test_empty_inputs_give_empty_timeline(self):
        assert assemble_timeline([], [], {}, now_utc=NOW) == []

    def test_order_is_stable_for_same_timestamp(self, cache):
        """Two events in one second must not reorder between runs."""
        swaps = query_wallet_swaps(cache, WALLET, "ethereum", UNI)
        transfers = query_wallet_transfers(cache, WALLET, "ethereum", UNI)
        for transfer in transfers:
            transfer["ts"] = swaps[0]["ts"]
        first = assemble_timeline(swaps, transfers, {}, now_utc=NOW)
        second = assemble_timeline(swaps, list(reversed(transfers)), {}, now_utc=NOW)
        assert [e.tx_hash for e in first] == [e.tx_hash for e in second]
