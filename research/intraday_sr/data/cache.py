"""Read Trading's 5m parquet and reduce it to the study tape.

Columns on disk: ``ts`` (ET-naive bar **open**), ``symbol``, OHLC float32,
``volume``, ``trades``, ``vwap``. Extended hours may be present. This loader
keeps RTH only, from 2019-01-02 through 2026-09-30, using the NYSE calendar
for holidays and early closes.

``ts`` stays the open. ``available_at`` is the close (open + 5 minutes).
Bad bars (high < low, non-positive prices, or a close more than 8× the
prior ATR_5m from the prior close) are dropped and counted. A missing 5m
bar is left missing. Nothing is forward-filled.

The 16:00-labeled print in an extended-hours file is after 15:55, so the
RTH filter removes it. It is not used as a 5m bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from research.intraday_sr.data.adjust import factor_for
from research.intraday_sr.data.calendar import is_session, last_bar_open
from research.intraday_sr.types import ET

# Per-symbol RTH parquet. The root is always passed in; this is only the documented default.
DEFAULT_CACHE_ROOT = Path("/workspace/research2/data/alpaca_intraday/m5rth_fixed33")

STUDY_START = date(2019, 1, 2)
STUDY_END = date(2026, 9, 30)
RTH_OPEN = time(9, 30)
ATR_LENGTH = 14
SPIKE_ATR = 8.0

# FB history is the pre-rename tape for META. BRK.B is stored as BRK-B.
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
)


@dataclass(frozen=True)
class CleanReport:
    """How many rows the loader discarded. The readout sums these."""

    input_rows: int
    kept: int
    off_session: int
    high_below_low: int
    nonpositive: int
    spike: int


def load_bars_file(
    path: str | Path,
    *,
    factors: pd.Series | None = None,
    start: date = STUDY_START,
    end: date = STUDY_END,
    symbol: str | None = None,
) -> tuple[pd.DataFrame, CleanReport]:
    """Load one parquet or csv cache file into the research frame."""
    target = Path(path)
    if target.suffix == ".csv":
        raw = pd.read_csv(target)
    else:
        raw = pd.read_parquet(target)
    return normalize_bars(raw, factors=factors, start=start, end=end, symbol=symbol)


def normalize_bars(
    raw: pd.DataFrame,
    *,
    factors: pd.Series | None = None,
    start: date = STUDY_START,
    end: date = STUDY_END,
    symbol: str | None = None,
) -> tuple[pd.DataFrame, CleanReport]:
    """Apply the RTH window, the study dates, and the bad-bar rules.

    ``raw`` uses the cache schema. ``ts`` may be naive (ET wall time) or
    already tz-aware. Naive stamps are localized to America/New_York and
    are not treated as UTC.
    """
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
    off_session = 0
    high_below_low = 0
    nonpositive = 0
    keep_mask = np.ones(len(frame), dtype=bool)
    sessions: list[date | None] = []
    for ts in frame["ts"]:
        day = ts.date()
        last_open = last_bar_open(day) if start <= day <= end else None
        if last_open is None or not is_session(day) or not (RTH_OPEN <= ts.time() <= last_open):
            sessions.append(None)
        else:
            sessions.append(day)
    session_ok = np.array([day is not None for day in sessions], dtype=bool)
    off_session = int((~session_ok).sum())
    keep_mask &= session_ok

    prices = frame.loc[:, ["open", "high", "low", "close"]].to_numpy(dtype=np.float64)
    bad_hl = prices[:, 1] < prices[:, 2]
    bad_px = (prices <= 0).any(axis=1) | ~np.isfinite(prices).all(axis=1)
    # Count only among rows that were still on the session clock.
    high_below_low = int((bad_hl & keep_mask).sum())
    nonpositive = int((bad_px & keep_mask & ~bad_hl).sum())
    keep_mask &= ~bad_hl & ~bad_px

    frame = frame.loc[keep_mask].copy()
    frame["session"] = [day for day, ok in zip(sessions, keep_mask) if ok]
    frame, spike = _drop_spikes(frame)
    frame["available_at"] = frame["ts"] + timedelta(minutes=5)
    frame["tf"] = "5m"
    if "vwap" not in frame.columns:
        frame["vwap"] = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    if "trades" not in frame.columns:
        frame["trades"] = 0
    frame["adj_factor"] = [
        factor_for(factors, sym, sess)
        for sym, sess in zip(frame["symbol"].tolist(), frame["session"].tolist())
    ]
    for column in ("open", "high", "low", "close", "vwap", "adj_factor"):
        frame[column] = frame[column].astype(np.float32)
    frame["volume"] = frame["volume"].astype(np.float64)
    frame["trades"] = frame["trades"].astype(np.int32)
    frame = frame.loc[:, list(RESEARCH_COLUMNS)].reset_index(drop=True)
    # A gap in `ts` stays a gap. Do not reindex onto a 5m grid.
    report = CleanReport(
        input_rows=input_rows,
        kept=int(len(frame)),
        off_session=off_session,
        high_below_low=high_below_low,
        nonpositive=nonpositive,
        spike=spike,
    )
    return frame, report


def _as_et_open(values: pd.Series) -> pd.Series:
    stamps = pd.to_datetime(values)
    if getattr(stamps.dt, "tz", None) is None:
        stamps = stamps.dt.tz_localize(ET, ambiguous="infer", nonexistent="shift_forward")
    else:
        stamps = stamps.dt.tz_convert(ET)
    return stamps


def _drop_spikes(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop closes more than 8×ATR_5m from the prior kept close of the same session.

    Overnight gaps are not judged against a 5-minute ATR. ATR itself carries
    across sessions. The spike bar is not part of the ATR that rejects it.
    Until 14 true ranges exist, the rule is off. A dropped bar does not move
    the session's prior close, so the next print is compared with the last
    good one.
    """
    if frame.empty:
        return frame, 0
    keep_parts: list[pd.DataFrame] = []
    dropped = 0
    for _, group in frame.groupby("symbol", sort=False):
        group = group.reset_index(drop=True)
        high = group["high"].to_numpy(dtype=np.float64)
        low = group["low"].to_numpy(dtype=np.float64)
        close = group["close"].to_numpy(dtype=np.float64)
        sessions = group["session"].tolist()
        keep = np.ones(len(group), dtype=bool)
        prev_close = None
        prev_session = None
        atr = None
        trs: list[float] = []
        for i in range(len(group)):
            if prev_session != sessions[i]:
                prev_close = close[i]
                prev_session = sessions[i]
                continue
            if atr is not None and abs(close[i] - prev_close) > SPIKE_ATR * atr:
                keep[i] = False
                dropped += 1
                continue
            tr = max(high[i] - low[i], abs(high[i] - prev_close), abs(low[i] - prev_close))
            trs.append(tr)
            if atr is None and len(trs) == ATR_LENGTH:
                atr = float(sum(trs) / ATR_LENGTH)
            elif atr is not None:
                atr = (atr * (ATR_LENGTH - 1) + tr) / ATR_LENGTH
            prev_close = close[i]
        keep_parts.append(group.loc[keep])
    if not keep_parts:
        return frame.iloc[0:0].copy(), dropped
    return pd.concat(keep_parts, ignore_index=True), dropped


