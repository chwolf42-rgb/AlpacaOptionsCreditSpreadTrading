"""Engine path: daily confirm / 1H timing, journal reasons, no live orders."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from alpaca_options_credit.broker.dry_run import DryRunBroker
from alpaca_options_credit.config import load_config
from alpaca_options_credit.engine import Engine
from alpaca_options_credit.journal import Journal
from alpaca_options_credit.models import ArmStatus
from tests.helpers import (
    FakeMarketData,
    flat_daily_bars,
    hybrid_happy_daily_hourly,
    hourly_waiting_no_tag,
    listed_chain,
)


def _events(journal: Journal) -> list[tuple[str, str, dict]]:
    con = sqlite3.connect(journal.path)
    rows = con.execute("SELECT kind, symbol, payload FROM events").fetchall()
    con.close()
    return [(k, s, json.loads(p)) for k, s, p in rows]


def _engine(tmp_path: Path, daily, hourly, mark: float = 0.80):
    cfg = load_config()
    cfg["bot"]["dry_run"] = True
    cfg["bot"]["var_dir"] = str(tmp_path / "var")
    cfg["_repo_root"] = str(tmp_path)
    cfg["universe"]["symbols"] = ["SPY"]
    cfg["rth"]["scan_only_rth"] = False
    cfg["calendar"]["skip_fomc"] = False
    cfg["calendar"]["skip_earnings"] = False
    inv = 102.9
    exp = date(2026, 3, 3) + timedelta(days=37)
    strikes = [round(90 + i * 0.5, 2) for i in range(0, 50)]
    chain = listed_chain(
        "SPY",
        "put",
        exp,
        strikes,
        invalidation=inv,
        width=5.0,
        short_bid=1.40,
        long_ask=0.25,
    )
    data = FakeMarketData({"SPY": hourly}, chain, mark=mark, daily_map={"SPY": daily})
    broker = DryRunBroker(equity=100_000)
    journal = Journal(tmp_path / "journal.sqlite")
    now = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)
    engine = Engine(
        cfg,
        journal,
        broker,
        data,
        dry_run=True,
        now_fn=lambda: now,
        calendar={"fomc": [], "earnings": {}},
    )
    return engine, broker, journal, data


def test_default_config_is_daily_1h_hybrid():
    cfg = load_config()
    tf = cfg["timeframe"]
    assert tf["structure_bar"] == "1Day"
    assert tf["timing_bar"] == "1Hour"
    assert "bar" not in tf
    assert 20 <= int(tf["volume_profile"]["lookback_bars"]) <= 30
    assert int(cfg["market_data"]["daily_bar_lookback"]) > int(tf["arm_timeout_bars"])
    assert cfg["universe"]["active"] == "day1"


def test_engine_fetches_daily_and_1h(tmp_path):
    daily, hourly = hybrid_happy_daily_hourly()
    engine, _, _, data = _engine(tmp_path, daily, hourly)
    engine.tick()
    tfs = {tf for _, tf in data.bar_calls}
    assert "1Day" in tfs
    assert "1Hour" in tfs


def test_engine_daily_not_confirmed_journals(tmp_path):
    daily = flat_daily_bars()
    hourly = daily
    engine, broker, journal, _ = _engine(tmp_path, daily, hourly)
    result = engine.tick()
    assert result.arms == []
    assert result.proposals == []
    assert broker.submitted_order_ids == []
    kinds = [(k, p.get("reason")) for k, _, p in _events(journal)]
    assert ("scan_skip", "daily_not_confirmed") in kinds


def test_engine_waiting_1h_pullback_arms_but_no_entry(tmp_path):
    daily, _ = hybrid_happy_daily_hourly()
    hourly = hourly_waiting_no_tag(daily[27].ts)
    engine, broker, journal, _ = _engine(tmp_path, daily, hourly)
    result = engine.tick()
    assert result.arms
    assert result.proposals == []
    assert broker.proposed_opens == []
    arm = journal.get_open_arm("SPY")
    assert arm is not None
    assert arm.status is ArmStatus.ARMED
    reasons = [p.get("reason") for k, _, p in _events(journal) if k == "no_entry"]
    assert "waiting_1h_pullback" in reasons


def test_engine_hybrid_ready_proposes_zero_orders(tmp_path):
    daily, hourly = hybrid_happy_daily_hourly()
    engine, broker, journal, _ = _engine(tmp_path, daily, hourly)
    result = engine.tick()
    assert any(not p.skip for p in result.proposals), [p.skip_reason for p in result.proposals]
    assert broker.submitted_order_ids == []
    assert journal.open_spreads()


def test_engine_1h_dip_does_not_cancel_arm(tmp_path):
    daily, _ = hybrid_happy_daily_hourly()
    hourly = hourly_waiting_no_tag(daily[27].ts)
    engine, _, journal, data = _engine(tmp_path, daily, hourly)
    engine.tick()
    assert journal.get_open_arm("SPY") is not None

    last = hourly[-1]
    dipped = list(hourly)
    dipped[-1] = last.__class__(
        ts=last.ts + timedelta(hours=1),
        open=101.0,
        high=103.0,
        low=98.5,
        close=99.0,
        volume=900_000,
    )
    data.bars_map["SPY"] = dipped
    engine.tick()
    arm = journal.get_open_arm("SPY")
    assert arm is not None
    cancel_reasons = [p.get("reason") for k, _, p in _events(journal) if k == "arm_cancel"]
    assert "daily_structure_break" not in cancel_reasons


def test_engine_daily_close_cancels_arm(tmp_path):
    daily, _ = hybrid_happy_daily_hourly()
    hourly = hourly_waiting_no_tag(daily[27].ts)
    engine, _, journal, data = _engine(tmp_path, daily, hourly)
    engine.tick()
    assert journal.get_open_arm("SPY") is not None

    broken = list(daily)
    last = broken[-1]
    broken[-1] = last.__class__(
        ts=last.ts + timedelta(days=1),
        open=101.0,
        high=103.0,
        low=98.5,
        close=99.0,
        volume=1_000_000,
    )
    data.daily_map["SPY"] = broken
    engine.tick()
    assert journal.get_open_arm("SPY") is None
    cancel_reasons = [p.get("reason") for k, _, p in _events(journal) if k == "arm_cancel"]
    assert "daily_structure_break" in cancel_reasons


def test_alpaca_timeframe_accepts_daily_and_hour():
    from alpaca.data.timeframe import TimeFrameUnit
    from alpaca_options_credit.broker.alpaca import _bars_start, _timeframe

    day = _timeframe("1Day")
    hour = _timeframe("1Hour")
    assert day.unit is TimeFrameUnit.Day
    assert hour.unit is TimeFrameUnit.Hour
    end = datetime(2026, 3, 4, tzinfo=timezone.utc)
    start = _bars_start("1Day", 60, end)
    assert (end - start).days >= 90
