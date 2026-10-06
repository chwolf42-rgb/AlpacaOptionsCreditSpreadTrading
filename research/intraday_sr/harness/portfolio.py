"""Portfolio / risk simulator: `simulate(signals, bars, risk, costs)` (SPEC sections 3, 4, 5, 7).

Execution runs on 5m bars for both entry timeframes (a 15m signal becomes live at its 15m close and is then
worked on the 5m tape), so the forced exit is exactly the open of the 15:55 bar.

Per session, in bar-open order across symbols:
  1. activate signals with available_at <= bar open (guard clock = previous close)
  2. exits (symbol A-Z): pending flatten (daily stop), forced 15:55 exit, gap/stop/target (fills.exit_on_bar)
  3. entries: live stop-entries, ties by higher zone score then symbol A-Z; caps: 1 per symbol, max
     concurrent (capacity freed this bar is usable next bar), max entries/day, no fills on bars opening at/after
     15:00, daily stop not hit; sizing 0.5% of day-start equity / stop distance, capped at 1x equity per position
     and 3x gross (cap-limited trades counted); zone target < 1R from the fill -> skipped
  4. at the bar close (guard clock advances): cancel a pending order on a close beyond formation
     invalidation when that level is set (F8, and Test B via B1), otherwise on a close through the
     zone (Test A). Then mark open P&L, and if
     realized + open <= -1.5% of day-start equity flatten at the next open and take no more entries that day.
Loss guardrail (SPEC v1.3 G3, primary d2+w5, from RiskCfg): a loss is a closed trade with R < 0 after costs,
counted at its exit fill, portfolio-wide, updated after every exit (also inside one bar, and for an entry stopped
out in its fill bar). Reaching the day/week limit blocks new entries for the rest of the session/week and cancels
armed triggers; open positions run to their normal exit; their losses keep counting.
Equity compounds daily.

Price units: Signal.trigger / stop / targets and the bar frame OHLC are ADJUSTED prices; as-traded = adjusted x
Bar.adj_factor (raw/adj) is used only for costs and the $0.01 tick (tick = 0.01 / adj_factor in adjusted units).
Signal.expires_at is the first bar OPEN at which the trigger is dead: a bar opening at or after expires_at can
never fill it. Target labels: "1R", "2R", "zone" (canonical; "next_zone" alias); anything else raises.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Iterable, Mapping, Optional

import numpy as np
import pandas as pd

from research.intraday_sr.harness import costs as K
from research.intraday_sr.harness.config import CostCfg, RiskCfg, canonical_target
from research.intraday_sr.types import Fill, Trade
from research.intraday_sr.harness.fills import STOP, TARGET, exit_on_bar, stop_entry_fill, widen_stop
from research.intraday_sr.harness.guard import Guard, Guarded, LookaheadError

BAR_5M = timedelta(minutes=5)


# Bad prints (SPEC v1.3.1 C1, CP0 lookahead fix): `bad_print` and the clamp for bar t depend on bars t+1 and t+2, so
# they are known only at the close of t+2. The simulator therefore NEVER reads `bad_print` (nor any clamped column)
# when simulating bar t: fills, stops, targets, marks and cancels all use the UNCLAMPED open/high/low/close.
# If a frame carries the unclamped extremes in separate columns, those win over `high`/`low`.
UNCLAMPED_COLS = {"high": ("high_unclamped", "high_raw"), "low": ("low_unclamped", "low_raw")}


class FillOutsideBar(AssertionError):
    """Defensive check: a fill price must lie inside its bar's UNCLAMPED [low, high]."""


class _Bar:
    __slots__ = ("open", "high", "low", "close", "adj_factor")

    def __init__(self, o, h, l, c, a):
        self.open, self.high, self.low, self.close, self.adj_factor = float(o), float(h), float(l), float(c), float(a)


# Pre-A1b behaviours, OFF by default; switched on only by harness.run --legacy-target-gap / --legacy-halfday for the
# post-run attribution check (such a run never writes the PROGRAM ledger). Set before pass-2 workers fork.
LEGACY = {"target_gap": False, "halfday": False}


def set_legacy(target_gap: bool = False, halfday: bool = False) -> dict:
    LEGACY.update(target_gap=bool(target_gap), halfday=bool(halfday))
    return dict(LEGACY)


