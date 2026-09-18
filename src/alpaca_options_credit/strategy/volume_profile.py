"""Volume-profile shelves (HVN) for pullback zones."""

from __future__ import annotations

from collections import defaultdict

from alpaca_options_credit.models import Bar


def typical_price(bar: Bar) -> float:
    return (bar.high + bar.low + bar.close) / 3.0


def profile(bars: list[Bar], bin_size: float) -> dict[float, float]:
    buckets: dict[float, float] = defaultdict(float)
    if bin_size <= 0:
        bin_size = 0.5
    for bar in bars:
        key = round(typical_price(bar) / bin_size) * bin_size
        buckets[float(key)] += bar.volume
    return dict(buckets)


def hvn_shelves(
    bars: list[Bar],
    *,
    bin_size: float = 0.5,
    percentile: float = 0.70,
) -> list[tuple[float, float]]:
    """Contiguous high-volume-node regions as (low, high) price shelves."""
    buckets = profile(bars, bin_size)
    if not buckets:
        return []
    volumes = sorted(buckets.values())
    idx = min(len(volumes) - 1, max(0, int(len(volumes) * percentile)))
    cutoff = volumes[idx]
    hot = sorted(p for p, v in buckets.items() if v >= cutoff)
    if not hot:
        return []
    shelves: list[tuple[float, float]] = []
    start = prev = hot[0]
    for price in hot[1:]:
        if abs(price - prev - bin_size) <= bin_size * 0.51:
            prev = price
        else:
            shelves.append((start, prev if prev >= start else start))
            start = prev = price
    shelves.append((start, prev))
    return shelves


def nearest_shelf(
    shelves: list[tuple[float, float]],
    anchor: float,
) -> tuple[float, float] | None:
    if not shelves:
        return None
    return min(shelves, key=lambda s: abs(((s[0] + s[1]) / 2.0) - anchor))
