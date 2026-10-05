"""Minimal Signal/Zone stand-ins with the SPEC section 3 field names, for harness unit tests only.
(The shared synthetic fixtures belong to S0 / Developer 2; these are local and throwaway.)"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping, Optional

import pandas as pd

ET = "America/New_York"


def t(day: str, hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{day} {hhmm}", tz=ET)


@dataclass(frozen=True)
class Z:
    symbol: str
    low: float
    high: float
    side: str
    score: float
    components: Mapping[str, float]
    kinds: tuple
    as_of_ts: datetime
    valid_from_ts: datetime
    available_at: datetime
    engine_cfg: str = "test"
    atr_d: float = 2.0


@dataclass(frozen=True)
class S:
    symbol: str
    tf: str
    direction: int
    test: str
    zone: Z
    formation: Optional[object]
    trigger: float
    stop: float
    targets: Mapping[str, float]
    expires_at: datetime
    components: Mapping[str, float]
    as_of_ts: datetime
    available_at: datetime
    variant_id: str = "v0"


def sig(symbol="AAA", day="2024-03-04", at="10:00", direction=1, trigger=100.10, stop=99.50, zlo=99.6, zhi=99.9,
        score=0.8, zone_target=101.5, expires="10:30", zone_at=None, atr_d=2.0, variant_id="v0"):
    av = t(day, at)
    z = Z(symbol, zlo, zhi, "support" if direction > 0 else "resistance", score, {"touches": 1.0}, ("pdl",),
          av - timedelta(minutes=5), zone_at or av, zone_at or av, atr_d=atr_d)
    return S(symbol, "5m", direction, "A", z, None, trigger, stop, {"1R": float("nan"), "2R": float("nan"),
             "zone": zone_target}, t(day, expires), {"n_confirm": 3.0}, av - timedelta(minutes=5), av, variant_id)


def bars(symbol, day, rows, adj=1.0, start="09:30"):
    """rows: list of (open, high, low, close) for consecutive 5m bars from `start`; volume 1e5."""
    t0 = t(day, start)
    out = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    out["ts"] = [t0 + pd.Timedelta(minutes=5 * i) for i in range(len(rows))]
    out["available_at"] = out["ts"] + pd.Timedelta(minutes=5)
    out["session"] = pd.Timestamp(day).date()
    out["adj_factor"] = adj
    out["volume"] = 1e5
    return out


def flat_day(symbol, day, px=100.0, n=78, overrides=None, adj=1.0):
    rows = [(px, px + 0.02, px - 0.02, px)] * n
    rows = list(rows)
    for i, r in (overrides or {}).items():
        rows[i] = r
    return bars(symbol, day, rows, adj)


def idx(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return ((h * 60 + m) - 570) // 5