def _check_fill(px: float, b: _Bar, what: str, sym, ts) -> None:
    if not (b.low - 1e-9 <= px <= b.high + 1e-9):
        raise FillOutsideBar(f"{what} fill {px} outside bar [{b.low}, {b.high}]: {sym} {ts}")


def _extreme(f: pd.DataFrame, col: str) -> np.ndarray:
    for alt in UNCLAMPED_COLS[col]:
        if alt in f:
            return f[alt].to_numpy(float)
    return f[col].to_numpy(float)


class FrameBarSource:
    """5m bars per symbol: DataFrame with tz-aware `ts` (bar OPEN), open/high/low/close, `session` (date) and
    `adj_factor` (raw/adj, constant within a session). `available_at` = ts + 5 min if absent."""

    def __init__(self, frames: Mapping[str, pd.DataFrame], adj_root=None):
        """adj_root: directory with Trading's adj_factors (see harness/adjfactors.py). Frames that already carry
        `adj_factor` keep it; otherwise it is joined from adj_root by session, else 1.0 (APPROXIMATE, in adj_info)."""
        from research.intraday_sr.harness import adjfactors as AF
        self._by = {}
        self.adj_info = AF.AdjInfo()
        for sym, df in frames.items():
            df = df.sort_values("ts", kind="stable")
            if "adj_factor" not in df:
                df = AF.attach(df, sym, adj_root, self.adj_info) if adj_root is not None else df.assign(adj_factor=1.0)
                if adj_root is None:
                    self.adj_info.source[sym] = "approx_1.0"
                    self.adj_info.approx_sessions[sym] = int(df["session"].nunique())
            else:
                self.adj_info.source[sym] = "frame"
                self.adj_info.approx_sessions[sym] = 0
            self._by[sym] = {d: g.reset_index(drop=True) for d, g in df.groupby("session", sort=True)}

    def session_frame(self, symbol: str, session: date) -> Optional[pd.DataFrame]:
        return self._by.get(symbol, {}).get(session)

    def daily(self, symbol: str) -> pd.DataFrame:
        rows = [(d, float(g["close"].iloc[-1]), float(g["volume"].sum()) if "volume" in g else np.nan)
                for d, g in self._by.get(symbol, {}).items()]
        return pd.DataFrame(rows, columns=["session", "close", "volume"]).set_index("session")


@dataclass
class _Pos:
    sig: Guarded
    raw_sig: object
    direction: int
    qty: float
    entry: Fill
    stop: float
    target: float
    risk_usd: float
    capped: bool
    tier: str
    adj: float
    entry_ts: datetime
    ambiguous: bool = False
    day_losses_before: int = 0
    week_losses_before: int = 0


@dataclass
class SimResult:
    trades: list
    meta: list                                   # one dict per trade (exit kind, flags)
    daily: pd.Series                             # daily return, indexed by session date
    counters: dict = field(default_factory=dict)
    sessions: Optional[pd.DataFrame] = None      # one row per session (G3.11): trips, daily stop, blocked signals


class SignalContractError(ValueError):
    """A signal violates the harness input contract (e.g. Zone.atr_d NaN). Raised per signal, never swallowed."""


def _formation_clock(sig):
    """(formation or None, available_at or None). Compact spills carry the timestamp without the object."""
    form = getattr(sig, "formation", None)
    if form is not None:
        return form, form.available_at
    return None, getattr(sig, "formation_available_at", None)


def _consume_formation(guard: Guard, sig) -> None:
    """F/B: consume the formation at the signal's decision bar.

    F8's retest prints after the break bar, so formation.available_at must be strictly before
    signal.available_at. Equality is lookahead. Compact pass 2 has no formation object; it
    checks the spilled f_avail_us the same way. Test A has neither and is unchanged.
    """
    form, avail = _formation_clock(sig)
    if form is None and avail is None:
        return
    decision = sig.available_at
    if avail is None or not (avail < decision):
        raise LookaheadError(
            f"formation.available_at={None if avail is None else avail.isoformat()} is not strictly "
            f"before signal.available_at={decision.isoformat()} "
            "(F8: the retest bar is after the break bar's close)"
        )
    if form is not None:
        guard.check(form, decision_ts=decision)


