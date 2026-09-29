"""Tight data budget: exits and armed setups before the rest of the watchlist."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from alpaca_options_credit.broker.dry_run import DryRunBroker
from alpaca_options_credit.config import load_config
from alpaca_options_credit.engine import Engine
from alpaca_options_credit.journal import Journal
from alpaca_options_credit.market_data_limit import MarketDataLimiter, replace_limiter, reset_limiter
from alpaca_options_credit.models import Arm, ArmStatus, OpenSpread, Side, SpreadKind, SpreadStatus


RTH = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)


class RecordingData:
    """Counts like a live data client. Returns no bars, so no orders are built."""

    limits_market_data = True

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def bars(self, symbol, timeframe, limit):
        self.calls.append(("bars", symbol, str(timeframe)))
        return []

    def chain(self, symbol, right, dte_min, dte_max, strike_lo, strike_hi):
        self.calls.append(("chain", symbol))
        return []

    def spread_mark(self, short_occ, long_occ):
        self.calls.append(("mark", short_occ, long_occ))
        # Above 50% of the 1.20 credit, so the exit check does not close the spread.
        return 0.90


def _engine(tmp_path, data, budget: int):
    clock = {"t": 0.0}

    def sleep(dt: float) -> None:
        clock["t"] += dt

    replace_limiter(MarketDataLimiter(budget, mono=lambda: clock["t"], sleep=sleep))
    cfg = load_config()
    cfg["bot"]["dry_run"] = True
    cfg["bot"]["var_dir"] = str(tmp_path / "var")
    cfg["_repo_root"] = str(tmp_path)
    cfg["universe"]["symbols"] = ["IWM", "QQQ", "SPY"]
    cfg["rth"]["scan_only_rth"] = True
    cfg["calendar"]["skip_fomc"] = False
    cfg["calendar"]["skip_earnings"] = False
    journal = Journal(tmp_path / "journal.sqlite")
    journal.upsert_spread(
        OpenSpread(
            id="sp1",
            underlying="QQQ",
            kind=SpreadKind.BULL_PUT_CREDIT,
            short_occ="QQQ260417P00450000",
            long_occ="QQQ260417P00445000",
            width=5.0,
            credit=1.20,
            qty=1,
            max_loss=380.0,
            invalidation=100.0,
            status=SpreadStatus.OPEN,
            opened_at="2026-03-03T00:00:00+00:00",
            expiration="2026-04-17",
        )
    )
    journal.upsert_arm(
        Arm(
            id="arm-spy",
            symbol="SPY",
            side=Side.BULLISH,
            invalidation=100.0,
            zone_low=99.0,
            zone_high=101.0,
            confirmed_at=RTH.isoformat(),
            status=ArmStatus.ARMED,
            reason="test",
            bar_index=1,
        )
    )
    engine = Engine(
        cfg,
        journal,
        DryRunBroker(equity=100_000),
        data,
        dry_run=True,
        now_fn=lambda: RTH,
        calendar={"fomc": [], "earnings": {}},
    )
    return engine


def test_tight_budget_serves_exits_then_armed_before_watchlist(tmp_path, caplog):
    data = RecordingData()
    try:
        engine = _engine(tmp_path, data, budget=4)
        with caplog.at_level("INFO"):
            engine.tick()
        # Exit check for the open QQQ spread (mark + daily bars) before any new scan.
        assert data.calls[0][0] == "mark"
        assert data.calls[1] == ("bars", "QQQ", "1Day")
        # Armed SPY is the next fresh fetch. IWM is the unarmed watchlist.
        assert ("bars", "SPY", "1Day") in data.calls
        assert ("bars", "SPY", "1Hour") in data.calls
        assert not any(call[1] == "IWM" for call in data.calls)
        spy_at = data.calls.index(("bars", "SPY", "1Day"))
        assert spy_at > 1
        assert "deferred" in caplog.text
        assert "does not fit" in caplog.text

        data.calls.clear()
        engine.tick()
        assert data.calls[0][0] == "mark"
        assert ("bars", "IWM", "1Day") in data.calls
        assert ("bars", "IWM", "1Hour") in data.calls
        # SPY was already scanned this pass. QQQ's scan is still waiting.
        # The QQQ daily bar above is the fresh exit check, not a cached scan.
        assert not any(call[1] == "SPY" for call in data.calls)
        assert not any(call[0] == "bars" and call[1] == "QQQ" and call[2] == "1Hour" for call in data.calls)
    finally:
        reset_limiter()


def test_unlimited_fixture_still_scans_every_symbol(tmp_path):
    """Dry-run fixtures do not set limits_market_data, so one tick still sees every name."""

    class OpenData(RecordingData):
        limits_market_data = False

    data = OpenData()
    engine = _engine(tmp_path, data, budget=1)
    engine.tick()
    scanned = {call[1] for call in data.calls if call[0] == "bars" and call[2] == "1Hour"}
    assert scanned == {"IWM", "QQQ", "SPY"}
    reset_limiter()
