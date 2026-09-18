"""Scan / arm / propose / reconcile loop. Dry-run places zero orders."""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

from alpaca_options_credit.broker.payloads import (
    close_credit_spread_payload,
    open_credit_spread_payload,
)
from alpaca_options_credit.calendar_stub import load_calendar, skip_new_entry
from alpaca_options_credit.config import var_dir
from alpaca_options_credit.heartbeat import Heartbeat, HeartbeatWriter
from alpaca_options_credit.journal import Journal
from alpaca_options_credit.models import (
    Arm,
    ArmStatus,
    Bar,
    OpenSpread,
    Side,
    SpreadKind,
    SpreadProposal,
    SpreadStatus,
)
from alpaca_options_credit.risk import decide
from alpaca_options_credit.rth import RTH_CLOSE, RTH_OPEN, is_rth, parse_hhmm
from alpaca_options_credit.strategy.spreads import (
    build_proposal,
    should_roll,
    stop_hit,
    take_profit_hit,
)
from alpaca_options_credit.strategy.structure import hybrid_entry

log = logging.getLogger(__name__)


@dataclass
class TickResult:
    status: str
    proposals: list[SpreadProposal]
    exits: list[str]
    arms: list[Arm]


class Engine:
    def __init__(
        self,
        cfg: dict[str, Any],
        journal: Journal,
        broker: Any,
        data: Any,
        *,
        dry_run: bool = True,
        heartbeat: Optional[HeartbeatWriter] = None,
        now_fn=None,
        calendar: Optional[dict[str, Any]] = None,
    ):
        self.cfg = cfg
        self.journal = journal
        self.broker = broker
        self.data = data
        self.dry_run = dry_run or bool(cfg.get("bot", {}).get("dry_run", True))
        self.heartbeat = heartbeat
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.loop = 0
        root = cfg.get("_repo_root")
        cal_rel = (cfg.get("calendar") or {}).get("file", "config/calendar.yaml")
        cal_path = __import__("pathlib").Path(cal_rel)
        if not cal_path.is_absolute() and root:
            cal_path = __import__("pathlib").Path(root) / cal_rel
        self.calendar = calendar if calendar is not None else load_calendar(cal_path)

    def _rth(self, now: datetime) -> bool:
        rth = self.cfg.get("rth") or {}
        return is_rth(
            now,
            open_t=parse_hhmm(str(rth.get("open", "09:30")), RTH_OPEN),
            close_t=parse_hhmm(str(rth.get("close", "16:00")), RTH_CLOSE),
        )

    def beat(self, status: str, detail: str = "") -> None:
        if not self.heartbeat:
            return
        self.heartbeat.write(
            Heartbeat(
                ts=self.now_fn().isoformat(),
                pid=os.getpid(),
                status=status,
                loop=self.loop,
                dry_run=self.dry_run,
                detail=detail,
            )
        )

    def tick(self) -> TickResult:
        self.loop += 1
        now = self.now_fn()
        rth = self._rth(now)
        proposals: list[SpreadProposal] = []
        exits: list[str] = []

        if rth:
            self.beat("rth_scan")
        else:
            self.beat("idle_off_hours")

        # Always persist/reconcile journaled spreads first.
        open_spreads = self.journal.open_spreads()
        if rth or (self.cfg.get("rth") or {}).get("manage_exits_off_hours"):
            exits.extend(self._manage_exits(open_spreads, now))
            open_spreads = self.journal.open_spreads()

        scan_ok = rth or not (self.cfg.get("rth") or {}).get("scan_only_rth", True)
        arms: list[Arm] = []
        if scan_ok:
            for symbol in (self.cfg.get("universe") or {}).get("symbols") or []:
                arm, prop = self._scan_symbol(symbol, now, open_spreads)
                if arm:
                    arms.append(arm)
                if prop:
                    proposals.append(prop)
                    if not prop.skip:
                        self._maybe_open(prop, open_spreads, now)
                        open_spreads = self.journal.open_spreads()

        return TickResult(
            status="rth_scan" if rth else "idle_off_hours",
            proposals=proposals,
            exits=exits,
            arms=arms,
        )

    def _tf_cfg(self) -> dict[str, Any]:
        return self.cfg.get("timeframe") or {}

    def _structure_bars(self, symbol: str) -> list[Bar]:
        # Locked hybrid: daily owns bias / VP / S/R / invalidation.
        # Legacy timeframe.bar (1H-only) is ignored if structure_bar is absent.
        tf = str(self._tf_cfg().get("structure_bar") or "1Day")
        md = self.cfg.get("market_data") or {}
        limit = int(md.get("daily_bar_lookback") or md.get("bar_lookback") or 60)
        return self.data.bars(symbol, tf, limit)

    def _timing_bars(self, symbol: str) -> list[Bar]:
        tf = str(self._tf_cfg().get("timing_bar") or "1Hour")
        limit = int((self.cfg.get("market_data") or {}).get("bar_lookback", 120))
        return self.data.bars(symbol, tf, limit)

    def _hybrid_kwargs(self) -> dict[str, Any]:
        tf = self._tf_cfg()
        vp = tf.get("volume_profile") or {}
        return {
            "left": int(tf.get("swing_left", 2)),
            "right": int(tf.get("swing_right", 2)),
            "atr_period": int(tf.get("atr_period", 14)),
            "vp_lookback": int(vp.get("lookback_bars", 25)),
            "vp_bin": float(vp.get("bin_size", 0.5)),
            "vp_percentile": float(vp.get("hvn_percentile", 0.70)),
            "no_chase_atr": float(tf.get("no_chase_atr", 0.5)),
        }

    def _scan_symbol(
        self,
        symbol: str,
        now: datetime,
        open_spreads: list[OpenSpread],
    ) -> tuple[Optional[Arm], Optional[SpreadProposal]]:
        tf = self._tf_cfg()
        daily = self._structure_bars(symbol)
        hourly = self._timing_bars(symbol)
        if len(daily) < 10:
            self.journal.log_event(
                "scan_skip", symbol, {"reason": "daily_not_confirmed", "detail": "not_enough_bars"}
            )
            return None, None

        existing = self.journal.get_open_arm(symbol)
        last_daily = daily[-1]
        armed_view = None
        if existing:
            armed_view = _view_from_arm(
                existing,
                last_daily,
                atr_v=0.0,
                confirm_ts=_confirm_ts_for_arm(existing, daily),
            )

        view, ready, why = hybrid_entry(daily, hourly, existing=armed_view, **self._hybrid_kwargs())

        if existing:
            if why == "daily_structure_break":
                self.journal.cancel_arm(existing.id, "daily_structure_break")
                self.journal.log_event(
                    "arm_cancel", symbol, {"reason": "daily_structure_break"}
                )
                log.info("daily structure break cancels arm %s %s", symbol, existing.id)
                return None, None
            # Timeout counted in daily structure bars after confirm_index.
            if last_daily and existing.bar_index is not None and (
                len(daily) - 1 - existing.bar_index
            ) > int(tf.get("arm_timeout_bars", 10)):
                self.journal.cancel_arm(existing.id, "arm_timeout")
                self.journal.log_event("arm_cancel", symbol, {"reason": "arm_timeout"})
                return None, None
            arm = existing
        elif view.confirmed and view.side and view.invalidation is not None:
            arm = Arm(
                id=str(uuid.uuid4()),
                symbol=symbol,
                side=view.side,
                invalidation=float(view.invalidation),
                zone_low=float(view.zone_low or view.invalidation),
                zone_high=float(view.zone_high or view.invalidation),
                confirmed_at=now.isoformat(),
                status=ArmStatus.ARMED,
                reason=view.reason,
                bar_index=view.confirm_index,
            )
            self.journal.upsert_arm(arm)
            self.journal.log_event(
                "arm",
                symbol,
                {
                    "side": arm.side.value,
                    "invalidation": arm.invalidation,
                    "zone": [arm.zone_low, arm.zone_high],
                    "reason": arm.reason,
                    "structure_bar": str(tf.get("structure_bar") or "1Day"),
                    "timing_bar": str(tf.get("timing_bar") or "1Hour"),
                },
            )
            log.info(
                "armed %s %s inv=%.2f reason=%s",
                symbol,
                arm.side.value,
                arm.invalidation,
                view.reason,
            )
        else:
            self.journal.log_event("scan_skip", symbol, {"reason": why, "detail": view.reason})
            log.info("reject %s %s", symbol, why)
            return None, None

        if not ready:
            self.journal.log_event("no_entry", symbol, {"reason": why})
            log.info("no_entry %s %s", symbol, why)
            return arm, None

        today = now.date() if isinstance(now, datetime) else date.today()
        skip_cal = skip_new_entry(self.calendar, self.cfg, symbol, today)
        if skip_cal:
            self.journal.log_event("calendar_skip", symbol, {"reason": skip_cal})
            return arm, None

        sp = self.cfg.get("spreads") or {}
        width = float(sp.get("width", 5.0))
        right = "put" if arm.side is Side.BULLISH else "call"
        chain = self.data.chain(
            symbol,
            right,
            int(sp.get("dte_min", 30)),
            int(sp.get("dte_max", 45)),
            arm.invalidation - width * 2,
            arm.invalidation + width * 2,
        )
        proposal = build_proposal(
            underlying=symbol,
            side=arm.side,
            invalidation=arm.invalidation,
            chain=chain,
            width=width,
            min_credit_pct=float(sp.get("min_credit_pct_of_width", 0.20)),
            today=today,
            dte_min=int(sp.get("dte_min", 30)),
            dte_max=int(sp.get("dte_max", 45)),
        )
        self.journal.log_event(
            "proposal",
            symbol,
            {
                "skip": proposal.skip,
                "skip_reason": proposal.skip_reason,
                "kind": proposal.kind.value,
                "credit": proposal.credit,
                "short": proposal.short.occ,
                "long": proposal.long.occ,
                "invalidation": proposal.invalidation,
                "pullback": why,
            },
        )
        log.info(
            "proposal %s skip=%s credit=%.2f short=%s",
            symbol,
            proposal.skip,
            proposal.credit,
            proposal.short.occ,
        )
        return arm, proposal

    def _maybe_open(
        self,
        proposal: SpreadProposal,
        open_spreads: list[OpenSpread],
        now: datetime,
    ) -> None:
        if proposal.skip:
            return
        risk_cfg = self.cfg.get("risk") or {}
        equity = self.broker.account_equity() or float(risk_cfg.get("paper_equity_fallback", 100000))
        sp = self.cfg.get("spreads") or {}
        decision = decide(
            equity=equity,
            proposal=proposal,
            open_spreads=open_spreads,
            risk_pct=float(risk_cfg.get("risk_per_trade_pct", 0.005)),
            max_concurrent=int(risk_cfg.get("max_concurrent", 8)),
            max_portfolio_risk_pct=float(risk_cfg.get("max_portfolio_risk_pct", 0.10)),
            one_per_underlying=bool(risk_cfg.get("one_spread_per_underlying", True)),
            multiplier=int(sp.get("multiplier", 100)),
        )
        if not decision.allow:
            self.journal.log_event(
                "risk_skip", proposal.underlying, {"reason": decision.reason}
            )
            return
        proposal.qty = decision.qty
        proposal.max_loss = decision.max_loss
        payload = open_credit_spread_payload(
            proposal,
            time_in_force=str(sp.get("time_in_force", "day")),
        )
        order_id = None
        if self.dry_run:
            # Observer: log the exact payload that *would* be sent; do not call live submit.
            log.info(
                "observer open %s %s qty=%s credit=%.2f (zero orders)",
                proposal.underlying,
                proposal.kind.value,
                proposal.qty,
                proposal.credit,
            )
            self.journal.log_event(
                "observer_open",
                proposal.underlying,
                {"payload": payload, "credit": proposal.credit, "qty": proposal.qty},
            )
            if getattr(self.broker, "dry_run", True):
                self.broker.submit_open(proposal, payload)
            order_id = None
            status = SpreadStatus.PROPOSED
        else:
            order_id = self.broker.submit_open(proposal, payload)
            status = SpreadStatus.OPEN

        spread = OpenSpread(
            id=str(uuid.uuid4()),
            underlying=proposal.underlying,
            kind=proposal.kind,
            short_occ=proposal.short.occ,
            long_occ=proposal.long.occ,
            width=proposal.width,
            credit=proposal.credit,
            qty=proposal.qty,
            max_loss=proposal.max_loss,
            invalidation=proposal.invalidation,
            status=status,
            opened_at=now.isoformat(),
            broker_order_id=order_id,
            expiration=proposal.short.expiration.isoformat(),
        )
        self.journal.upsert_spread(spread)
        arm = self.journal.get_open_arm(proposal.underlying)
        if arm:
            self.journal.mark_arm_triggered(arm.id, "spread_opened" if not self.dry_run else "observer_proposed")

    def _manage_exits(self, open_spreads: list[OpenSpread], now: datetime) -> list[str]:
        reasons: list[str] = []
        exits_cfg = self.cfg.get("exits") or {}
        tp_frac = float(exits_cfg.get("take_profit_frac_of_credit", 0.50))
        stop_mult = float(exits_cfg.get("stop_multiple_of_credit", 2.0))
        for spread in open_spreads:
            if spread.status == SpreadStatus.PROPOSED and self.dry_run:
                # Observer journal rows are proposals, not live risk — still evaluate marks.
                pass
            mark = self.data.spread_mark(spread.short_occ, spread.long_occ)
            reason = None
            thesis_intact = True
            bars = self._structure_bars(spread.underlying)
            if bars and (exits_cfg.get("honor_structure_break", True)):
                last = bars[-1]
                if spread.kind is SpreadKind.BULL_PUT_CREDIT and last.close < spread.invalidation:
                    reason = "structure_break"
                    thesis_intact = False
                if spread.kind is SpreadKind.BEAR_CALL_CREDIT and last.close > spread.invalidation:
                    reason = "structure_break"
                    thesis_intact = False
            if mark is not None and reason is None:
                if take_profit_hit(spread.credit, mark, tp_frac):
                    reason = "take_profit"
                elif stop_hit(spread.credit, mark, stop_mult):
                    reason = "stop_2x_credit"
            if not reason:
                continue

            dte = 0
            if spread.expiration:
                try:
                    dte = (date.fromisoformat(spread.expiration) - now.date()).days
                except ValueError:
                    dte = 0
            roll_cfg = exits_cfg.get("roll") or {}
            rolling = should_roll(roll_cfg=roll_cfg, thesis_intact=thesis_intact, dte=dte)
            payload = close_credit_spread_payload(
                short_occ=spread.short_occ,
                long_occ=spread.long_occ,
                qty=spread.qty or 1,
                debit=mark if mark is not None else spread.credit * 0.5,
            )
            if rolling:
                self.journal.log_event(
                    "roll_stub",
                    spread.underlying,
                    {"would_roll": True, "instead": "close_unless_execute", "dte": dte},
                )
            if self.dry_run:
                self.journal.log_event(
                    "observer_close",
                    spread.underlying,
                    {"reason": reason, "payload": payload, "mark": mark},
                )
                if getattr(self.broker, "dry_run", True):
                    self.broker.submit_close(spread, payload)
            else:
                self.broker.submit_close(spread, payload)
            self.journal.close_spread(spread.id, reason)
            reasons.append(f"{spread.underlying}:{reason}")
        return reasons

    def run_forever(self) -> None:
        loop_cfg = self.cfg.get("loop") or {}
        while True:
            result = self.tick()
            sleep = (
                float(loop_cfg.get("sleep_seconds_rth", 30))
                if result.status == "rth_scan"
                else float(loop_cfg.get("sleep_seconds_off_hours", 60))
            )
            time.sleep(sleep)


