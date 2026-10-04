"""Shared contract shim.

`Fill` and `Trade` live in research/intraday_sr/types.py (SPEC section 3, authored in S0 by Developer 2).
Until that file lands on research/intraday-sr, this module provides byte-for-byte equivalent fallbacks so the
harness and its tests run. Delete the fallback once types.py is merged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

try:  # pragma: no cover - exercised once S0 lands
    from research.intraday_sr.types import Fill, Trade  # type: ignore  # noqa: F401
    FROM_TYPES = True
except Exception:  # noqa: BLE001
    FROM_TYPES = False

    @dataclass(frozen=True)
    class Fill:  # SPEC section 3
        symbol: str
        ts: datetime
        price: float
        qty: float
        side: int
        reason: str
        cost: float

    @dataclass(frozen=True)
    class Trade:  # SPEC section 3
        signal: Any
        entry: Fill
        exit: Fill
        r: float
        pnl: float
        fold: int
        variant_id: str

TZ = "America/New_York"
