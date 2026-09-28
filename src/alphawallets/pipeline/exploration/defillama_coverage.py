"""DefiLlama historical price coverage investigation.

Tests whether DefiLlama's free /prices/historical API can serve the price
data AlphaWallets needs for FIFO PnL calculation over 30d and 90d windows.

Coverage matters for two token groups from CLAUDE.md Section 2:
- 6 airdrops (UNI, ARB, ENA, EIGEN, MORPHO, ETHFI)
- 10 major DeFi tokens (UNI, AAVE, LDO, PENDLE, CRV, ENA, MKR, MORPHO, LINK, ARB)

Union: 12 unique tokens.

DefiLlama API: https://coins.llama.fi/prices/historical/{timestamp}/{coin_id}
- Free, no auth
- coin_id format: {chain}:{address}, e.g. ethereum:0x...

Run:
    uv run python -m alphawallets.pipeline.exploration.defillama_coverage

Output: prints coverage matrix + summary. No files written — output feeds
ADR 0008.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

# ---------- Token catalog ----------

# {token_symbol: coin_id} where coin_id = "{chain}:{address}"
# ARB probed on both chains since its situation is unusual: airdrop happened on
# Arbitrum, later bridged to Ethereum. Per ADR 0001 V1 tracks Ethereum activity.
TOKENS: dict[str, str] = {
    # V1 airdrops
    "UNI": "ethereum:0x1f9840a85d5af5bf1d1762f925bdaddc4201f984",
    "ARB (eth)": "ethereum:0xb50721bcf8d664c30412cfbc6cf7a15145234ad1",
    "ARB (arb)": "arbitrum:0x912ce59144191c1204e64559fe8253a0e49e6548",
    "ENA": "ethereum:0x57e114b691db790c35207b2e685d4a43181e6061",
    "EIGEN": "ethereum:0xec53bf9167f50cdeb3ae105f56099aaab9061f83",
    "MORPHO": "ethereum:0x58d97b57bb95320f9a05dc918aef65434969c2b2",
    "ETHFI": "ethereum:0xfe0c30065b384f05761f15d0cc899d4f9f9cc0eb",
    # V1 DeFi (excluding overlaps with airdrops)
    "AAVE": "ethereum:0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9",
    "LDO": "ethereum:0x5a98fcbea516cf06857215779fd812ca3bef1b32",
    "PENDLE": "ethereum:0x808507121b80c02388fad14726482e061b8da827",
    "CRV": "ethereum:0xd533a949740bb3306d119cc777fa900ba034cd52",
    "MKR": "ethereum:0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2",
    "LINK": "ethereum:0x514910771af9ca656af840dff83e8264ecf986ca",
}

# Test dates in Unix timestamps
NOW = datetime.now(UTC)
TEST_DATES: dict[str, int] = {
    "yesterday": int((NOW - timedelta(days=1)).timestamp()),
    "30d ago": int((NOW - timedelta(days=30)).timestamp()),
    "90d ago": int((NOW - timedelta(days=90)).timestamp()),
    "1 year ago": int((NOW - timedelta(days=365)).timestamp()),
}

API_BASE = "https://coins.llama.fi/prices/historical"


# ---------- Data model ----------


@dataclass(frozen=True)
class Probe:
    token: str
    date_label: str
    timestamp: int
    price: float | None
    latency_ms: float
    error: str | None = None


# ---------- Probing ----------


def probe(client: httpx.Client, token: str, coin_id: str, date_label: str, ts: int) -> Probe:
    url = f"{API_BASE}/{ts}/{coin_id}"
    start = time.time()
    try:
        response = client.get(url, timeout=15)
        latency_ms = (time.time() - start) * 1000
        response.raise_for_status()
        data = response.json()
        coins = data.get("coins", {})
        if coin_id in coins and "price" in coins[coin_id]:
            return Probe(token, date_label, ts, coins[coin_id]["price"], latency_ms)
        return Probe(token, date_label, ts, None, latency_ms, "no price in response")
    except httpx.HTTPError as e:
        latency_ms = (time.time() - start) * 1000
        return Probe(token, date_label, ts, None, latency_ms, f"http: {type(e).__name__}")
    except Exception as e:
        latency_ms = (time.time() - start) * 1000
        return Probe(token, date_label, ts, None, latency_ms, f"error: {type(e).__name__}: {e}")


# ---------- Main ----------


def main() -> None:
    print(f"DefiLlama coverage investigation — {NOW.date().isoformat()}")
    print(f"Testing {len(TOKENS)} tokens across {len(TEST_DATES)} dates")
    print(f"API base: {API_BASE}")
    print("=" * 90)
    print()

    probes: list[Probe] = []
    with httpx.Client() as client:
        for token, coin_id in TOKENS.items():
            for date_label, ts in TEST_DATES.items():
                p = probe(client, token, coin_id, date_label, ts)
                probes.append(p)
                # Gentle pacing — DefiLlama's free tier has soft limits
                time.sleep(0.15)

    # Coverage matrix
    print(f"{'Token':<12} | " + " | ".join(f"{d:<12}" for d in TEST_DATES))
    print("-" * 95)
    for token in TOKENS:
        row = [token]
        for date_label in TEST_DATES:
            match = next(
                (p for p in probes if p.token == token and p.date_label == date_label),
                None,
            )
            if match and match.price is not None:
                row.append(f"${match.price:>10,.2f}")
            elif match:
                row.append(f"MISS ({match.error[:6] if match.error else '?'})")
            else:
                row.append("N/A")
        print(f"{row[0]:<12} | " + " | ".join(f"{c:<12}" for c in row[1:]))

    # Summary stats
    print()
    print("=" * 90)
    print("Summary:")

    total = len(probes)
    hits = sum(1 for p in probes if p.price is not None)
    misses = total - hits
    print(f"  Total probes:   {total}")
    print(f"  With price:     {hits} ({100 * hits / total:.1f}%)")
    print(f"  Missing price:  {misses} ({100 * misses / total:.1f}%)")

    # Latency
    latencies = [p.latency_ms for p in probes]
    latencies.sort()
    print(f"  Median latency: {latencies[len(latencies) // 2]:.0f} ms")
    print(f"  p95 latency:    {latencies[int(len(latencies) * 0.95)]:.0f} ms")

    # Per-date coverage
    print()
    print("Per-date coverage:")
    for date_label in TEST_DATES:
        date_probes = [p for p in probes if p.date_label == date_label]
        date_hits = sum(1 for p in date_probes if p.price is not None)
        print(
            f"  {date_label:<12}: {date_hits}/{len(date_probes)} covered "
            f"({100 * date_hits / len(date_probes):.0f}%)"
        )

    # Any missing tokens?
    tokens_with_gaps = {p.token for p in probes if p.price is None}
    if tokens_with_gaps:
        print()
        print("Tokens with any gaps:")
        for token in sorted(tokens_with_gaps):
            gaps = [p.date_label for p in probes if p.token == token and p.price is None]
            print(f"  {token}: missing at {', '.join(gaps)}")
    else:
        print()
        print("All 12 tokens have coverage across all 4 test dates.")


if __name__ == "__main__":
    main()
