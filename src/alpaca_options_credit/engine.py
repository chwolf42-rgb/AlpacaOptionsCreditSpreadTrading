"""Scan / arm / propose / reconcile loop. Dry-run places zero orders."""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

from alpaca_options_credit.bar_quality import (
    STALE_BARS,
    last_completed_daily_bar,
    next_bar_close,
    stale_bars_detail,
)
from alpaca_options_credit.broker.payloads import (
    assert_atomic_mleg,
    close_credit_spread_payload,
    emergency_flatten_residual_leg_payload,
    open_credit_spread_payload,
)
from alpaca_options_credit.calendar_stub import load_calendar, skip_new_entry
from alpaca_options_credit.close_prices import (
    SOURCE_FILL,
    SOURCE_QUOTE,
    CloseOrderView,
    EntryOrderView,
    as_debit,
)
from alpaca_options_credit.config import validate_exit_policy, var_dir
from alpaca_options_credit.heartbeat import Heartbeat, HeartbeatWriter
from alpaca_options_credit.http_bounds import current_inflight, is_transport_failure, set_inflight_hook
from alpaca_options_credit.journal import Journal
from alpaca_options_credit.market_data_limit import (
    PRIORITY_ARMED,
    PRIORITY_EXIT,
    PRIORITY_WATCH,
    PAGES_PER_SYMBOL,
    budget_per_min,
    data_priority,
    estimate_rth_scan_pages,
    get_limiter,
)
from alpaca_options_credit.models import (
    HELD_SPREAD_STATUSES,
    Arm,
    ArmStatus,
    Bar,
    OpenSpread,
    Side,
    SpreadKind,
    SpreadProposal,
    SpreadStatus,
)
from alpaca_options_credit.risk import decide, max_loss_dollars
from alpaca_options_credit.rth import RTH_CLOSE, RTH_OPEN, as_et, is_rth, parse_hhmm
from alpaca_options_credit.strategy.spreads import (
    DAILY_CLOSE_THROUGH_INV,
    PRE_PROPOSAL_SKIP_REASONS,
    STOP_CREDIT_EXIT,
    STRUCTURE_BREAK_EXIT,
    UNDERWATER_OPEN_BLOCKED,
    build_proposal,
    entry_skip_event_kind,
    mid_credit,
    should_roll,
    side_for_spread,
    stop_hit,
    take_profit_hit,
)
from alpaca_options_credit.strategy.structure import (
    daily_close_through_invalidation,
    hybrid_entry,
)

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
        self._beat_status = "starting"
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        set_inflight_hook(self._on_inflight)
        self.loop = 0
        root = cfg.get("_repo_root")
        cal_rel = (cfg.get("calendar") or {}).get("file", "config/calendar.yaml")
        cal_path = __import__("pathlib").Path(cal_rel)
        if not cal_path.is_absolute() and root:
            cal_path = __import__("pathlib").Path(root) / cal_rel
        self.calendar = calendar if calendar is not None else load_calendar(cal_path)
        validate_exit_policy(self.cfg)
        self._overnight_flagged_date = None
        self._legacy_entries_reconciled = False
        self._tick_data_pages = 0
        self._scan_queue: deque[str] = deque()
        self._scan_pass_active = False
        self._pass_members: set[str] = set()
        self._bar_memo: dict[str, dict[str, list[Bar]]] = {}
        self._bar_memo_until: dict[str, datetime] = {}

    def _rth(self, now: datetime) -> bool:
        rth = self.cfg.get("rth") or {}
        return is_rth(
            now,
            open_t=parse_hhmm(str(rth.get("open", "09:30")), RTH_OPEN),
            close_t=parse_hhmm(str(rth.get("close", "16:00")), RTH_CLOSE),
        )

    def _on_inflight(self) -> None:
        """Refresh the heartbeat at the edges of a blocking HTTP call."""
        self.beat(self._beat_status or "starting")

    def beat(self, status: str, detail: str = "") -> None:
        if not self.heartbeat:
            return
        snap = current_inflight()
        self.heartbeat.write(
            Heartbeat(
                ts=self.now_fn().isoformat(),
                pid=os.getpid(),
                status=status,
                loop=self.loop,
                dry_run=self.dry_run,
                detail=detail,
                inflight_op=snap.op,
                inflight_symbol=snap.symbol,
                inflight_since=snap.since,
            )
        )

    def tick(self) -> TickResult:
        self.loop += 1
        now = self.now_fn()
        rth = self._rth(now)
        self._beat_status = "rth_scan" if rth else "idle_off_hours"
        proposals: list[SpreadProposal] = []
        exits: list[str] = []
        self._tick_data_pages = 0
        self._bar_memo = {}
        self._bar_memo_until = {}
        if getattr(self.data, "limits_market_data", False):
            get_limiter().set_wait_hook(
                lambda: self.beat(
                    "rth_scan" if rth else "idle_off_hours",
                    "market-data pace",
                )
            )

        # Reconcile working entries before any exit. A day mleg that has not
        # filled is not a position: do not close it, flatten it, or flag it
        # overnight. Exits still run before new entries.
        open_spreads = self.journal.open_spreads()
        if not self.dry_run:
            self._reconcile_legacy_open_entries()
            open_spreads = self.journal.open_spreads()
        if rth:
            self.beat("rth_scan", _open_spread_detail(open_spreads, prefix="rth_exits_first"))
        else:
            self._flag_overnight_opens(open_spreads, now)

        rth_cfg = self.cfg.get("rth") or {}
        submit_closes = bool(rth or rth_cfg.get("manage_exits_off_hours"))
        # Off-hours: still latch structure-break / already-due exits; do not submit
        # (options do not trade AH). First RTH poll submits before any new entry.
        # Pending entries are reconciled here too: a structure break cancels the
        # working entry instead of submitting a close.
        if open_spreads or submit_closes:
            with data_priority(PRIORITY_EXIT):
                if not self.dry_run:
                    self._reconcile_pending_entries(open_spreads, now)
                    open_spreads = self.journal.open_spreads()
                held = _held_spreads(open_spreads)
                exits.extend(self._reconcile_naked_legs(held, now, submit=submit_closes))
                open_spreads = self.journal.open_spreads()
                held = _held_spreads(open_spreads)
                exits.extend(self._manage_exits(held, now, submit=submit_closes))
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
            arms, proposals = self._scan_universe(now, open_spreads, rth=rth)

        return TickResult(
            status="rth_scan" if rth else "idle_off_hours",
            proposals=proposals,
            exits=exits,
            arms=arms,
        )

    def _tf_cfg(self) -> dict[str, Any]:
        return self.cfg.get("timeframe") or {}

    def _limits_market_data(self) -> bool:
        return bool(getattr(self.data, "limits_market_data", False))

    def _note_pages(self, n: int) -> None:
        """Count a fresh data-API page. No-op for fixture data (no HTTP cap)."""
        if n > 0 and self._limits_market_data():
            self._tick_data_pages += n

    def _structure_spec(self) -> tuple[str, int]:
        # Locked hybrid: daily owns bias / VP / S/R / invalidation.
        # Legacy timeframe.bar (1H-only) is ignored if structure_bar is absent.
        tf = str(self._tf_cfg().get("structure_bar") or "1Day")
        md = self.cfg.get("market_data") or {}
        limit = int(md.get("daily_bar_lookback") or md.get("bar_lookback") or 60)
        return tf, limit

    def _timing_spec(self) -> tuple[str, int]:
        tf = str(self._tf_cfg().get("timing_bar") or "1Hour")
        limit = int((self.cfg.get("market_data") or {}).get("bar_lookback", 120))
        return tf, limit

    def _can_batch_bars(self) -> bool:
        return self._limits_market_data() and callable(getattr(self.data, "bars_for_symbols", None))

    def _batch_symbols(self, symbol: str) -> list[str]:
        symbols = list((self.cfg.get("universe") or {}).get("symbols") or [])
        if symbol and symbol not in symbols:
            symbols = [symbol, *symbols]
        return symbols

    def _prime_timeframe(self, symbols: list[str], timeframe: str, limit: int, now: datetime) -> None:
        batch = self.data.bars_for_symbols(list(symbols), timeframe, int(limit), now=now)
        self._note_pages(int(getattr(batch, "pages", 0) or 0))
        by_symbol = getattr(batch, "by_symbol", None) or {}
        key = str(timeframe)
        self._bar_memo[key] = {name: list(by_symbol.get(name) or []) for name in symbols}
        # Drop the memo at the next close so a bar that closes mid-pass is
        # fetched before any later symbol reads it.
        self._bar_memo_until[key] = next_bar_close(timeframe, now)

    def _batched_bars(self, symbol: str, timeframe: str, limit: int) -> list[Bar]:
        key = str(timeframe)
        now = self.now_fn()
        memo = self._bar_memo.get(key)
        until = self._bar_memo_until.get(key)
        if memo is not None and symbol in memo and until is not None and now < until:
            return list(memo[symbol])
        self._prime_timeframe(self._batch_symbols(symbol), timeframe, limit, now)
        memo = self._bar_memo.get(key) or {}
        return list(memo.get(symbol) or [])

    def _structure_bars(self, symbol: str) -> list[Bar]:
        tf, limit = self._structure_spec()
        if self._can_batch_bars():
            return self._batched_bars(symbol, tf, limit)
        self._note_pages(1)
        return self.data.bars(symbol, tf, limit)

    def _timing_bars(self, symbol: str) -> list[Bar]:
        tf, limit = self._timing_spec()
        if self._can_batch_bars():
            return self._batched_bars(symbol, tf, limit)
        self._note_pages(1)
        return self.data.bars(symbol, tf, limit)

    def _spread_mark(self, short_occ: str, long_occ: str) -> Optional[float]:
        self._note_pages(1)
        return self.data.spread_mark(short_occ, long_occ)

    def _ordered_symbols(self, symbols: list[str]) -> list[str]:
        """Armed setups first, then the rest of the universe."""
        armed = [s for s in symbols if self.journal.get_open_arm(s)]
        armed_set = set(armed)
        rest = [s for s in symbols if s not in armed_set]
        return armed + rest

    def _reprioritize(self, pending: list[str], symbols: list[str]) -> list[str]:
        """Armed names still waiting jump ahead of the watchlist. Already scanned names stay done."""
        allowed = set(symbols)
        kept: list[str] = []
        seen: set[str] = set()
        for symbol in pending:
            if symbol in allowed and symbol not in seen:
                kept.append(symbol)
                seen.add(symbol)
        for symbol in symbols:
            if symbol not in seen and symbol not in self._pass_members:
                kept.append(symbol)
                seen.add(symbol)
                self._pass_members.add(symbol)
        self._pass_members &= allowed | seen
        return self._ordered_symbols(kept)

    def _scan_universe(
        self,
        now: datetime,
        open_spreads: list[OpenSpread],
        *,
        rth: bool,
    ) -> tuple[list[Arm], list[SpreadProposal]]:
        """Scan bars. Exit checks have already run.

        A client with ``bars_for_symbols`` fetches each timeframe once for the
        whole universe and walks every name this loop. Closed bars are reused
        by that client until the next close. Clients without it pace one page
        per symbol and finish the pass on a later loop, still with fresh fetches.
        """
        symbols = list((self.cfg.get("universe") or {}).get("symbols") or [])
        arms: list[Arm] = []
        proposals: list[SpreadProposal] = []
        if not self._limits_market_data():
            for symbol in self._ordered_symbols(symbols):
                self._consume_scan(symbol, now, open_spreads, arms, proposals)
            return arms, proposals
        if self._can_batch_bars():
            return self._scan_batched(symbols, now, open_spreads, rth=rth)

        budget = budget_per_min()
        if not self._scan_pass_active:
            self._scan_queue = deque(self._ordered_symbols(symbols))
            self._pass_members = set(symbols)
            self._scan_pass_active = True
            self._log_pass_budget(symbols, budget)
        else:
            self._scan_queue = deque(self._reprioritize(list(self._scan_queue), symbols))

        while self._scan_queue and self._tick_data_pages < budget:
            symbol = self._scan_queue.popleft()
            self.beat(
                "rth_scan" if rth else "idle_off_hours",
                f"scan {symbol} pages={self._tick_data_pages}/{budget}",
            )
            self._consume_scan(symbol, now, open_spreads, arms, proposals)

        deferred = len(self._scan_queue)
        if deferred:
            armed_left = sum(1 for s in self._scan_queue if self.journal.get_open_arm(s))
            log.info(
                "market-data scan %d/%d pages this cycle; deferred %d symbol(s) "
                "(%d armed, %d watch) for a fresh fetch next cycle — no cached bars",
                self._tick_data_pages,
                budget,
                deferred,
                armed_left,
                deferred - armed_left,
            )
        else:
            self._scan_pass_active = False
            log.info(
                "market-data scan pass complete: %d/%d pages this cycle",
                self._tick_data_pages,
                budget,
            )
        return arms, proposals

    def _scan_batched(
        self,
        symbols: list[str],
        now: datetime,
        open_spreads: list[OpenSpread],
        *,
        rth: bool,
    ) -> tuple[list[Arm], list[SpreadProposal]]:
        """One loop for the universe. Stock bars are already one request per timeframe.

        The first exit mark ran before this. Daily bars may already be primed
        by that exit's structure read. Option snapshots and quotes are still
        fetched inside the exit check and inside any entry-ready chain.
        """
        arms: list[Arm] = []
        proposals: list[SpreadProposal] = []
        budget = budget_per_min()
        with data_priority(PRIORITY_ARMED):
            if symbols:
                self._structure_bars(symbols[0])
                self._timing_bars(symbols[0])
        cold = estimate_rth_scan_pages(len(symbols), 0)
        log.info(
            "market-data batched scan: %d/%d pages so far, cold bar pass is %d "
            "page(s) for %d symbol(s)",
            self._tick_data_pages,
            budget,
            cold,
            len(symbols),
        )
        for symbol in self._ordered_symbols(symbols):
            self.beat(
                "rth_scan" if rth else "idle_off_hours",
                f"scan {symbol} pages={self._tick_data_pages}/{budget}",
            )
            self._consume_scan(symbol, now, open_spreads, arms, proposals)
        self._scan_pass_active = False
        self._scan_queue.clear()
        if self._tick_data_pages > budget:
            log.info(
                "market-data scan used %d/%d pages this cycle; the rolling cap "
                "paces the overflow. Exits already ran",
                self._tick_data_pages,
                budget,
            )
        else:
            log.info(
                "market-data scan pass complete: %d/%d pages this cycle",
                self._tick_data_pages,
                budget,
            )
        return arms, proposals

    def _log_pass_budget(self, symbols: list[str], budget: int) -> None:
        # Exit checks already ran this tick. Count those pages even if a spread
        # closed during the check, so the line matches what was just fetched.
        exit_pages = self._tick_data_pages
        pages = len(symbols) * PAGES_PER_SYMBOL + exit_pages
        if pages > budget:
            log.info(
                "full scan needs about %d data pages (%d symbols x 2 bars + "
                "%d exit-check pages) and does not fit in %d/min; "
                "pacing fresh fetches — exits, then armed setups, then the watchlist",
                pages,
                len(symbols),
                exit_pages,
                budget,
            )

    def _consume_scan(
        self,
        symbol: str,
        now: datetime,
        open_spreads: list[OpenSpread],
        arms: list[Arm],
        proposals: list[SpreadProposal],
    ) -> None:
        level = PRIORITY_ARMED if self.journal.get_open_arm(symbol) else PRIORITY_WATCH
        with data_priority(level):
            arm, prop = self._scan_symbol(symbol, now, open_spreads)
        if arm:
            arms.append(arm)
        if prop:
            proposals.append(prop)
            if not prop.skip:
                self._maybe_open(prop, open_spreads, now)
                open_spreads[:] = self.journal.open_spreads()

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

        # Aged arms: the Monday invalidation can be underwater by the time a
        # later 1h reconfirm fires. Block before strike selection so we do not
        # emit an openable proposal. Structure-break *exits* are unchanged.
        if self._open_blocked_by_daily_close(symbol, arm.side, arm.invalidation, daily, now):
            return arm, None

        sp = self.cfg.get("spreads") or {}
        width = float(sp.get("width", 5.0))
        right = "put" if arm.side is Side.BULLISH else "call"
        self._note_pages(1)
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
            min_short_inv_gap=float(sp.get("min_short_inv_gap", 1.0)),
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

    def _open_blocked_by_daily_close(
        self,
        symbol: str,
        side: Side,
        invalidation: float,
        bars: list[Bar],
        now: datetime,
    ) -> bool:
        """Refuse an open when the last completed daily close is through inv.

        Does not change arm-cancel or the structure_break exit. A forming
        session bar cannot hide a prior completed close that already broke.
        """
        completed = last_completed_daily_bar(bars, now)
        if completed is None:
            self.journal.log_event(
                "entry_skip",
                symbol,
                {
                    "reason": UNDERWATER_OPEN_BLOCKED,
                    "detail": "no_completed_daily_bar",
                    "skip_reason": UNDERWATER_OPEN_BLOCKED,
                    "invalidation": invalidation,
                    "side": side.value,
                },
            )
            log.info("underwater open blocked %s — no completed daily close", symbol)
            return True
        if not daily_close_through_invalidation(side, invalidation, completed.close):
            return False
        self.journal.log_event(
            "entry_skip",
            symbol,
            {
                "reason": UNDERWATER_OPEN_BLOCKED,
                "detail": DAILY_CLOSE_THROUGH_INV,
                "skip_reason": UNDERWATER_OPEN_BLOCKED,
                "invalidation": invalidation,
                "last_daily_close": completed.close,
                "side": side.value,
            },
        )
        log.info(
            "underwater open blocked %s close=%.4f inv=%.4f",
            symbol,
            completed.close,
            invalidation,
        )
        return True

    def _maybe_open(
        self,
        proposal: SpreadProposal,
        open_spreads: list[OpenSpread],
        now: datetime,
    ) -> None:
        if proposal.skip:
            return
        # Fill-time gate. A proposal built on an earlier bar must not become
        # an observer fill or a live open after the completed daily close
        # has gone through invalidation.
        daily = self._structure_bars(proposal.underlying)
        if self._open_blocked_by_daily_close(
            proposal.underlying,
            side_for_spread(proposal.kind),
            proposal.invalidation,
            daily,
            now,
        ):
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
            try:
                order_id = self.broker.submit_open(proposal, payload)
            except Exception as exc:
                self.journal.log_event(
                    "entry_submit_failed",
                    proposal.underlying,
                    {"error": f"{type(exc).__name__}: {exc}"},
                )
                log.error(
                    "ENTRY SUBMIT FAILED %s: %s — spread not journaled",
                    proposal.underlying,
                    exc,
                )
                return
            if not order_id:
                self.journal.log_event(
                    "entry_submit_failed",
                    proposal.underlying,
                    {"error": "empty order id"},
                )
                log.error(
                    "ENTRY SUBMIT FAILED %s — empty broker id; spread not journaled",
                    proposal.underlying,
                )
                return
            # Day mleg may never fill. Stay pending until the broker reports
            # a fill; the limit credit is not the fill price.
            status = SpreadStatus.PENDING_ENTRY

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
            entry_order_id=order_id if status is SpreadStatus.PENDING_ENTRY else None,
            entry_limit_credit=(
                proposal.credit if status is SpreadStatus.PENDING_ENTRY else None
            ),
        )
        self.journal.upsert_spread(spread)
        if status is SpreadStatus.PENDING_ENTRY:
            self.journal.log_event(
                "entry_submitted",
                proposal.underlying,
                {
                    "spread_id": spread.id,
                    "order_id": order_id,
                    "limit_credit": proposal.credit,
                    "qty": proposal.qty,
                    "short_occ": proposal.short.occ,
                    "long_occ": proposal.long.occ,
                },
            )
        arm = self.journal.get_open_arm(proposal.underlying)
        if arm:
            self.journal.mark_arm_triggered(arm.id, "spread_opened" if not self.dry_run else "observer_proposed")

    def _option_positions(self) -> Optional[dict[str, int]]:
        getter = getattr(self.broker, "option_positions", None)
        if getter is None:
            return {}
        try:
            raw = getter()
        except Exception as exc:
            log.error(
                "option_positions failed (%s) — cannot confirm legs are paired",
                type(exc).__name__,
            )
            return None
        if raw is None:
            log.error(
                "option_positions unavailable — will not treat the book as flat"
            )
            return None
        return {str(k): int(v) for k, v in raw.items()}

    def _fetch_entry_order(self, order_id: str) -> Optional[EntryOrderView]:
        getter = getattr(self.broker, "get_entry_order", None)
        if getter is None or not order_id:
            return None
        try:
            view = getter(order_id)
        except Exception as exc:
            log.error(
                "entry order lookup failed %s (%s) — leaving the journal state unchanged",
                order_id,
                type(exc).__name__,
            )
            return None
        return view

    def _reconcile_legacy_open_entries(self) -> None:
        """Repair OPEN rows journaled before the entry mleg filled.

        A still-working order becomes PENDING_ENTRY. A terminal order with no
        fill and no position in either leg becomes ENTRY_EXPIRED or CANCELLED.
        A filled order, or any leg still on the book, stays OPEN. Unknown
        broker state is retried on the next poll.
        """
        if self.dry_run or self._legacy_entries_reconciled:
            return
        candidates = [
            s
            for s in self.journal.open_spreads()
            if s.status is SpreadStatus.OPEN and (s.entry_order_id or s.broker_order_id)
        ]
        if not candidates:
            self._legacy_entries_reconciled = True
            return
        positions = self._option_positions()
        unknown = False
        for spread in candidates:
            order_id = str(spread.entry_order_id or spread.broker_order_id or "")
            view = self._fetch_entry_order(order_id)
            if view is None:
                unknown = True
                self.journal.log_event(
                    "legacy_entry_status_unknown",
                    spread.underlying,
                    {"spread_id": spread.id, "order_id": order_id},
                )
                log.error(
                    "legacy open %s order %s unconfirmed — not marking it filled or dead",
                    spread.underlying,
                    order_id,
                )
                continue
            if view.state == "open":
                if not self.journal.mark_pending_entry(spread.id, order_id):
                    unknown = True
                    continue
                if view.filled_qty > 0:
                    self.journal.note_entry_progress(spread.id, view.filled_qty)
                self.journal.log_event(
                    "legacy_entry_still_working",
                    spread.underlying,
                    {
                        "spread_id": spread.id,
                        "order_id": order_id,
                        "filled_qty": view.filled_qty,
                        "order_qty": view.order_qty,
                    },
                )
                log.warning(
                    "legacy OPEN %s order %s is still working — treating as pending "
                    "entry (not a held spread)",
                    spread.underlying,
                    order_id,
                )
                continue
            if view.state == "filled" or view.filled_qty > 0:
                self._repair_legacy_filled_open(spread, view, order_id)
                continue
            if positions is None:
                unknown = True
                self.journal.log_event(
                    "legacy_entry_positions_unknown",
                    spread.underlying,
                    {
                        "spread_id": spread.id,
                        "order_id": order_id,
                        "raw_status": view.raw_status,
                    },
                )
                log.error(
                    "legacy OPEN %s order %s is %s with no fill, but positions are "
                    "unknown — not freeing the slot",
                    spread.underlying,
                    order_id,
                    view.raw_status,
                )
                continue
            short_q = int(positions.get(spread.short_occ, 0) or 0)
            long_q = int(positions.get(spread.long_occ, 0) or 0)
            if short_q != 0 or long_q != 0:
                self.journal.log_event(
                    "legacy_entry_positions_held",
                    spread.underlying,
                    {
                        "spread_id": spread.id,
                        "order_id": order_id,
                        "short_qty": short_q,
                        "long_qty": long_q,
                        "raw_status": view.raw_status,
                    },
                )
                log.warning(
                    "legacy OPEN %s order %s is terminal-unfilled but a leg is still "
                    "on the book (short=%s long=%s) — left OPEN",
                    spread.underlying,
                    order_id,
                    short_q,
                    long_q,
                )
                continue
            self._retire_unfilled_entry(spread, view, order_id, legacy=True)
        if not unknown:
            self._legacy_entries_reconciled = True

    def _repair_legacy_filled_open(
        self, spread: OpenSpread, view: EntryOrderView, order_id: str
    ) -> None:
        """Keep a filled legacy OPEN, but store the actual fill when it differs.

        Rows already in a close (``close_filled_qty`` > 0) are left alone.
        """
        if int(spread.close_filled_qty or 0) > 0:
            log.info(
                "legacy OPEN %s order %s already closing — left as journaled",
                spread.underlying,
                order_id,
            )
            return
        qty = int(view.filled_qty or 0) or int(spread.qty)
        priced = (
            view.filled_avg_credit
            if view.filled_avg_credit is not None and view.filled_avg_credit > 0
            else None
        )
        credit = float(priced if priced is not None else spread.credit)
        qty_differs = qty != int(spread.qty)
        credit_differs = (
            priced is not None and abs(credit - float(spread.credit)) > 1e-6
        )
        if not qty_differs and not credit_differs:
            log.info(
                "legacy OPEN %s order %s already filled qty=%s — left OPEN",
                spread.underlying,
                order_id,
                qty,
            )
            return
        multiplier = int((self.cfg.get("spreads") or {}).get("multiplier", 100))
        max_loss = max_loss_dollars(spread.width, credit, multiplier) * qty
        if not self.journal.apply_open_fill(
            spread.id,
            qty=qty,
            credit=credit,
            max_loss=max_loss,
            filled_qty=qty,
        ):
            return
        limit = (
            spread.entry_limit_credit
            if spread.entry_limit_credit is not None
            else spread.credit
        )
        self.journal.log_event(
            "legacy_entry_filled",
            spread.underlying,
            {
                "spread_id": spread.id,
                "order_id": order_id,
                "filled_qty": qty,
                "ordered_qty": spread.qty,
                "limit_credit": limit,
                "filled_credit": priced,
                "credit": credit,
                "max_loss": max_loss,
            },
        )
        log.info(
            "legacy OPEN %s reconciled to fill qty=%s credit=%.2f (was %.2f)",
            spread.underlying,
            qty,
            credit,
            float(spread.credit),
        )

    def _reconcile_pending_entries(
        self, open_spreads: list[OpenSpread], now: datetime
    ) -> None:
        if self.dry_run:
            return
        for spread in open_spreads:
            if spread.status is not SpreadStatus.PENDING_ENTRY:
                continue
            self._reconcile_one_pending_entry(spread, now)

    def _reconcile_one_pending_entry(self, spread: OpenSpread, now: datetime) -> None:
        order_id = str(spread.entry_order_id or spread.broker_order_id or "")
        if not order_id:
            self.journal.log_event(
                "entry_order_missing",
                spread.underlying,
                {"spread_id": spread.id},
            )
            log.error(
                "pending entry %s has no order id — not promoting and not expiring",
                spread.underlying,
            )
            return
        view = self._fetch_entry_order(order_id)
        if view is None:
            self.journal.log_event(
                "entry_status_unknown",
                spread.underlying,
                {"spread_id": spread.id, "order_id": order_id},
            )
            log.error(
                "pending entry %s order %s unconfirmed — not promoting and not expiring",
                spread.underlying,
                order_id,
            )
            return
        if view.state == "open":
            self._track_working_entry(spread, view, order_id, now)
            return
        self._apply_terminal_entry(spread, view, order_id)

    def _track_working_entry(
        self,
        spread: OpenSpread,
        view: EntryOrderView,
        order_id: str,
        now: datetime,
    ) -> None:
        if view.filled_qty > int(spread.entry_filled_qty or 0):
            self.journal.note_entry_progress(spread.id, view.filled_qty)
            self.journal.log_event(
                "entry_partial_working",
                spread.underlying,
                {
                    "spread_id": spread.id,
                    "order_id": order_id,
                    "filled_qty": view.filled_qty,
                    "order_qty": view.order_qty or spread.qty,
                    "filled_credit": view.filled_avg_credit,
                },
            )
            log.info(
                "pending entry %s partial fill %s/%s — still working, not closing",
                spread.underlying,
                view.filled_qty,
                view.order_qty or spread.qty,
            )
        # pending_cancel is still working. Do not submit a close for it.
        if view.raw_status == "pending_cancel":
            return
        if not self._pending_structure_broken(spread):
            return
        self._cancel_working_entry(spread, order_id, now)

    def _pending_structure_broken(self, spread: OpenSpread) -> bool:
        """Same daily structure-break as an exit. Marks are not an entry cancel."""
        reason, _thesis = self._evaluate_exit_reason(spread, None, allow_mark=False)
        return reason == STRUCTURE_BREAK_EXIT

    def _cancel_working_entry(
        self, spread: OpenSpread, order_id: str, now: datetime
    ) -> None:
        cancel = getattr(self.broker, "cancel_entry_order", None)
        if cancel is None:
            self.journal.log_event(
                "entry_cancel_failed",
                spread.underlying,
                {
                    "spread_id": spread.id,
                    "order_id": order_id,
                    "error": "broker has no cancel_entry_order",
                },
            )
            log.error(
                "structure break on pending %s but broker cannot cancel entry %s — "
                "not submitting a close",
                spread.underlying,
                order_id,
            )
            return
        try:
            cancel(order_id)
        except Exception as exc:
            self.journal.log_event(
                "entry_cancel_failed",
                spread.underlying,
                {
                    "spread_id": spread.id,
                    "order_id": order_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            log.error(
                "cancel entry failed %s %s (%s) — not submitting a close",
                spread.underlying,
                order_id,
                type(exc).__name__,
            )
            return
        self.journal.log_event(
            "entry_cancel_structure_break",
            spread.underlying,
            {
                "spread_id": spread.id,
                "order_id": order_id,
                "as_of": now.isoformat(),
            },
        )
        log.warning(
            "cancelled working entry %s order %s — structure break, no close submitted",
            spread.underlying,
            order_id,
        )
        view = self._fetch_entry_order(order_id)
        if view is None or view.state == "open":
            if view is not None and view.filled_qty > int(spread.entry_filled_qty or 0):
                self.journal.note_entry_progress(spread.id, view.filled_qty)
            return
        fresh = self.journal.get_spread(spread.id) or spread
        self._apply_terminal_entry(fresh, view, order_id)

    def _apply_terminal_entry(
        self, spread: OpenSpread, view: EntryOrderView, order_id: str
    ) -> None:
        # A terminal partial is a real (smaller) spread. The mleg is atomic,
        # so filled_qty is paired units, not a naked leg.
        if view.state == "filled" or view.filled_qty > 0:
            self._promote_filled_entry(spread, view, order_id)
            return
        self._retire_unfilled_entry(spread, view, order_id, legacy=False)

    def _retire_unfilled_entry(
        self,
        spread: OpenSpread,
        view: EntryOrderView,
        order_id: str,
        *,
        legacy: bool,
    ) -> None:
        status = _terminal_entry_status(view.raw_status)
        reason = _terminal_entry_reason(status)
        wrote = self.journal.mark_entry_terminal(
            spread.id, status, reason=reason, allow_open=legacy
        )
        if not wrote:
            return
        payload = {
            "spread_id": spread.id,
            "order_id": order_id,
            "raw_status": view.raw_status,
            "filled_qty": view.filled_qty,
            "legacy": legacy,
        }
        if legacy:
            self.journal.log_event("legacy_entry_unfilled", spread.underlying, payload)
        self.journal.log_event(reason, spread.underlying, payload)
        log.info(
            "entry %s %s order %s status=%s — slot freed",
            reason,
            spread.underlying,
            order_id,
            view.raw_status,
        )

    def _promote_filled_entry(
        self, spread: OpenSpread, view: EntryOrderView, order_id: str
    ) -> None:
        qty = int(view.filled_qty or 0)
        if qty <= 0:
            qty = int(view.order_qty or spread.qty or 0)
        if qty <= 0:
            log.error("filled entry %s has no qty — not promoting", spread.underlying)
            return
        limit = (
            spread.entry_limit_credit
            if spread.entry_limit_credit is not None
            else spread.credit
        )
        priced = (
            view.filled_avg_credit
            if view.filled_avg_credit is not None and view.filled_avg_credit > 0
            else None
        )
        credit = float(priced if priced is not None else limit)
        multiplier = int((self.cfg.get("spreads") or {}).get("multiplier", 100))
        max_loss = max_loss_dollars(spread.width, credit, multiplier) * qty
        wrote = self.journal.promote_pending_entry(
            spread.id,
            qty=qty,
            credit=credit,
            max_loss=max_loss,
            filled_qty=qty,
        )
        if not wrote:
            return
        kind = (
            "entry_partial"
            if view.state == "partial" or qty < int(spread.qty)
            else "entry_filled"
        )
        payload = {
            "spread_id": spread.id,
            "order_id": order_id,
            "filled_qty": qty,
            "ordered_qty": spread.qty,
            "limit_credit": limit,
            "filled_credit": priced,
            "credit": credit,
            "max_loss": max_loss,
            "unpriced": priced is None,
            "filled_at": view.filled_at,
        }
        self.journal.log_event(kind, spread.underlying, payload)
        if priced is None:
            self.journal.log_event("entry_fill_unpriced", spread.underlying, payload)
            log.warning(
                "entry %s filled qty=%s but the broker sent no credit — "
                "OPEN keeps the limit credit %.2f",
                spread.underlying,
                qty,
                credit,
            )
            return
        log.info(
            "entry filled %s qty=%s credit=%.2f (limit %.2f) — now OPEN",
            spread.underlying,
            qty,
            credit,
            float(limit),
        )

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
            if spread.status is SpreadStatus.PENDING_ENTRY:
                continue
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

            mark = self._spread_mark(spread.short_occ, spread.long_occ)
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
                self._record_quote_close(spread, "naked_leg", mark, now)
                reasons.append(f"{spread.underlying}:naked_leg")
            else:
                reasons.append(f"{spread.underlying}:naked_leg:close_failed")
        return reasons

    def _flag_overnight_opens(self, open_spreads: list[OpenSpread], now: datetime) -> None:
        """Heartbeat + once-per-ET-date journal flag. No AH mleg submits.

        A working entry is not a position held overnight. Day orders expire;
        they are not flagged and they are not closed here.
        """
        open_spreads = [
            s for s in open_spreads if s.status is not SpreadStatus.PENDING_ENTRY
        ]
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
            elif exits_cfg.get("credit_stop", True) and stop_hit(
                spread.credit, mark, stop_mult
            ):
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
            if spread.status is SpreadStatus.PENDING_ENTRY:
                continue
            mark = self._spread_mark(spread.short_occ, spread.long_occ)
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

            if not self.dry_run and live.exit_order_id:
                resolution = self._settle_live_close(live, reason, working_ids)
                if resolution != "submit":
                    reasons.append(self._exit_token(live, reason, resolution))
                    continue

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

            fresh = self.journal.get_spread(live.id) or live
            remaining = qty - int(fresh.close_filled_qty or 0)
            if remaining <= 0:
                if (
                    fresh.close_filled_qty > 0
                    and fresh.close_fill_notional is not None
                ):
                    self.journal.record_close(
                        fresh.id,
                        reason,
                        close_debit=fresh.close_fill_notional / fresh.close_filled_qty,
                        closed_at=now.isoformat(),
                        source=SOURCE_FILL,
                    )
                    reasons.append(f"{fresh.underlying}:{reason}")
                else:
                    reasons.append(f"{fresh.underlying}:{reason}:invalid_qty")
                continue

            payload = close_credit_spread_payload(
                short_occ=fresh.short_occ,
                long_occ=fresh.long_occ,
                qty=remaining,
                debit=mark if mark is not None else fresh.credit * 0.5,
            )
            accepted = self._attempt_mleg_close(fresh, payload, reason, mark)
            if not accepted:
                reasons.append(f"{fresh.underlying}:{reason}:close_failed")
                continue
            if self.dry_run:
                # Observer: the close debit is the mid of the two legs (the
                # same mark that tripped TP / stop). No broker fill exists.
                self._record_quote_close(fresh, reason, mark, now)
                reasons.append(f"{fresh.underlying}:{reason}")
                continue
            filled = self.journal.get_spread(fresh.id) or fresh
            resolution = self._settle_live_close(
                filled, reason, self._working_close_ids()
            )
            reasons.append(self._exit_token(filled, reason, resolution))
        return reasons

    def _record_quote_close(
        self,
        spread: OpenSpread,
        reason: str,
        mark: Optional[float],
        now: datetime,
    ) -> None:
        """Dry-run and naked-leg closes: journal the quote, or a missing marker."""
        self.journal.record_close(
            spread.id,
            reason,
            close_debit=as_debit(mark),
            closed_at=now.isoformat(),
            source=SOURCE_QUOTE,
        )

    def _fetch_close_order(self, order_id: str) -> Optional[CloseOrderView]:
        getter = getattr(self.broker, "get_close_order", None)
        if getter is None:
            return None
        try:
            view = getter(order_id)
        except Exception as exc:
            log.error(
                "close order lookup failed %s (%s) — will not replace a working close",
                order_id,
                type(exc).__name__,
            )
            return None
        if view is None:
            return None
        return view

    def _settle_live_close(
        self,
        spread: OpenSpread,
        reason: str,
        working_ids: Optional[set[str]],
    ) -> str:
        """Apply a live mleg fill. Returns closed|working|unknown|partial|dead|submit."""
        order_id = spread.exit_order_id
        if not order_id:
            return "submit"
        view = self._fetch_close_order(order_id)
        if view is None:
            if working_ids is None or order_id in working_ids:
                return "unknown" if working_ids is None else "working"
            return "unknown"
        result = self.journal.apply_close_fill(
            spread.id,
            reason,
            view,
            target_qty=int(spread.qty),
        )
        if result in {"closed", "unpriced"}:
            return "closed"
        if result == "working":
            return "working"
        if result == "partial":
            return "partial"
        if result == "dead":
            self.journal.log_event(
                "close_failed",
                spread.underlying,
                {
                    "spread_id": spread.id,
                    "reason": reason,
                    "error": "close order ended with no fill",
                    "order_id": order_id,
                },
            )
            return "dead"
        return "unknown"

    def _exit_token(self, spread: OpenSpread, reason: str, resolution: str) -> str:
        base = f"{spread.underlying}:{reason}"
        if resolution == "closed":
            return base
        if resolution == "working":
            log.info(
                "working mleg close %s still live for %s qty=%s — "
                "not cancelling, not replacing",
                spread.exit_order_id,
                spread.underlying,
                spread.qty,
            )
            return f"{base}:exit_working"
        if resolution == "partial":
            return f"{base}:partial_fill"
        if resolution == "dead":
            return f"{base}:close_failed"
        self.journal.log_event(
            "close_status_unknown",
            spread.underlying,
            {
                "spread_id": spread.id,
                "exit_order_id": spread.exit_order_id,
                "reason": reason,
            },
        )
        log.error(
            "working close %s for %s unconfirmed — not cancelling, not replacing",
            spread.exit_order_id,
            spread.underlying,
        )
        return f"{base}:working_unconfirmed"

    def run_forever(self) -> None:
        loop_cfg = self.cfg.get("loop") or {}
        while True:
            try:
                result = self.tick()
            except Exception as exc:
                # A timed-out read must not kill the child. The supervisor
                # would otherwise see a dead process, and a hang with no
                # timeout was the stale-heartbeat restart. Strategy errors
                # still propagate.
                if not is_transport_failure(exc):
                    raise
                log.error(
                    "poll tick failed (%s) — heartbeat continues",
                    type(exc).__name__,
                )
                status = self._beat_status or "rth_scan"
                self.beat(status, f"tick_error:{type(exc).__name__}")
                result = TickResult(status=status, proposals=[], exits=[], arms=[])
            sleep = (
                float(loop_cfg.get("sleep_seconds_rth", 30))
                if result.status == "rth_scan"
                else float(loop_cfg.get("sleep_seconds_off_hours", 60))
            )
            time.sleep(sleep)


_ENTRY_EXPIRED_ORDER_STATUSES = frozenset({"expired", "done_for_day"})


def _terminal_entry_status(raw_status: str) -> SpreadStatus:
    if str(raw_status or "") in _ENTRY_EXPIRED_ORDER_STATUSES:
        return SpreadStatus.ENTRY_EXPIRED
    return SpreadStatus.CANCELLED


def _terminal_entry_reason(status: SpreadStatus) -> str:
    if status is SpreadStatus.ENTRY_EXPIRED:
        return "entry_expired"
    return "entry_cancelled"


def write_startup_beat(writer: HeartbeatWriter, *, dry_run: bool) -> None:
    """Heartbeat used until the engine exists and takes over the hook."""
    snap = current_inflight()
    writer.write(
        Heartbeat(
            ts=datetime.now(timezone.utc).isoformat(),
            pid=os.getpid(),
            status="starting",
            loop=0,
            dry_run=dry_run,
            detail="startup",
            inflight_op=snap.op,
            inflight_symbol=snap.symbol,
            inflight_since=snap.since,
        )
    )


def _held_spreads(spreads: list[OpenSpread]) -> list[OpenSpread]:
    return [s for s in spreads if s.status in HELD_SPREAD_STATUSES]


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
    # Beat before the first REST call so a slow account/data fetch cannot
    # look like a missing child. The HTTP wrapper refreshes this while the
    # call is in flight (blocked_in=get_account / get_stock_bars / ...).
    write_startup_beat(hb, dry_run=dry_run)
    set_inflight_hook(lambda: write_startup_beat(hb, dry_run=dry_run))
    data = AlpacaMarketData(creds, cfg)
    if dry_run:
        broker = DryRunBroker(equity=equity)
        # Prefer live account equity when keys exist, still zero orders.
        try:
            live = AlpacaBroker(creds, cfg)
            broker = DryRunBroker(equity=live.account_equity() or equity, account_number=live.account_number())
        except Exception as exc:
            log.warning("live account fetch skipped in observer: %s", type(exc).__name__)
        return Engine(cfg, journal, broker, data, dry_run=True, heartbeat=hb)

    broker = AlpacaBroker(creds, cfg)
    return Engine(cfg, journal, broker, data, dry_run=False, heartbeat=hb)
