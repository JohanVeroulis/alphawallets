"""Shared fixture cache for the wallet activity proof tests.

The cache is hand-built and deliberately noisy: it holds rows on the wrong
chain, for the wrong token, in an untracked pool and for other wallets, so every
WHERE clause has something to exclude. A query that dropped its filters would
still return rows here — it would just return the wrong ones.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from alphawallets.db import connect
from alphawallets.fetchers.erc20.writer import create_tables as create_erc20_tables
from alphawallets.fetchers.prices.writer import create_tables as create_price_tables
from alphawallets.fetchers.uniswap_v3.writer import create_tables as create_swap_tables

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