def _confirm_ts_for_arm(arm: Arm, daily: list[Bar]):
    if 0 <= arm.bar_index < len(daily):
        return daily[arm.bar_index].ts
    return None


def _view_from_arm(arm: Arm, last: Bar, atr_v: float, confirm_ts=None):
    from alpaca_options_credit.strategy.structure import StructureView

    return StructureView(
        side=arm.side,
        confirmed=True,
        invalidation=arm.invalidation,
        zone_low=arm.zone_low,
        zone_high=arm.zone_high,
        confirm_index=arm.bar_index,
        reason=arm.reason,
        last_close=last.close,
        atr=atr_v,
        confirm_ts=confirm_ts,
    )


def build_engine(
    cfg: dict[str, Any],
    *,
    dry_run: bool,
    fixture: bool,
    env=None,
) -> Engine:
    from alpaca_options_credit.broker.dry_run import DryRunBroker
    from alpaca_options_credit.broker.fixture_data import FixtureMarketData

    equity = float((cfg.get("risk") or {}).get("paper_equity_fallback", 100000))
    symbols = list((cfg.get("universe") or {}).get("symbols") or [])

    if fixture:
        cfg = dict(cfg)
        cfg["bot"] = {**cfg.get("bot", {}), "dry_run": True, "var_dir": "var/options-fixture"}
        rth = dict(cfg.get("rth") or {})
        rth["scan_only_rth"] = False  # fixture demo is runnable outside RTH
        cfg["rth"] = rth
        vdir = var_dir(cfg)
        journal = Journal(vdir / "journal.sqlite")
        hb = HeartbeatWriter(vdir / str((cfg.get("heartbeat") or {}).get("file", "heartbeat.json")))
        broker = DryRunBroker(equity=equity)
        data = FixtureMarketData(symbols)
        return Engine(cfg, journal, broker, data, dry_run=True, heartbeat=hb)

    vdir = var_dir(cfg)
    journal = Journal(vdir / "journal.sqlite")
    hb = HeartbeatWriter(vdir / str((cfg.get("heartbeat") or {}).get("file", "heartbeat.json")))

    from alpaca_options_credit.credentials import load_credentials
    from alpaca_options_credit.broker.alpaca import AlpacaBroker, AlpacaMarketData

    creds = load_credentials(env)
    data = AlpacaMarketData(creds, cfg)
    if dry_run:
        broker = DryRunBroker(equity=equity)
        # Prefer live account equity when keys exist, still zero orders.
        try:
            live = AlpacaBroker(creds)
            broker = DryRunBroker(equity=live.account_equity() or equity, account_number=live.account_number())
        except Exception as exc:
            log.warning("live account fetch skipped in observer: %s", type(exc).__name__)
        return Engine(cfg, journal, broker, data, dry_run=True, heartbeat=hb)

    broker = AlpacaBroker(creds)
    return Engine(cfg, journal, broker, data, dry_run=False, heartbeat=hb)