def cache_filename(symbol: str) -> str:
    """Parquet stem for ``symbol``. The cache stores BRK.B as ``BRK-B``."""
    if symbol == "BRK.B":
        return "BRK-B"
    return symbol


def symbol_from_cache_name(name: str) -> str:
    """Inverse of ``cache_filename``. ``FB`` stitches into META."""
    stem = name[:-8] if name.endswith(".parquet") else name
    if stem == "BRK-B":
        return "BRK.B"
    if stem == "FB":
        return "META"
    return stem


def symbol_cache_path(cache_root: str | Path, symbol: str) -> Path:
    return Path(cache_root) / f"{cache_filename(symbol)}.parquet"


def expected_bar_count(day: date) -> int | None:
    """5m bars in one RTH session: 78, or 42 when the NYSE closes at 13:00."""
    last = last_bar_open(day)
    if last is None:
        return None
    start_min = RTH_OPEN.hour * 60 + RTH_OPEN.minute
    end_min = last.hour * 60 + last.minute
    return (end_min - start_min) // 5 + 1


@dataclass(frozen=True)
class SymbolValidation:
    """Result of checking one symbol's raw cache file before the study reads it."""

    symbol: str
    rows: int
    duplicate_timestamps: int
    outside_rth: int
    bad_bars: int
    count_mismatches: tuple[tuple[str, int, int], ...]

    @property
    def ok(self) -> bool:
        return (
            self.duplicate_timestamps == 0
            and self.outside_rth == 0
            and self.bad_bars == 0
            and not self.count_mismatches
        )


