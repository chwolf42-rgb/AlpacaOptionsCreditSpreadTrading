"""Read one symbol of Trading's 5m parquet at a time.

Default end is 2026-03-31. A later bar needs a holdout token. Malformed
bars are dropped and counted. Isolated spikes are clamped, not dropped.
Missing bars stay missing. Short sessions are reported and do not fail
validation.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from research.intraday_sr.data.adjust import factor_for
from research.intraday_sr.data.badprint import repair_bad_prints
from research.intraday_sr.data.calendar import is_session, last_bar_open
from research.intraday_sr.data.holdout import HoldoutToken, require_dev_range
from research.intraday_sr.grids import DEV_END
from research.intraday_sr.io import read_bytes
from research.intraday_sr.types import ET

STUDY_START = date(2019, 1, 2)
STUDY_END = date(2026, 9, 30)
# grids.DEV_END is the frozen ISO string. Loaders compare real dates.
DEV_END_DATE = date.fromisoformat(DEV_END)
RTH_OPEN = time(9, 30)
SYMBOL_ALIASES = {"BRK-B": "BRK.B", "FB": "META"}

RESEARCH_COLUMNS = (
    "symbol",
    "tf",
    "ts",
    "available_at",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vwap",
    "trades",
    "session",
    "adj_factor",
    "high_unclamped",
    "low_unclamped",
    "bad_print",
    "bad_print_visible_at",
)


@dataclass(frozen=True)
class CleanReport:
    input_rows: int
    kept: int
    off_session: int
    malformed: int
    bad_prints: int
    flagged_rate: float
    review: bool


@dataclass(frozen=True)
class SymbolValidation:
    symbol: str
    rows: int
    duplicate_timestamps: int
    outside_rth: int
    bad_bars: int
    nonmonotonic: int
    count_mismatches: tuple[tuple[str, int, int], ...]

    @property
    def ok(self) -> bool:
        return (
            self.duplicate_timestamps == 0
            and self.outside_rth == 0
            and self.bad_bars == 0
            and self.nonmonotonic == 0
        )


class SymbolValidationError(ValueError):
    def __init__(self, report: SymbolValidation):
        self.report = report
        super().__init__(
            f"{report.symbol} failed cache validation: "
            f"duplicates={report.duplicate_timestamps} outside_rth={report.outside_rth} "
            f"bad_bars={report.bad_bars} nonmonotonic={report.nonmonotonic}"
        )


def cache_filename(symbol: str) -> str:
    if symbol == "BRK.B":
        return "BRK-B"
    return symbol


def symbol_from_cache_name(name: str) -> str:
    stem = name[:-8] if name.endswith(".parquet") else name
    if stem == "BRK-B":
        return "BRK.B"
    if stem == "FB":
        return "META"
    return stem


def symbol_cache_path(cache_root: str | Path, symbol: str) -> Path:
    return Path(cache_root) / f"{cache_filename(symbol)}.parquet"


def expected_bar_count(day: date) -> int | None:
    last = last_bar_open(day)
    if last is None:
        return None
    start_min = RTH_OPEN.hour * 60 + RTH_OPEN.minute
    end_min = last.hour * 60 + last.minute
    return (end_min - start_min) // 5 + 1


def _read_parquet(path: str | Path) -> pd.DataFrame:
    return pq.read_table(io.BytesIO(read_bytes(path))).to_pandas()


def _as_et_open(values: pd.Series) -> pd.Series:
    stamps = pd.to_datetime(values)
    if getattr(stamps.dt, "tz", None) is None:
        return stamps.dt.tz_localize(ET)
    return stamps.dt.tz_convert(ET)


def validate_symbol(raw: pd.DataFrame, symbol: str) -> SymbolValidation:
    """Fail on duplicates, out-of-RTH prints, bad OHLC, or a backwards clock.

    A short session is recorded in ``count_mismatches`` and does not fail.
    """
    wanted = SYMBOL_ALIASES.get(symbol, symbol)
    frame = raw.copy()
    if "symbol" not in frame.columns:
        frame["symbol"] = wanted
    else:
        frame["symbol"] = frame["symbol"].astype(str).replace(SYMBOL_ALIASES)
    frame = frame.loc[frame["symbol"] == wanted].copy()
    if "ts" not in frame.columns:
        raise ValueError(f"{symbol} cache frame has no ts column")
    frame["ts"] = _as_et_open(frame["ts"])
    frame = frame.sort_values("ts")
    rows = int(len(frame))
    duplicate_timestamps = int(frame.duplicated(subset=["ts"]).sum()) if rows else 0
    nonmonotonic = 0
    if rows > 1:
        nonmonotonic = int((frame["ts"].diff().iloc[1:] <= pd.Timedelta(0)).sum())

    outside = 0
    sessions: list[date] = []
    for ts in frame["ts"]:
        day = ts.date()
        last_open = last_bar_open(day)
        wall = ts.time()
        if last_open is None or not (RTH_OPEN <= wall <= last_open) or not is_session(day):
            outside += 1
        sessions.append(day)

    bad = 0
    if rows and {"open", "high", "low", "close"}.issubset(frame.columns):
        prices = frame.loc[:, ["open", "high", "low", "close"]].to_numpy(dtype=np.float64)
        bad_hl = prices[:, 1] < prices[:, 2]
        bad_px = (prices <= 0).any(axis=1) | ~np.isfinite(prices).all(axis=1)
        bad = int((bad_hl | bad_px).sum())
    elif rows:
        bad = rows

    mismatches: list[tuple[str, int, int]] = []
    if rows:
        counted = pd.Series(sessions).value_counts()
        for day, actual in counted.items():
            expected = expected_bar_count(day)
            if expected is None or int(actual) != expected:
                mismatches.append((day.isoformat(), int(actual), -1 if expected is None else expected))
    mismatches.sort()
    return SymbolValidation(
        symbol=wanted,
        rows=rows,
        duplicate_timestamps=duplicate_timestamps,
        outside_rth=outside,
        bad_bars=bad,
        nonmonotonic=nonmonotonic,
        count_mismatches=tuple(mismatches),
    )


def _as_session_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _atr_by_session(frame: pd.DataFrame) -> dict[tuple[str, date], float]:
    """Wilder-14 ATR through the prior session, keyed by (symbol, session)."""
    out: dict[tuple[str, date], float] = {}
    for symbol, group in frame.groupby("symbol", sort=True, observed=False):
        daily = []
        for session, day in group.groupby("session", sort=True):
            day_key = pd.Timestamp(session).date()
            ordered = day.sort_values("ts")
            daily.append(
                (
                    day_key,
                    float(ordered["high"].max()),
                    float(ordered["low"].min()),
                    float(ordered["close"].iloc[-1]),
                )
            )
        if len(daily) < 15:
            continue
        ranges: list[float] = []
        for index in range(1, len(daily)):
            _, high, low, close = daily[index]
            prev = daily[index - 1][3]
            ranges.append(max(high - low, abs(high - prev), abs(low - prev)))
        atr = sum(ranges[:14]) / 14
        # The first ATR applies to the session AFTER the 14th true range's session,
        # i.e. through yesterday relative to daily[15] if it exists. ranges[k] belongs
        # to daily[k+1]. After 14 ranges, ATR through daily[14] is known for daily[15].
        ready_from = 15
        cursor = atr
        if len(daily) > ready_from:
            out[(str(symbol), daily[ready_from][0])] = cursor
        for index, true_range in enumerate(ranges[14:], start=15):
            cursor = (cursor * 13 + true_range) / 14
            nxt = index + 1
            if nxt < len(daily):
                out[(str(symbol), daily[nxt][0])] = cursor
    return out


def normalize_bars(
    raw: pd.DataFrame,
    *,
    factors: pd.Series,
    start: date = STUDY_START,
    end: date = DEV_END_DATE,
    symbol: str | None = None,
    token: HoldoutToken | None = None,
) -> tuple[pd.DataFrame, CleanReport]:
    """RTH window, malformed-bar drop, and the isolated-spike clamp."""
    require_dev_range(start, end, token)
    if factors is None:
        raise ValueError("adjustment factors are required on the study path")
    required = ("ts", "open", "high", "low", "close", "volume")
    missing = [name for name in required if name not in raw.columns]
    if missing:
        raise ValueError(f"bar frame is missing {missing}")
    frame = raw.copy()
    if "symbol" not in frame.columns:
        if symbol is None:
            raise ValueError("bar frame has no symbol column")
        frame["symbol"] = symbol
    frame["symbol"] = frame["symbol"].astype(str).replace(SYMBOL_ALIASES)
    if symbol is not None:
        wanted = SYMBOL_ALIASES.get(symbol, symbol)
        frame = frame.loc[frame["symbol"] == wanted]
    frame["ts"] = _as_et_open(frame["ts"])
    frame = frame.sort_values(["symbol", "ts"]).reset_index(drop=True)
    input_rows = int(len(frame))

    prices = frame.loc[:, ["open", "high", "low", "close"]].to_numpy(dtype=np.float64)
    malformed = (prices[:, 1] < prices[:, 2]) | (prices <= 0).any(axis=1) | ~np.isfinite(prices).all(axis=1)
    malformed_count = int(malformed.sum())
    frame = frame.loc[~malformed].copy()

    keep_clock = []
    off_session = 0
    for ts in frame["ts"]:
        day = ts.date()
        last_open = last_bar_open(day)
        wall = ts.time()
        inside = (
            last_open is not None
            and is_session(day)
            and RTH_OPEN <= wall <= last_open
            and start <= day <= end
        )
        if not inside:
            off_session += 1
        keep_clock.append(inside)
    frame = frame.loc[keep_clock].copy()
    if frame.empty:
        report = CleanReport(input_rows, 0, off_session, malformed_count, 0, 0.0, False)
        return _empty(), report

    sessions = [ts.date() for ts in frame["ts"]]
    factors_out = []
    for sym, day in zip(frame["symbol"].astype(str), sessions):
        value = factor_for(factors, sym, day)
        factors_out.append(value)
    factor_arr = np.asarray(factors_out, dtype=np.float64)
    if not np.isfinite(factor_arr).all():
        raise ValueError("a session is missing its adjustment factor")

    frame["available_at"] = frame["ts"] + timedelta(minutes=5)
    # Date objects through the ATR and spike pass. Stored as int32 YYYYMMDD:
    # pandas 3 will not keep a datetime64[D] column.
    frame["session"] = sessions
    frame["tf"] = "5m"
    frame["adj_factor"] = factor_arr
    if "vwap" not in frame.columns:
        frame["vwap"] = frame["close"]
    if "trades" not in frame.columns:
        frame["trades"] = 0
    atr = _atr_by_session(frame)
    frame, flagged = repair_bad_prints(frame, atr)
    rate = flagged / len(frame) if len(frame) else 0.0
    session_days = [_as_session_date(value) for value in frame["session"].tolist()]
    frame["session"] = np.asarray(
        [day.year * 10000 + day.month * 100 + day.day for day in session_days],
        dtype=np.int32,
    )
    frame["symbol"] = frame["symbol"].astype("category")
    frame["tf"] = frame["tf"].astype("category")
    frame["open"] = frame["open"].astype(np.float32)
    frame["high"] = frame["high"].astype(np.float32)
    frame["low"] = frame["low"].astype(np.float32)
    frame["close"] = frame["close"].astype(np.float32)
    frame["vwap"] = frame["vwap"].astype(np.float32)
    frame["volume"] = frame["volume"].astype(np.float64)
    frame["trades"] = frame["trades"].astype(np.int32)
    frame["adj_factor"] = frame["adj_factor"].astype(np.float64)
    frame = frame.loc[:, list(RESEARCH_COLUMNS)].reset_index(drop=True)
    report = CleanReport(
        input_rows=input_rows,
        kept=int(len(frame)),
        off_session=off_session,
        malformed=malformed_count,
        bad_prints=flagged,
        flagged_rate=rate,
        review=rate > 0.001,
    )
    return frame, report


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=list(RESEARCH_COLUMNS))


def load_symbol(
    cache_root: str | Path,
    symbol: str | None = None,
    *,
    factors: pd.Series | None = None,
    start: date = STUDY_START,
    end: date | None = None,
    token: HoldoutToken | None = None,
):
    """Load one symbol from a cache directory, or one parquet path for the chokepoint test.

    A file path returns the raw table. A directory is the study path: factors
    are required and the default end is 2026-03-31.
    """
    path = Path(cache_root)
    if path.is_file():
        frame = _read_parquet(path)
        if symbol is not None and "symbol" in frame.columns:
            frame = frame.loc[frame["symbol"] == symbol].reset_index(drop=True)
        return frame
    if end is None:
        end = DEV_END_DATE
    if symbol is None:
        raise ValueError("symbol is required")
    if factors is None:
        raise ValueError("adjustment factors are required on the study path")
    raw = _read_parquet(symbol_cache_path(path, symbol))
    report = validate_symbol(raw, symbol)
    if not report.ok:
        raise SymbolValidationError(report)
    return normalize_bars(raw, factors=factors, start=start, end=end, symbol=symbol, token=token)


def iter_symbol_bars(
    cache_root: str | Path,
    *,
    factors: pd.Series,
    symbols: tuple[str, ...] | list[str],
    start: date = STUDY_START,
    end: date | None = None,
    token: HoldoutToken | None = None,
) -> Iterator[tuple[str, pd.DataFrame, CleanReport]]:
    """One symbol at a time. The previous frame is not retained by this iterator."""
    if end is None:
        end = DEV_END_DATE
    for symbol in symbols:
        frame, report = load_symbol(
            cache_root,
            symbol,
            factors=factors,
            start=start,
            end=end,
            token=token,
        )
        yield symbol, frame, report
