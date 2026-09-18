"""Synthetic bars + chain so `observe --fixture` runs without Alpaca keys."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from alpaca_options_credit.models import Bar, ContractQuote
from alpaca_options_credit.strategy.spreads import occ_symbol


def _ts(i: int) -> datetime:
    base = datetime(2026, 3, 3, 14, 30, tzinfo=timezone.utc)
    return base + timedelta(hours=i)


def bullish_pullback_bars(symbol: str = "SPY") -> list[Bar]:
    """HH/HL confirm then first pullback into the broken-high / HVN shelf.

    Same geometry as the unit-test series so `observe --fixture` is deterministic.
    """
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
    return [
        Bar(ts=_ts(i), open=c, high=h, low=l, close=c, volume=v)
        for i, (h, l, c, v) in enumerate(rows)
    ]


def fixture_chain(
    underlying: str,
    right: str,
    invalidation: float,
    width: float = 5.0,
    today: Optional[date] = None,
) -> list[ContractQuote]:
    today = today or date.today()
    exp = today + timedelta(days=37)
    strikes = [round(invalidation + i * 0.5, 2) for i in range(-24, 25)]
    long_k = invalidation - width if right == "put" else invalidation + width
    out: list[ContractQuote] = []
    for k in strikes:
        near_short = abs(k - invalidation) <= 0.76
        near_long = abs(k - long_k) <= 0.76
        if near_short:
            bid, ask = 1.40, 1.45
        elif near_long:
            bid, ask = 0.20, 0.25
        else:
            bid, ask = 0.35, 0.40
        out.append(
            ContractQuote(
                occ=occ_symbol(underlying, exp, right, k),
                strike=k,
                expiration=exp,
                right=right,
                bid=bid,
                ask=ask,
            )
        )
    return out


class FixtureMarketData:
    """In-process data for observer without keys."""

    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self._bars = {s: bullish_pullback_bars(s) for s in symbols}

    def bars(self, symbol: str, timeframe: str, limit: int) -> list[Bar]:
        data = self._bars.get(symbol) or bullish_pullback_bars(symbol)
        return data[-limit:]

    def chain(
        self,
        symbol: str,
        right: str,
        dte_min: int,
        dte_max: int,
        strike_lo: float,
        strike_hi: float,
    ) -> list[ContractQuote]:
        inv = (strike_lo + strike_hi) / 2.0
        quotes = fixture_chain(symbol, right, inv, width=5.0)
        return [q for q in quotes if strike_lo - 1 <= q.strike <= strike_hi + 1]

    def spread_mark(self, short_occ: str, long_occ: str) -> Optional[float]:
        return 0.80  # still above 50% TP for a ~1.15 credit; no instant close