class SymbolValidationError(ValueError):
    """Raised when a cache file fails ``validate_symbol`` so the study does not read it."""

    def __init__(self, report: SymbolValidation):
        self.report = report
        super().__init__(
            f"{report.symbol} failed cache validation: "
            f"duplicates={report.duplicate_timestamps} outside_rth={report.outside_rth} "
            f"bad_bars={report.bad_bars} session_count_mismatches={len(report.count_mismatches)}"
        )


def validate_symbol(raw: pd.DataFrame, symbol: str) -> SymbolValidation:
    """Check bar count per session, RTH bounds, duplicate timestamps, and bad bars.

    ``raw`` is the cache file, before bad bars are dropped. A full session must
    have 78 bars (09:30 through 15:55). An early close must have 42 (through
    12:55). Anything else, a duplicate open, a print outside that window, or a
    bar with high < low or a non-positive price fails the symbol.
    """
    wanted = SYMBOL_ALIASES.get(symbol, symbol)
    frame = raw.copy()
    if "symbol" not in frame.columns:
        frame["symbol"] = wanted
    else:
        frame["symbol"] = frame["symbol"].astype(str).replace(SYMBOL_ALIASES)
    frame = frame.loc[frame["symbol"] == wanted]
    if "ts" not in frame.columns:
        raise ValueError(f"{symbol} cache frame has no ts column")
    frame = frame.copy()
    frame["ts"] = _as_et_open(frame["ts"])
    rows = int(len(frame))
    duplicate_timestamps = int(frame.duplicated(subset=["ts"]).sum()) if rows else 0

    outside = 0
    sessions: list[date] = []
    for ts in frame["ts"]:
        day = ts.date()
        last_open = last_bar_open(day)
        if last_open is None or not (RTH_OPEN <= ts.time() <= last_open):
            outside += 1
            sessions.append(day)
        else:
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
        count_mismatches=tuple(mismatches),
    )


def load_symbol(
    cache_root: str | Path,
    symbol: str,
    *,
    factors: pd.Series | None = None,
    start: date = STUDY_START,
    end: date = STUDY_END,
) -> tuple[pd.DataFrame, CleanReport]:
    """Validate one symbol's parquet, then normalize it. An invalid file is not returned."""
    path = symbol_cache_path(cache_root, symbol)
    raw = pd.read_parquet(path)
    report = validate_symbol(raw, symbol)
    if not report.ok:
        raise SymbolValidationError(report)
    return normalize_bars(raw, factors=factors, start=start, end=end, symbol=symbol)


def load_universe_bars(
    cache_root: str | Path,
    *,
    factors: pd.Series | None = None,
    symbols: tuple[str, ...] | list[str] | None = None,
    start: date = STUDY_START,
    end: date = STUDY_END,
) -> tuple[tuple[pd.DataFrame, ...], tuple[CleanReport, ...], tuple[SymbolValidation, ...]]:
    """Validate every symbol before any normalized frame is built.

    The study must not read a tape that has not passed ``validate_symbol``.
    When any symbol fails, this raises and returns nothing.
    """
    if symbols is None:
        from research.intraday_sr.grids import UNIVERSE

        names = UNIVERSE
    else:
        names = tuple(symbols)
    raws: list[pd.DataFrame] = []
    reports: list[SymbolValidation] = []
    for symbol in names:
        raw = pd.read_parquet(symbol_cache_path(cache_root, symbol))
        report = validate_symbol(raw, symbol)
        if not report.ok:
            raise SymbolValidationError(report)
        raws.append(raw)
        reports.append(report)
    frames: list[pd.DataFrame] = []
    cleans: list[CleanReport] = []
    for symbol, raw in zip(names, raws):
        frame, clean = normalize_bars(raw, factors=factors, start=start, end=end, symbol=symbol)
        frames.append(frame)
        cleans.append(clean)
    return tuple(frames), tuple(cleans), tuple(reports)
