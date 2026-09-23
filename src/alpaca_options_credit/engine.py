"""Scan / arm / propose / reconcile loop. Dry-run places zero orders."""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

from alpaca_options_credit.bar_quality import STALE_BARS, stale_bars_detail
from alpaca_options_credit.broker.payloads import (
    assert_atomic_mleg,
    close_credit_spread_payload,
    emergency_flatten_residual_leg_payload,
    open_credit_spread_payload,
)
from alpaca_options_credit.calendar_stub import load_calendar, skip_new_entry
from alpaca_options_credit.config import validate_exit_policy, var_dir
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
from alpaca_options_credit.rth import RTH_CLOSE, RTH_OPEN, as_et, is_rth, parse_hhmm
from alpaca_options_credit.strategy.spreads import (
    PRE_PROPOSAL_SKIP_REASONS,
    STOP_CREDIT_EXIT,
    build_proposal,
    entry_skip_event_kind,
    mid_credit,
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
        validate_exit_policy(self.cfg)
        self._overnight_flagged_date = None

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

        # Always persist/reconcile journaled spreads first — exits before entries.
        open_spreads = self.journal.open_spreads()
        if rth:
            self.beat("rth_scan", _open_spread_detail(open_spreads, prefix="rth_exits_first"))
        else:
            self._flag_overnight_opens(open_spreads, now)

        rth_cfg = self.cfg.get("rth") or {}
        submit_closes = bool(rth or rth_cfg.get("manage_exits_off_hours"))
        # Off-hours: still latch structure-break / already-due exits; do not submit
        # (options do not trade AH). First RTH poll submits before any new entry.
        if open_spreads or submit_closes:
            exits.extend(self._reconcile_naked_legs(open_spreads, now, submit=submit_closes))
            open_spreads = self.journal.open_spreads()
            exits.extend(self._manage_exits(open_spreads, now, submit=submit_closes))
            open_spreads = self.journal.open_spreads()

        scan_ok = rth or not rth_cfg.get("scan_only_rth", True)
        unprotected = [s for s in open_spreads if s.status is SpreadStatus.EXITING]
        if unprotected and not self.dry_run:
            self.journal.log_event(
                "entry_blocked_unprotected_exit",
                None,
                {
                    "count": len(unprotected),
                    "spreads": [
                        {
                            "id": s.id,
                            "underlying": s.underlying,
                            "exit_reason": s.exit_reason,
                            "close_attempts": s.close_attempts,
                            "last_close_error": s.last_close_error,
                        }
                        for s in unprotected
                    ],
                },
            )
            log.error(
                "fail-closed: %d credit spread(s) have a latched exit but no accepted "
                "close — blocking new entries this poll: %s",
                len(unprotected),
                ", ".join(f"{s.underlying}:{s.exit_reason}" for s in unprotected),
            )
            scan_ok = False

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
        # Fail closed before structure / arm / entry. A stale tail must not
        # confirm, arm, cancel, or open.
        daily_tf = str(tf.get("structure_bar") or "1Day")
        hourly_tf = str(tf.get("timing_bar") or "1Hour")
        stale = stale_bars_detail(daily, daily_tf, now) or stale_bars_detail(
            hourly, hourly_tf, now
        )
        if stale:
            self.journal.log_event(
                "scan_skip",
                symbol,
                {"reason": STALE_BARS, "detail": stale},
            )
            log.warning("stale bars %s %s", symbol, stale)
            return None, None
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
            max_leg_spread_pct_of_mid=float(sp.get("max_leg_spread_pct_of_mid") or 0),
            max_credit_pct_of_width=_max_credit_pct(sp),
            min_open_interest=int(sp.get("min_open_interest") or 0),
        )
        # Junk / debit / thin credit never reaches the proposal log. Dry-run
        # still places zero orders; these skips are counted by reason token.
        if proposal.skip and proposal.skip_reason in PRE_PROPOSAL_SKIP_REASONS:
            self._log_pre_proposal_skip(symbol, proposal, why)
            return arm, None

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

    def _log_pre_proposal_skip(self, symbol: str, proposal: SpreadProposal, why: str) -> None:
        kind = entry_skip_event_kind(proposal.skip_reason)
        mid = mid_credit(proposal.short, proposal.long)
        payload = {
            "reason": proposal.skip_reason,
            "skip_reason": proposal.skip_reason,
            "spread_kind": proposal.kind.value,
            "credit": proposal.credit,
            "mid_credit": mid,
            "width": proposal.width,
            "short": proposal.short.occ,
            "long": proposal.long.occ,
            "short_bid": proposal.short.bid,
            "short_ask": proposal.short.ask,
            "long_bid": proposal.long.bid,
            "long_ask": proposal.long.ask,
            "pullback": why,
        }
        self.journal.log_event(kind, symbol, payload)
        log.info(
            "%s %s reason=%s credit=%.2f short=%s long=%s",
            kind,
            symbol,
            proposal.skip_reason,
            proposal.credit,
            proposal.short.occ,
            proposal.long.occ,
        )

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
        payload = assert_atomic_mleg(
            open_credit_spread_payload(
                proposal,
                time_in_force=str(sp.get("time_in_force", "day")),
            ),
            intent="open",
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

    def _option_positions(self) -> Optional[dict[str, int]]:
        getter = getattr(self.broker, "option_positions", None)
        if getter is None:
            return {}
        try:
            return {str(k): int(v) for k, v in (getter() or {}).items()}
        except Exception as exc:
            log.error(
                "option_positions failed (%s) — cannot confirm legs are paired",
                type(exc).__name__,
            )
            return None

    def _flatten_residual_leg(
        self,
        spread: OpenSpread,
        occ: str,
        qty: int,
        *,
        flatten_short: bool,
        mark: Optional[float],
    ) -> bool:
        """CRITICAL path: flatten one leftover contract. Not a spread exit."""
        limit = max(abs(mark or 0.0), abs(spread.credit), 0.05)
        payload = emergency_flatten_residual_leg_payload(
            occ=occ,
            qty=qty,
            flatten_short=flatten_short,
            limit_price=limit,
        )
        flatten = getattr(self.broker, "flatten_residual", None)
        if flatten is None:
            attempts = self.journal.record_close_failure(
                spread.id, "broker has no flatten_residual"
            )
            self.journal.log_event(
                "naked_leg_critical",
                spread.underlying,
                {
                    "spread_id": spread.id,
                    "occ": occ,
                    "qty": qty,
                    "flatten_short": flatten_short,
                    "error": "broker has no flatten_residual",
                    "close_attempts": attempts,
                },
            )
            return False
        try:
            order_id = flatten(occ, payload)
        except Exception as exc:
            attempts = self.journal.record_close_failure(
                spread.id, f"{type(exc).__name__}: {exc}"
            )
            self.journal.log_event(
                "naked_leg_critical",
                spread.underlying,
                {
                    "spread_id": spread.id,
                    "occ": occ,
                    "qty": qty,
                    "flatten_short": flatten_short,
                    "error": f"{type(exc).__name__}: {exc}",
                    "close_attempts": attempts,
                },
            )
            log.critical(
                "NAKED LEG FLATTEN FAILED %s %s occ=%s attempt=%s: %s",
                spread.underlying,
                spread.id,
                occ,
                attempts,
                exc,
            )
            return False
        if not order_id and not self.dry_run:
            attempts = self.journal.record_close_failure(
                spread.id, "flatten_residual returned empty"
            )
            self.journal.log_event(
                "naked_leg_critical",
                spread.underlying,
                {
                    "spread_id": spread.id,
                    "occ": occ,
                    "error": "flatten_residual returned empty",
                    "close_attempts": attempts,
                },
            )
            return False
        return True

    def _reconcile_naked_legs(
        self,
        open_spreads: list[OpenSpread],
        now: datetime,
        *,
        submit: bool,
    ) -> list[str]:
        """If a mleg only filled one side, flatten the residual. Never rest a naked short."""
        positions = self._option_positions()
        if positions is None:
            self.journal.log_event("naked_leg_status_unknown", None, {})
            return []
        reasons: list[str] = []
        for spread in open_spreads:
            short_q = int(positions.get(spread.short_occ, 0) or 0)
            long_q = int(positions.get(spread.long_occ, 0) or 0)
            short_units = abs(short_q)
            long_units = abs(long_q)
            if short_units == 0 and long_units == 0:
                continue
            if short_units == long_units:
                continue
            self.journal.latch_exit(spread.id, "naked_leg", thesis_intact=False)
            self.journal.log_event(
                "naked_leg_critical",
                spread.underlying,
                {
                    "spread_id": spread.id,
                    "short_occ": spread.short_occ,
                    "long_occ": spread.long_occ,
                    "short_qty": short_q,
                    "long_qty": long_q,
                    "submit": submit,
                },
            )
            log.critical(
                "NAKED LEG %s %s short=%s qty=%s long=%s qty=%s — "
                "atomic mleg was broken; flatten residual immediately "
                "(never leave a naked short resting)",
                spread.underlying,
                spread.id,
                spread.short_occ,
                short_q,
                spread.long_occ,
                long_q,
            )
            if not submit:
                reasons.append(f"{spread.underlying}:naked_leg:latched_off_hours")
                continue

            mark = self.data.spread_mark(spread.short_occ, spread.long_occ)
            paired = min(short_units, long_units)
            accepted = True
            if paired > 0:
                payload = close_credit_spread_payload(
                    short_occ=spread.short_occ,
                    long_occ=spread.long_occ,
                    qty=paired,
                    debit=mark if mark is not None else spread.credit * 0.5,
                )
                accepted = self._attempt_mleg_close(spread, payload, "naked_leg", mark)
            leftover_short = short_units - paired
            leftover_long = long_units - paired
            if leftover_short:
                accepted = (
                    self._flatten_residual_leg(
                        spread,
                        spread.short_occ,
                        leftover_short,
                        flatten_short=True,
                        mark=mark,
                    )
                    and accepted
                )
            if leftover_long:
                accepted = (
                    self._flatten_residual_leg(
                        spread,
                        spread.long_occ,
                        leftover_long,
                        flatten_short=False,
                        mark=mark,
                    )
                    and accepted
                )
            if accepted:
                self.journal.close_spread(
                    spread.id,
                    "naked_leg",
                    close_debit=mark,
                    closed_at=now.isoformat(),
                )
                reasons.append(f"{spread.underlying}:naked_leg")
            else:
                reasons.append(f"{spread.underlying}:naked_leg:close_failed")
        return reasons

    def _flag_overnight_opens(self, open_spreads: list[OpenSpread], now: datetime) -> None:
        """Heartbeat + once-per-ET-date journal flag. No AH mleg submits."""
        detail = _open_spread_detail(open_spreads, prefix="overnight_open")
        self.beat("idle_off_hours", detail)
        if not open_spreads:
            return
        et_day = as_et(now).date()
        if self._overnight_flagged_date == et_day:
            return
        self._overnight_flagged_date = et_day
        payload = {
            "count": len(open_spreads),
            "spreads": [
                {
                    "id": s.id,
                    "underlying": s.underlying,
                    "status": s.status.value,
                    "exit_reason": s.exit_reason,
                    "qty": s.qty,
                }
                for s in open_spreads
            ],
        }
        self.journal.log_event("overnight_open", None, payload)
        log.warning(
            "overnight open credit spreads (options do not trade AH; first RTH poll "
            "closes before new entries): %s",
            ", ".join(f"{s.underlying}:{s.status.value}" for s in open_spreads),
        )

    def _evaluate_exit_reason(
        self,
        spread: OpenSpread,
        mark: Optional[float],
        *,
        allow_mark: bool,
    ) -> tuple[Optional[str], bool]:
        """Options-native only: structure-break and/or credit mark-to-close.

        No equity OCO/bracket. A latched EXITING reason is sticky even if the
        mark recovers — otherwise a failed close could leave the book naked.
        """
        if spread.status is SpreadStatus.EXITING and spread.exit_reason:
            return spread.exit_reason, spread.thesis_intact

        exits_cfg = self.cfg.get("exits") or {}
        reason: Optional[str] = None
        thesis_intact = True
        bars = self._structure_bars(spread.underlying)
        structure_tf = str(self._tf_cfg().get("structure_bar") or "1Day")
        stale = stale_bars_detail(bars, structure_tf, self.now_fn())
        if stale:
            # Do not structure-break on a stale daily tail. Mark exits still run.
            self.journal.log_event(
                "stale_bars",
                spread.underlying,
                {"reason": STALE_BARS, "detail": stale, "context": "exit_structure"},
            )
            log.warning("stale bars %s %s — skip structure exit", spread.underlying, stale)
            bars = []
        if bars and exits_cfg.get("honor_structure_break", True):
            last = bars[-1]
            if spread.kind is SpreadKind.BULL_PUT_CREDIT and last.close < spread.invalidation:
                reason = "structure_break"
                thesis_intact = False
            elif spread.kind is SpreadKind.BEAR_CALL_CREDIT and last.close > spread.invalidation:
                reason = "structure_break"
                thesis_intact = False
        if reason is None and allow_mark and mark is not None:
            tp_frac = float(exits_cfg.get("take_profit_frac_of_credit", 0.50))
            stop_mult = float(exits_cfg.get("stop_multiple_of_credit", 1.5))
            if take_profit_hit(spread.credit, mark, tp_frac):
                reason = "take_profit"
            elif stop_hit(spread.credit, mark, stop_mult):
                reason = STOP_CREDIT_EXIT
        return reason, thesis_intact

    def _working_close_ids(self) -> Optional[set[str]]:
        """Open broker order ids, or None if the query failed (do not replace)."""
        getter = getattr(self.broker, "open_order_ids", None)
        if getter is None:
            return set()
        try:
            return {str(i) for i in (getter() or []) if i}
        except Exception as exc:
            log.error(
                "cannot list open orders (%s) — will not cancel or replace a working close",
                type(exc).__name__,
            )
            return None

    def _attempt_mleg_close(
        self,
        spread: OpenSpread,
        payload: dict[str, Any],
        reason: str,
        mark: Optional[float],
    ) -> bool:
        """Submit one mleg close. Never cancel a working close to replace it.

        Returns True only when the close was accepted (journal may then close).
        Failures stay EXITING with a loud alert so the next poll retries.
        """
        assert_atomic_mleg(payload, intent="close")
        if self.dry_run:
            self.journal.log_event(
                "observer_close",
                spread.underlying,
                {"reason": reason, "payload": payload, "mark": mark},
            )
        try:
            order_id = self.broker.submit_close(spread, payload)
        except Exception as exc:
            attempts = self.journal.record_close_failure(
                spread.id, f"{type(exc).__name__}: {exc}"
            )
            self.journal.log_event(
                "close_failed",
                spread.underlying,
                {
                    "spread_id": spread.id,
                    "reason": reason,
                    "error": f"{type(exc).__name__}: {exc}",
                    "close_attempts": attempts,
                    "qty": spread.qty,
                },
            )
            log.error(
                "CLOSE FAILED %s %s reason=%s attempt=%s qty=%s: %s — "
                "spread stays open with latched exit; retry next poll",
                spread.underlying,
                spread.id,
                reason,
                attempts,
                spread.qty,
                exc,
            )
            return False

        if not order_id and not self.dry_run:
            attempts = self.journal.record_close_failure(
                spread.id, "submit_close returned empty"
            )
            self.journal.log_event(
                "close_failed",
                spread.underlying,
                {
                    "spread_id": spread.id,
                    "reason": reason,
                    "error": "submit_close returned empty",
                    "close_attempts": attempts,
                    "qty": spread.qty,
                },
            )
            log.error(
                "CLOSE FAILED %s %s reason=%s attempt=%s — empty broker id; "
                "spread stays open with latched exit; retry next poll",
                spread.underlying,
                spread.id,
                reason,
                attempts,
            )
            return False

        if order_id:
            self.journal.record_working_close(spread.id, str(order_id))
        return True

    def _manage_exits(
        self,
        open_spreads: list[OpenSpread],
        now: datetime,
        *,
        submit: bool = True,
    ) -> list[str]:
        """Software mark-to-close + structure-break. No equity OCO/brackets.

        Close qty is the journaled spread qty (no tranche leftover). A working
        mleg close is left alone — never cancelled to make room for a replacement.
        """
        reasons: list[str] = []
        exits_cfg = self.cfg.get("exits") or {}
        working_ids = self._working_close_ids() if submit else set()
        for spread in open_spreads:
            mark = self.data.spread_mark(spread.short_occ, spread.long_occ)
            reason, thesis_intact = self._evaluate_exit_reason(
                spread, mark, allow_mark=submit
            )
            if not reason:
                continue

            self.journal.latch_exit(spread.id, reason, thesis_intact=thesis_intact)
            live = self.journal.get_spread(spread.id) or spread
            reason = live.exit_reason or reason

            dte = 0
            if live.expiration:
                try:
                    dte = (date.fromisoformat(live.expiration) - now.date()).days
                except ValueError:
                    dte = 0
            roll_cfg = exits_cfg.get("roll") or {}
            rolling = should_roll(roll_cfg=roll_cfg, thesis_intact=thesis_intact, dte=dte)
            if rolling:
                self.journal.log_event(
                    "roll_stub",
                    live.underlying,
                    {"would_roll": True, "instead": "close_unless_execute", "dte": dte},
                )

            if not submit:
                reasons.append(f"{live.underlying}:{reason}:latched_off_hours")
                log.info(
                    "latched %s %s off-hours (no AH options trade); first RTH will close",
                    live.underlying,
                    reason,
                )
                continue

            qty = int(live.qty)
            if qty <= 0:
                attempts = self.journal.record_close_failure(
                    live.id, f"invalid close qty={live.qty}"
                )
                self.journal.log_event(
                    "close_failed",
                    live.underlying,
                    {
                        "spread_id": live.id,
                        "reason": reason,
                        "error": f"invalid close qty={live.qty}",
                        "close_attempts": attempts,
                    },
                )
                log.error(
                    "CLOSE FAILED %s %s invalid qty=%s — cannot invent a tranche size",
                    live.underlying,
                    live.id,
                    live.qty,
                )
                reasons.append(f"{live.underlying}:{reason}:invalid_qty")
                continue

            if live.exit_order_id:
                if working_ids is None:
                    self.journal.log_event(
                        "close_status_unknown",
                        live.underlying,
                        {
                            "spread_id": live.id,
                            "exit_order_id": live.exit_order_id,
                            "reason": reason,
                        },
                    )
                    log.error(
                        "working close %s for %s unconfirmed (order-list failed) — "
                        "not cancelling, not replacing",
                        live.exit_order_id,
                        live.underlying,
                    )
                    reasons.append(f"{live.underlying}:{reason}:working_unconfirmed")
                    continue
                if live.exit_order_id in working_ids:
                    log.info(
                        "working mleg close %s still live for %s qty=%s — "
                        "not cancelling, not replacing",
                        live.exit_order_id,
                        live.underlying,
                        qty,
                    )
                    reasons.append(f"{live.underlying}:{reason}:exit_working")
                    continue

            payload = close_credit_spread_payload(
                short_occ=live.short_occ,
                long_occ=live.long_occ,
                qty=qty,
                debit=mark if mark is not None else live.credit * 0.5,
            )
            accepted = self._attempt_mleg_close(live, payload, reason, mark)
            if not accepted:
                reasons.append(f"{live.underlying}:{reason}:close_failed")
                continue
            self.journal.close_spread(
                live.id, reason, close_debit=mark, closed_at=now.isoformat()
            )
            reasons.append(f"{live.underlying}:{reason}")
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


def _max_credit_pct(sp: dict[str, Any]) -> float:
    """Upper bound on natural credit / width. Missing → 1.0; 0 disables."""
    raw = sp.get("max_credit_pct_of_width", 1.0)
    if raw is None:
        return 1.0
    return float(raw)


def _open_spread_detail(open_spreads: list[OpenSpread], *, prefix: str) -> str:
    if not open_spreads:
        return f"{prefix}=0"
    names = ",".join(s.underlying for s in open_spreads)
    exiting = sum(1 for s in open_spreads if s.status is SpreadStatus.EXITING)
    return f"{prefix}={len(open_spreads)} exiting={exiting} {names}"


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
