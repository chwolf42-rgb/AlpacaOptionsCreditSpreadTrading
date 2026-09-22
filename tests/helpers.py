from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from alpaca_options_credit.models import Bar, ContractQuote
from alpaca_options_credit.strategy.spreads import occ_symbol

UTC = timezone.utc

# Shared geometry: daily HH/HL, first pullback into the broken-high shelf, then
# a 1H turn in the zone (close up vs prior) so hybrid reconfirm can fire.
BULLISH_ROWS: list[tuple[float, float, float, float]] = []
for _ in range(12):
    BULLISH_ROWS.append((107.8, 107.2, 107.5, 2_000_000))
BULLISH_ROWS.extend(
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
        (110.5, 108.8, 110.1, 1_100_000),  # 1H reconfirm turn, still in zone
    ]
)


def bar(
    i: int,
    high: float,
    low: float,
    close: float,
    volume: float = 1_000_000.0,
    *,
    step: str = "hour",
    open_: Optional[float] = None,
) -> Bar:
    if step == "day":
        ts = datetime(2026, 1, 5, 14, 30, tzinfo=UTC) + timedelta(days=i)
    else:
        ts = datetime(2026, 3, 3, 14, 30, tzinfo=UTC) + timedelta(hours=i)
    o = close if open_ is None else open_
    return Bar(ts=ts, open=o, high=high, low=low, close=close, volume=volume)


def bars_from_rows(
    rows: list[tuple[float, float, float, float]],
    *,
    step: str = "hour",
    start: Optional[datetime] = None,
    start_offset: int = 0,
) -> list[Bar]:
    out: list[Bar] = []
    for i, (h, l, c, v) in enumerate(rows):
        if start is not None:
            delta = timedelta(days=i + start_offset) if step == "day" else timedelta(hours=i + start_offset)
            ts = start + delta
            out.append(Bar(ts=ts, open=c, high=h, low=l, close=c, volume=v))
        else:
            out.append(bar(i + start_offset, h, l, c, v, step=step))
    return out


def bullish_confirm_pullback_bars(*, step: str = "hour") -> list[Bar]:
    """Unambiguous HH/HL, close above broken high, pullback, then a turn in zone."""
    return bars_from_rows(BULLISH_ROWS, step=step)


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


def hybrid_happy_daily_hourly() -> tuple[list[Bar], list[Bar]]:
    """Daily confirm + 1H series timestamped *after* the daily confirm bar."""
    daily = bullish_confirm_pullback_bars(step="day")
    confirm_ts = daily[27].ts  # matches confirm_index of this geometry
    hourly = bars_from_rows(BULLISH_ROWS, start=confirm_ts, start_offset=1)
    return daily, hourly


def hourly_waiting_no_tag(confirm_ts: datetime) -> list[Bar]:
    """1H stays extended above the daily shelf — no pullback tag."""
    rows = [(116.5, 114.8, 115.6, 800_000) for _ in range(20)]
    return bars_from_rows(rows, start=confirm_ts, start_offset=1)


def hourly_chase_extended(confirm_ts: datetime) -> list[Bar]:
    """Tags the daily zone then runs away — chase, do not enter."""
    hourly = bars_from_rows(BULLISH_ROWS[:-1], start=confirm_ts, start_offset=1)
    i = len(hourly)
    hourly.append(
        Bar(
            ts=confirm_ts + timedelta(hours=i + 1),
            open=124.5,
            high=126.0,
            low=120.0,
            close=124.5,
            volume=800_000,
        )
    )
    return hourly


def hourly_in_zone_no_reconfirm(confirm_ts: datetime) -> list[Bar]:
    """Pulls into the daily zone without 1H HH/HL — reconfirm fails."""
    rows: list[tuple[float, float, float, float]] = []
    px = 114.0
    for _ in range(18):
        rows.append((px + 0.4, px - 0.5, px - 0.2, 700_000))
        px -= 0.35
    # last prints sit in the ~107.5–110.3 daily shelf
    rows.append((110.2, 108.7, 109.1, 800_000))
    rows.append((110.0, 108.6, 108.9, 800_000))
    return bars_from_rows(rows, start=confirm_ts, start_offset=1)


def flat_daily_bars(n: int = 24) -> list[Bar]:
    return [bar(i, 100.4, 99.6, 100.0, 500_000, step="day") for i in range(n)]


def shift_bars(bars: list[Bar], delta: timedelta) -> list[Bar]:
    return [
        Bar(
            ts=b.ts + delta,
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=b.volume,
        )
        for b in bars
    ]


def align_bars_to(*series: list[Bar], end: datetime) -> tuple[list[Bar], ...]:
    """Shift every series by the same delta so the newest bar lands on `end`."""
    lasts = [s[-1].ts for s in series if s]
    if not lasts:
        return tuple(list(s) for s in series)
    delta = end - max(lasts)
    return tuple(shift_bars(s, delta) for s in series)


class FakeMarketData:
    def __init__(
        self,
        bars_map: dict[str, list[Bar]],
        chain: list[ContractQuote],
        mark: float = 0.80,
        daily_map: dict[str, list[Bar]] | None = None,
    ):
        self.bars_map = bars_map
        self.daily_map = daily_map or bars_map
        self._chain = chain
        self.mark = mark
        self.bar_calls: list[tuple[str, str]] = []

    def bars(self, symbol: str, timeframe: str, limit: int) -> list[Bar]:
        self.bar_calls.append((symbol, str(timeframe)))
        tf = str(timeframe)
        src = self.daily_map if tf in {"1Day", "1D", "Day", "daily"} else self.bars_map
        return (src.get(symbol) or [])[-limit:]

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
