"""Earnings / FOMC blackout calendar (config stub)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import yaml


def load_calendar(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"fomc": [], "earnings": {}}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    data.setdefault("fomc", [])
    data.setdefault("earnings", {})
    return data


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def in_window(event: date, today: date, days: int) -> bool:
    return abs((event - today).days) <= days


def fomc_blackout(calendar: dict[str, Any], today: date, days: int) -> bool:
    for raw in calendar.get("fomc") or []:
        d = _as_date(raw)
        if d and in_window(d, today, days):
            return True
    return False


def earnings_blackout(
    calendar: dict[str, Any],
    symbol: str,
    today: date,
    days: int,
    expiration: Optional[date] = None,
) -> bool:
    dates = (calendar.get("earnings") or {}).get(symbol) or []
    for raw in dates:
        d = _as_date(raw)
        if not d:
            continue
        if in_window(d, today, days):
            return True
        if expiration is not None and abs((d - expiration).days) <= days:
            return True
    return False


def skip_new_entry(
    calendar: dict[str, Any],
    cfg: dict[str, Any],
    symbol: str,
    today: date,
    expiration: Optional[date] = None,
) -> Optional[str]:
    cal_cfg = cfg.get("calendar", {})
    if cal_cfg.get("skip_fomc", True) and fomc_blackout(
        calendar, today, int(cal_cfg.get("fomc_blackout_days", 1))
    ):
        return "fomc_blackout"
    if cal_cfg.get("skip_earnings", True) and earnings_blackout(
        calendar,
        symbol,
        today,
        int(cal_cfg.get("earnings_blackout_days", 3)),
        expiration=expiration,
    ):
        return "earnings_blackout"
    return None


def days_to(expiration: date, today: date) -> int:
    return (expiration - today).days