def _cancel_level(sig) -> float | None:
    """F8 invalidation when the signal carries one. NaN / absent keeps Test A's zone cancel."""
    level = getattr(sig, "cancel_level", None)
    if level is None:
        form = getattr(sig, "formation", None)
        level = None if form is None else getattr(form, "invalidation", None)
    if level is None:
        return None
    try:
        value = float(level)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(value):
        return None
    return value


def _sig_name(sig) -> str:
    z = getattr(sig, "zone", None)
    return (f"{getattr(sig, 'variant_id', '?')} {getattr(sig, 'symbol', '?')} available_at="
            f"{getattr(sig, 'available_at', '?')} zone_id={getattr(z, 'zone_id', '?')}")


def _atr_d(sig) -> float:
    v = getattr(sig.zone, "atr_d", None)           # CP0 R1: Zone.atr_d is the ONLY source (required, finite, > 0)
    if v is not None and np.isfinite(v) and v > 0:
        return float(v)
    raise SignalContractError(f"Zone.atr_d must be finite and > 0 (got {v!r}) for the 0.10 ATR_d stop floor: "
                              f"{_sig_name(sig)}")


def _zone_target(sig) -> float:
    tg = sig.targets or {}
    for k in ("zone", "next_zone"):
        if k in tg and tg[k] is not None and np.isfinite(float(tg[k])):
            return float(tg[k])
    raise SignalContractError(f"zone-target variant but signal has no finite targets['zone']: {_sig_name(sig)}")


def _default_tier_fn(bars, cfg: CostCfg) -> Callable[[str, date], str]:
    cache: dict[str, pd.Series] = {}

    def f(sym: str, d: date) -> str:
        if sym in cfg.t0_symbols:
            return "T0"
        if sym not in cache:
            daily = bars.daily(sym) if hasattr(bars, "daily") else pd.DataFrame()
            cache[sym] = K.prior_median_dollar_volume(daily, cfg.dv_lookback) if len(daily) else pd.Series(dtype=float)
        v = cache[sym].get(d, np.nan)
        return K.tier(sym, float(v) if v is not None else np.nan, cfg)
    return f


def _week_key(d: date) -> tuple:
    """Mon-Fri ET calendar week (ISO year, ISO week); sessions are weekdays so ISO weeks match G3.5."""
    return d.isocalendar()[:2]


def simulate(signals: Iterable, bars, risk: RiskCfg = RiskCfg(), costs: CostCfg = CostCfg(), *,
             sessions: Optional[Iterable[date]] = None, fold: int = -1,
             tier_fn: Optional[Callable[[str, date], str]] = None, guard: Optional[Guard] = None,
             window_starts: Optional[Iterable[date]] = None) -> SimResult:
    """Simulate one portfolio. The loss guardrail comes from `risk` (default = PRIMARY d2+w5, SPEC v1.3 G1).

    window_starts: sessions where a simulated window begins (G3.10); the week loss counter is reset there in
    addition to the normal Monday reset. Default: every fold train/test start, the 2019-02-01 trading start and the
    holdout start, so one continuous development path gives every fold window counters that start at 0.
    """
    from research.intraday_sr.harness.walkforward import WINDOW_STARTS
    ws = set(WINDOW_STARTS if window_starts is None else window_starts)
    guard = guard or Guard()
    tier_fn = tier_fn or _default_tier_fn(bars, costs)
    by_day: dict[date, list] = defaultdict(list)
    for s in signals:
        by_day[s.available_at.astimezone(pd.Timestamp(s.available_at).tz).date()].append(s)
    days = sorted(set(sessions) if sessions is not None else set(by_day))
    equity = risk.start_equity
    week_key, week_losses = None, 0
    trades, meta, rets, srows = [], [], [], []
    c = defaultdict(int)
    ws_sorted = sorted(ws)
    prev = None
    for d in days:
        wk = _week_key(d)
        starts_window = any((prev is None or w > prev) and w <= d for w in ws_sorted) if ws_sorted else False
        prev = d
        if wk != week_key or starts_window:
            week_key, week_losses = wk, 0
        day_start = equity
        st = _run_day(d, by_day.get(d, []), bars, risk, costs, tier_fn, guard, fold, day_start, week_losses,
                      trades, meta, c)
        week_losses = st["week_losses_end"]
        srows.append(st)
        equity = day_start + st["pnl"]
        rets.append(st["pnl"] / day_start)
    daily = pd.Series(rets, index=pd.Index(days, name="session"), dtype=float)
    c["trades"] = len(trades)
    sess = pd.DataFrame(srows, columns=SESSION_COLS)
    c["days_halted_day_limit"] = int(sess["day_limit_trip_ts"].notna().sum()) if len(sess) else 0
    c["weeks_halted_week_limit"] = int(sess["week_limit_trip_ts"].notna().sum()) if len(sess) else 0
    c["daily_stop_days"] = int(sess["daily_stop_ts"].notna().sum()) if len(sess) else 0
    c["signals_cancelled_at_trip"] = int(sess["signals_cancelled_at_trip"].sum()) if len(sess) else 0
    c["signals_arrived_blocked"] = int(sess["signals_arrived_blocked"].sum()) if len(sess) else 0
    return SimResult(trades, meta, daily, dict(c), sess)


