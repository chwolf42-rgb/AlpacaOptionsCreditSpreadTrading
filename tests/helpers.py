from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alpaca_options_credit.models import Bar, ContractQuote
from alpaca_options_credit.strategy.spreads import occ_symbol

UTC = timezone.utc


def bar(i: int, high: float, low: float, close: float, volume: float = 1_000_000.0) -> Bar:
    ts = datetime(2026, 3, 3, 14, 30, tzinfo=UTC) + timedelta(hours=i)
    return Bar(ts=ts, open=close, high=high, low=low, close=close, volume=volume)


def bullish_confirm_pullback_bars() -> list[Bar]:
    """Unambiguous HH/HL, close above broken high, then first pullback into that high."""
    rows: list[tuple[float, float, float, float]] = []
    for _ in range(12):
        rows.append((107.8, 107.2, 107.5, 2_000_000))
    rows.extend(
        [
            (107.4, 106.8, 107.0, 800_000),
            (106.6, 105.9, 106.2, 800_000),
            (105.4, 99.8, 101.0, 900_000),
            (103.5, 101.2, 103.0, 800_000),
            (105.2, 102.8, 104.8, 800_000),
            (107.0, 104.5, 106.5, 900_000),
            (109.4, 106.0, 108.8, 1_100_000),
            (108.6, 107.1, 107.8, 1_000_000),
            (108.2, 106.9, 107.4, 1_000_000),
            (107.5, 104.0, 105.0, 900_000),
            (105.8, 102.9, 104.2, 900_000),
            (106.4, 103.5, 105.8, 900_000),
            (108.0, 105.0, 107.2, 1_000_000),
            (110.0, 107.0, 109.2, 1_100_000),
            (112.5, 108.8, 111.8, 1_200_000),
            (115.2, 111.0, 114.6, 1_300_000),
            (114.4, 112.0, 113.5, 1_100_000),
            (113.8, 111.5, 112.8, 1_000_000),
            (113.0, 110.5, 111.2, 1_000_000),
            (110.8, 108.6, 109.0, 1_200_000),
        ]
    )
    return [bar(i, h, l, c, v) for i, (h, l, c, v) in enumerate(rows)]


def structure_break_bars() -> list[Bar]:
    bars = bullish_confirm_pullback_bars()
    last_i = len(bars)
    bars.append(bar(last_i, 103.0, 98.5, 99.0, 1_000_000))
    return bars


def no_chase_extended_bars() -> list[Bar]:
    bars = bullish_confirm_pullback_bars()[:-1]
    i = len(bars)
    bars.append(bar(i, 126.0, 120.0, 124.5, 800_000))
    return bars


def dip_holds_bars() -> list[Bar]:
    bars = bullish_confirm_pullback_bars()
    inv_proxy = 102.9
    i = len(bars)
    bars.append(bar(i, 110.0, inv_proxy - 0.4, inv_proxy + 1.5, 900_000))
    return bars


class FakeMarketData:
    def __init__(self, bars_map: dict[str, list[Bar]], chain: list[ContractQuote], mark: float = 0.80):
        self.bars_map = bars_map
        self._chain = chain
        self.mark = mark
        self.bar_calls: list[str] = []

    def bars(self, symbol: str, timeframe: str, limit: int) -> list[Bar]:
        self.bar_calls.append(symbol)
        return (self.bars_map.get(symbol) or [])[-limit:]

    def chain(self, symbol, right, dte_min, dte_max, strike_lo, strike_hi):
        return [q for q in self._chain if q.right == right and strike_lo - 1 <= q.strike <= strike_hi + 1]

    def spread_mark(self, short_occ: str, long_occ: str) -> float:
        return self.mark


def listed_chain(
    underlying: str,
    right: str,
    expiration,
    strikes: list[float],
    *,
    bid: float | None = None,
    ask: float | None = None,
    short_bid: float | None = None,
    long_ask: float | None = None,
    invalidation: float | None = None,
    width: float = 5.0,
) -> list[ContractQuote]:
    """If invalidation is set, short-near-inv bids are rich enough to clear the 20% gate."""
    out: list[ContractQuote] = []
    for k in strikes:
        if invalidation is not None and short_bid is not None:
            near_short = abs(k - invalidation) <= 0.76
            near_long = abs(k - (invalidation - width if right == "put" else invalidation + width)) <= 0.76
            if near_short:
                b, a = short_bid, short_bid + 0.05
            elif near_long and long_ask is not None:
                b, a = max(0.05, long_ask - 0.05), long_ask
            else:
                b, a = 0.20, 0.25
        else:
            b, a = float(bid or 0.5), float(ask or 0.55)
        out.append(
            ContractQuote(
                occ=occ_symbol(underlying, expiration, right, k),
                strike=k,
                expiration=expiration,
                right=right,
                bid=b,
                ask=a,
            )
        )
    return out