SESSION_COLS = ["session", "pnl", "entries", "day_losses", "week_losses_start", "week_losses_end",
                "day_limit_trip_ts", "week_limit_trip_ts", "daily_stop_ts", "signals_cancelled_at_trip", "signals_arrived_blocked",
                "week_blocked_at_open"]


def _run_day(d, sigs, bars, risk, costs, tier_fn, guard, fold, day_start, week_losses, trades, meta, c):
    lim_d, lim_w = risk.max_losses_day, risk.max_losses_week
    target = canonical_target(risk.target)
    st = {"session": d, "pnl": 0.0, "entries": 0, "day_losses": 0, "week_losses_start": week_losses,
          "week_losses_end": week_losses, "day_limit_trip_ts": None, "week_limit_trip_ts": None,
          "daily_stop_ts": None, "signals_cancelled_at_trip": 0, "signals_arrived_blocked": 0,
          "week_blocked_at_open": bool(lim_w is not None and week_losses >= lim_w)}
    if not sigs:
        return st
    syms = sorted({s.symbol for s in sigs})
    frames = {s: bars.session_frame(s, d) for s in syms}
    frames = {s: f for s, f in frames.items() if f is not None and len(f)}
    if not frames:
        c["signals_no_bars"] += len(sigs)
        return st
    early = max(f["ts"].max().time() for f in frames.values()) <= risk.forced_exit_early
    forced_t = risk.forced_exit_early if early else risk.forced_exit
    tss = {s: f["ts"].tolist() for s, f in frames.items()}
    rows = {s: {ts: i for i, ts in enumerate(v)} for s, v in tss.items()}
    arr = {s: [_Bar(*r) for r in zip(f["open"].to_numpy(float), _extreme(f, "high"), _extreme(f, "low"),
                                      f["close"].to_numpy(float), f["adj_factor"].to_numpy(float))]
           for s, f in frames.items()}
    last_good: dict = {}       # symbol -> (bar ts, close) of the latest bar seen (marks, end-of-day fallback)
    timeline = sorted({ts for v in tss.values() for ts in v})
    inactive = sorted(sigs, key=lambda s: (s.available_at, s.symbol))
    c["signals"] += len(sigs)
    pending: list[Guarded] = []
    pos: dict[str, _Pos] = {}
    realized = 0.0
    entries = 0
    halted = False          # daily -1.5% stop
    flatten = False
    ai = 0
    L = {"day": 0, "week": week_losses}

    def blocked() -> bool:
        return (lim_d is not None and L["day"] >= lim_d) or (lim_w is not None and L["week"] >= lim_w)

    def cancel_armed(ts):
        nonlocal pending
        if pending:
            st["signals_cancelled_at_trip"] += len(pending)      # armed triggers cancelled by the trip
            pending = []

    def close_pos(sym, p: _Pos, px, kind, ts, cost_kind, ambiguous=False, bar=None):
        nonlocal realized
        if bar is not None:
            _check_fill(float(px), bar, kind, sym, ts)
        side = -p.direction
        cost = K.fill_cost(cost_kind, side, p.qty, px, p.adj, p.tier, costs)
        ex = Fill(sym, ts, float(px), float(p.qty), side, kind, float(cost))
        pnl = p.direction * p.qty * (px - p.entry.price) - p.entry.cost - cost
        r = float(pnl / p.risk_usd)
        realized += pnl
        trades.append(Trade(p.raw_sig, p.entry, ex, r, float(pnl), fold, str(getattr(p.raw_sig, "variant_id", ""))))
        tripped = None
        if r < 0:                                       # G3.1-2: loss = R < 0 after costs, counted at the exit fill
            was = blocked()
            L["day"] += 1
            L["week"] += 1
            if lim_d is not None and L["day"] == lim_d:
                st["day_limit_trip_ts"] = ts
                tripped = "day"
            if lim_w is not None and L["week"] == lim_w:
                st["week_limit_trip_ts"] = ts
                tripped = "week" if tripped is None else "day+week"
            if blocked() and not was:
                cancel_armed(ts)                        # G3.6: armed stop-entry triggers are cancelled
        meta.append({"symbol": sym, "session": d, "exit_kind": kind, "capped": p.capped, "tier": p.tier,
                     "ambiguous_same_bar": bool(p.ambiguous or ambiguous), "risk_usd": p.risk_usd,
                     "direction": p.direction, "entry_ts": p.entry_ts, "exit_ts": ts,
                     "day_losses_before": p.day_losses_before, "week_losses_before": p.week_losses_before,
                     "is_loss": r < 0, "tripped": tripped})
        c[f"exit_{kind}"] += 1

    for ts in timeline:
        guard.advance(ts)                                  # decision time = previous bar's close = this open
        while ai < len(inactive) and inactive[ai].available_at <= ts:
            raw = inactive[ai]
            _consume_formation(guard, raw)             # formation.available_at must be < the decision bar
            g = guard.wrap(raw)
            ai += 1
            if blocked():                                  # arrives while a limit is active: never armed
                st["signals_arrived_blocked"] += 1                   # became available while blocked
            else:
                pending.append(g)
        here = {}
        for s_ in frames:
            j = rows[s_].get(ts)
            if j is None:
                continue
            here[s_] = arr[s_][j]                           # unclamped; bad_print is not visible at bar t
        occupied = len(pos)
        # ---- (i) exits of positions open at the bar start, symbol A-Z (all fills in a bar share its ts)
        for sym in sorted(pos):
            if sym not in here:
                continue
            p = pos[sym]
            b = here[sym]
            if flatten:
                close_pos(sym, p, float(b.open), "daily_stop", ts, K.FORCED, bar=b)
                del pos[sym]
                continue
            if ts.time() >= forced_t:
                close_pos(sym, p, float(b.open), "forced_eod", ts, K.FORCED, bar=b)
                del pos[sym]
                continue
            tick = 0.01 / p.adj
            hit = exit_on_bar(p.direction, p.stop, p.target, float(b.open), float(b.high), float(b.low), tick, False,
                              legacy_target_gap=LEGACY["target_gap"])
            if hit is not None:
                close_pos(sym, p, hit.price, hit.kind, ts, K.STOP if hit.kind == STOP else K.TARGET, hit.ambiguous,
                          bar=b)
                del pos[sym]
        # ---- (ii) entries, priority: higher zone score, then symbol A-Z
        live = []
        for g in pending:
            if g.symbol not in here:
                live.append(g)
                continue
            if ts >= g.expires_at:
                c["expired"] += 1
                continue
            live.append(g)
        pending = live
        # A1b half-day rule: no entry fill at or after the session's forced-exit bar (12:55 on half days); a normal
        # day's cutoff stays last_entry (15:00 < 15:55). LEGACY["halfday"] restores the pre-A1b last_entry-only rule.
        entry_cut = risk.last_entry if LEGACY["halfday"] else min(risk.last_entry, forced_t)
        if not halted and not blocked() and ts.time() < entry_cut:
            cands = sorted((g for g in pending if g.symbol in here and g.symbol not in pos),
                           key=lambda g: (-float(g.zone.score), g.symbol))
            for g in cands:
                if blocked():                              # a trip earlier in this bar (step i or iii)
                    break
                if g not in pending:
                    continue
                b = here[g.symbol]
                dirn = int(g.direction)
                px = stop_entry_fill(dirn, float(g.trigger), float(b.open), float(b.high), float(b.low))
                if px is None:
                    continue
                _check_fill(float(px), b, "entry", g.symbol, ts)
                pending.remove(g)
                if g.symbol in pos:
                    c["skip_symbol_busy"] += 1
                    continue
                if occupied >= risk.max_concurrent:
                    c["skip_concurrency"] += 1
                    continue
                if entries >= risk.max_entries_per_day:
                    c["skip_daily_entry_cap"] += 1
                    continue
                adj = float(b.adj_factor)
                stop = widen_stop(dirn, px, float(g.stop), _atr_d(g), risk.min_stop_atr_d)
                rps = abs(px - stop)
                if dirn > 0 and float(g.stop) > stop or dirn < 0 and float(g.stop) < stop:
                    c["stop_widened"] += 1
                if target == "1R":
                    tgt = px + dirn * rps
                elif target == "2R":
                    tgt = px + dirn * 2 * rps
                elif target == "zone":
                    tgt = _zone_target(g)
                    if dirn * (tgt - px) < risk.zone_target_min_r * rps:
                        c["skip_zone_target_lt_1R"] += 1
                        continue
                qty = math.floor(risk.risk_pct * day_start / rps)
                gross = sum(q.qty * q.entry.price for q in pos.values())
                cap_q = math.floor(min(risk.max_pos_notional_x * day_start,
                                       max(risk.max_gross_notional_x * day_start - gross, 0.0)) / px)
                capped = qty > cap_q
                qty = min(qty, cap_q)
                if qty < 1:
                    c["skip_cap_zero_qty"] += 1
                    continue
                tier = tier_fn(g.symbol, d)
                cost = K.fill_cost(K.ENTRY, dirn, qty, px, adj, tier, costs)
                ent = Fill(g.symbol, ts, float(px), float(qty), dirn, "entry", float(cost))
                p = _Pos(g, g.unwrap, dirn, float(qty), ent, stop, tgt, float(qty * rps), capped, tier, adj, ts,
                         day_losses_before=L["day"], week_losses_before=L["week"])
                pos[g.symbol] = p
                entries += 1
                occupied += 1
                c["cap_limited"] += int(capped)
                hit = exit_on_bar(dirn, stop, tgt, float(b.open), float(b.high), float(b.low), 0.01 / adj, True,
                                  legacy_target_gap=LEGACY["target_gap"])
                if hit is not None:                        # (iii) fill-bar stop counts before the next entry
                    close_pos(g.symbol, p, hit.price, hit.kind, ts, K.STOP, hit.ambiguous, bar=b)
                    del pos[g.symbol]
        # ---- bar close
        close_ts = ts + BAR_5M
        guard.advance(close_ts)
        keep = []
        for g in pending:
            if g.symbol in here and g.available_at <= close_ts:
                cl = float(here[g.symbol].close)
                level = _cancel_level(g)
                if level is not None:
                    if (int(g.direction) > 0 and cl < level) or (int(g.direction) < 0 and cl > level):
                        c["cancel_invalidation"] += 1
                        continue
                else:
                    z = g.zone
                    if (int(g.direction) > 0 and cl < float(z.low)) or (int(g.direction) < 0 and cl > float(z.high)):
                        c["cancel_zone_close"] += 1
                        continue
            keep.append(g)
        pending = keep
        for s_, b_ in here.items():
            last_good[s_] = (ts, b_.close)
        open_pnl = 0.0
        for sym, p in pos.items():
            last = last_good.get(sym, (None, float(p.entry.price)))[1]
            open_pnl += p.direction * p.qty * (last - p.entry.price) - p.entry.cost
        if not halted and realized + open_pnl <= risk.daily_loss_stop * day_start:     # signed (-0.015)
            halted = True
            flatten = True
            st["daily_stop_ts"] = close_ts
            c["halt_daily_loss_stop"] += 1
            c["halt_days"] += 1
    # positions still open after the last bar (missing 15:55 bar): exit at the last close, forced
    for sym in sorted(pos):
        lts, lpx = last_good.get(sym, (tss[sym][-1], float(pos[sym].entry.price)))
        close_pos(sym, pos[sym], lpx, "forced_eod_lastclose", lts + BAR_5M, K.FORCED)
        del pos[sym]
    c["pending_unfilled_eod"] += len(pending) + (len(inactive) - ai)
    st.update(pnl=realized, entries=entries, day_losses=L["day"], week_losses_end=L["week"])
    return st
