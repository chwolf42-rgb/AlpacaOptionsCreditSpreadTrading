"""Portfolio / risk simulator: `simulate(signals, bars, risk, costs)` (SPEC sections 3, 4, 5, 7).

Execution runs on 5m bars for both entry timeframes (a 15m signal becomes live at its 15m close and is then
worked on the 5m tape), so the forced exit is exactly the open of the 15:55 bar.

Per session, in bar-open order across symbols:
  1. activate signals with available_at <= bar open (guard clock = previous close)
  2. exits: pending flatten (daily stop / guardrail), forced 15:55 exit, gap/stop/target (fills.exit_on_bar)
  3. entries: live stop-entries, ties by higher zone score then symbol A-Z; caps: 1 per symbol, max
     concurrent (capacity freed this bar is usable next bar), max entries/day, no fills on bars opening at/after
     15:00, daily stop not hit; sizing 0.5% of day-start equity / stop distance, capped at 1x equity per position
     and 3x gross (cap-limited trades counted); zone target < 1R from the fill -> skipped
  4. at the bar close (guard clock advances): cancel orders whose zone closed through, mark open P&L, and if
     realized + open <= -1.5% of day-start equity (or a guardrail overlay trips) flatten at the next open and
     take no more entries that day. Guardrail overlays (v1.1 A3) only block new entries after N losing trades.
Equity compounds daily.
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
from research.intraday_sr.harness.config import CostCfg, GuardrailCfg, RiskCfg
from research.intraday_sr.harness.contracts import Fill, Trade
from research.intraday_sr.harness.fills import STOP, TARGET, exit_on_bar, stop_entry_fill, widen_stop
from research.intraday_sr.harness.guard import Guard, Guarded

BAR_5M = timedelta(minutes=5)


class _Bar:
    __slots__ = ("open", "high", "low", "close", "adj_factor")

    def __init__(self, o, h, l, c, a):
        self.open, self.high, self.low, self.close, self.adj_factor = float(o), float(h), float(l), float(c), float(a)


class FrameBarSource:
    """5m bars per symbol: DataFrame with tz-aware `ts` (bar OPEN), open/high/low/close, `session` (date) and
    `adj_factor` (raw/adj, constant within a session). `available_at` = ts + 5 min if absent."""

    def __init__(self, frames: Mapping[str, pd.DataFrame]):
        self._by = {}
        for sym, df in frames.items():
            df = df.sort_values("ts", kind="stable")
            if "adj_factor" not in df:
                df = df.assign(adj_factor=1.0)
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


@dataclass
class SimResult:
    trades: list
    meta: list                                   # one dict per trade (exit kind, flags)
    daily: pd.Series                             # daily return, indexed by session date
    counters: dict = field(default_factory=dict)


def _atr_d(sig) -> float:
    z = sig.zone
    for src in (getattr(z, "atr_d", None), (sig.components or {}).get("atr_d"), (z.components or {}).get("atr_d")):
        if src is not None and np.isfinite(src) and src > 0:
            return float(src)
    raise ValueError("signal needs ATR_d (zone.atr_d or components['atr_d']) for the 0.10 ATR_d stop floor")


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


def simulate(signals: Iterable, bars, risk: RiskCfg = RiskCfg(), costs: CostCfg = CostCfg(), *,
             sessions: Optional[Iterable[date]] = None, fold: int = -1, guardrail: GuardrailCfg = GuardrailCfg(),
             tier_fn: Optional[Callable[[str, date], str]] = None, guard: Optional[Guard] = None) -> SimResult:
    guard = guard or Guard()
    tier_fn = tier_fn or _default_tier_fn(bars, costs)
    by_day: dict[date, list] = defaultdict(list)
    for s in signals:
        by_day[s.available_at.astimezone(pd.Timestamp(s.available_at).tz).date()].append(s)
    days = sorted(set(sessions) if sessions is not None else set(by_day))
    equity = risk.start_equity
    week_key, week_losers = None, 0
    trades, meta, rets = [], [], []
    c = defaultdict(int)
    for d in days:
        wk = d.isocalendar()[:2]
        if wk != week_key:
            week_key, week_losers, week_trig = wk, 0, False
        day_start = equity
        day_pnl, n, losers = _run_day(d, by_day.get(d, []), bars, risk, costs, guardrail, tier_fn, guard, fold,
                                      day_start, week_losers, trades, meta, c)
        week_losers += losers
        if guardrail.week_losers is not None and week_losers >= guardrail.week_losers and not week_trig:
            week_trig = True
            c["guardrail_week_triggers"] += 1
        equity = day_start + day_pnl
        rets.append(day_pnl / day_start)
    daily = pd.Series(rets, index=pd.Index(days, name="session"), dtype=float)
    c["trades"] = len(trades)
    return SimResult(trades, meta, daily, dict(c))


def _run_day(d, sigs, bars, risk, costs, gr, tier_fn, guard, fold, day_start, week_losers, trades, meta, c):
    if not sigs:
        return 0.0, 0, 0
    syms = sorted({s.symbol for s in sigs})
    frames = {s: bars.session_frame(s, d) for s in syms}
    frames = {s: f for s, f in frames.items() if f is not None and len(f)}
    if not frames:
        c["signals_no_bars"] += len(sigs)
        return 0.0, 0, 0
    early = max(f["ts"].max().time() for f in frames.values()) <= risk.forced_exit_early
    forced_t = risk.forced_exit_early if early else risk.forced_exit
    tss = {s: f["ts"].tolist() for s, f in frames.items()}
    rows = {s: {ts: i for i, ts in enumerate(v)} for s, v in tss.items()}
    arr = {s: [_Bar(*r) for r in zip(f["open"].to_numpy(float), f["high"].to_numpy(float), f["low"].to_numpy(float),
                                      f["close"].to_numpy(float), f["adj_factor"].to_numpy(float))]
           for s, f in frames.items()}
    timeline = sorted({ts for v in tss.values() for ts in v})
    inactive = sorted(sigs, key=lambda s: (s.available_at, s.symbol))
    c["signals"] += len(sigs)
    pending: list[Guarded] = []
    pos: dict[str, _Pos] = {}
    realized = 0.0
    entries = 0
    halted = False
    flatten = False
    losers_known = 0    # ... as known at the last bar close (guardrail input)
    gr_trip_logged = False
    ai = 0

    nonlocal_losers = [0]

    def close_pos(sym, p: _Pos, px, kind, ts, cost_kind, ambiguous=False):
        nonlocal realized
        side = -p.direction
        cost = K.fill_cost(cost_kind, side, p.qty, px, p.adj, p.tier, costs)
        ex = Fill(sym, ts, float(px), float(p.qty), side, kind, float(cost))
        pnl = p.direction * p.qty * (px - p.entry.price) - p.entry.cost - cost
        realized += pnl
        if pnl < 0:
            nonlocal_losers[0] += 1
        trades.append(Trade(p.raw_sig, p.entry, ex, float(pnl / p.risk_usd), float(pnl), fold,
                            str(getattr(p.raw_sig, "variant_id", ""))))
        meta.append({"symbol": sym, "session": d, "exit_kind": kind, "capped": p.capped, "tier": p.tier,
                     "ambiguous_same_bar": bool(p.ambiguous or ambiguous), "risk_usd": p.risk_usd,
                     "direction": p.direction, "entry_ts": p.entry_ts, "exit_ts": ts})
        c[f"exit_{kind}"] += 1

    for ts in timeline:
        guard.advance(ts)                                  # decision time = previous bar's close = this open
        while ai < len(inactive) and inactive[ai].available_at <= ts:
            pending.append(guard.wrap(inactive[ai]))
            ai += 1
        here = {s: arr[s][rows[s][ts]] for s in frames if ts in rows[s]}
        occupied = len(pos)
        # ---- exits
        for sym in list(pos):
            if sym not in here:
                continue
            p = pos[sym]
            b = here[sym]
            if flatten:
                close_pos(sym, p, float(b.open), "daily_stop", ts, K.FORCED)
                del pos[sym]
                continue
            if ts.time() >= forced_t:
                close_pos(sym, p, float(b.open), "forced_eod", ts, K.FORCED)
                del pos[sym]
                continue
            tick = 0.01 / p.adj
            hit = exit_on_bar(p.direction, p.stop, p.target, float(b.open), float(b.high), float(b.low), tick, False)
            if hit is not None:
                close_pos(sym, p, hit.price, hit.kind, ts, K.STOP if hit.kind == STOP else K.TARGET, hit.ambiguous)
                del pos[sym]
        # ---- entries
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
        gr_block = gr.blocks(losers_known, week_losers + losers_known)
        if gr_block and not halted and ts.time() < risk.last_entry:
            for g in [g for g in pending if g.symbol in here and g.symbol not in pos]:
                if stop_entry_fill(int(g.direction), float(g.trigger), float(here[g.symbol].open),
                                   float(here[g.symbol].high), float(here[g.symbol].low)) is not None:
                    pending.remove(g)
                    c[f"skip_guardrail_{gr_block}"] += 1
        if not halted and not gr_block and ts.time() < risk.last_entry:
            cands = sorted((g for g in pending if g.symbol in here and g.symbol not in pos),
                           key=lambda g: (-float(g.zone.score), g.symbol))
            for g in cands:
                b = here[g.symbol]
                dirn = int(g.direction)
                px = stop_entry_fill(dirn, float(g.trigger), float(b.open), float(b.high), float(b.low))
                if px is None:
                    continue
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
                if risk.target == "1R":
                    tgt = px + dirn * rps
                elif risk.target == "2R":
                    tgt = px + dirn * 2 * rps
                else:
                    tgt = float(g.targets["zone"])
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
                p = _Pos(g, g.unwrap, dirn, float(qty), ent, stop, tgt, float(qty * rps), capped, tier, adj, ts)
                pos[g.symbol] = p
                entries += 1
                occupied += 1
                c["cap_limited"] += int(capped)
                hit = exit_on_bar(dirn, stop, tgt, float(b.open), float(b.high), float(b.low), 0.01 / adj, True)
                if hit is not None:
                    close_pos(g.symbol, p, hit.price, hit.kind, ts, K.STOP, hit.ambiguous)
                    del pos[g.symbol]
        # ---- bar close
        close_ts = ts + BAR_5M
        guard.advance(close_ts)
        losers_known = nonlocal_losers[0]
        if not gr_trip_logged and gr.day_losers is not None and losers_known >= gr.day_losers:
            gr_trip_logged = True
            c["guardrail_day_triggers"] += 1
        keep = []
        for g in pending:
            if g.symbol in here and g.available_at <= close_ts:
                z = g.zone
                cl = float(here[g.symbol].close)
                if (int(g.direction) > 0 and cl < float(z.low)) or (int(g.direction) < 0 and cl > float(z.high)):
                    c["cancel_zone_close"] += 1
                    continue
            keep.append(g)
        pending = keep
        open_pnl = 0.0
        for sym, p in pos.items():
            j = rows[sym].get(ts)
            last = arr[sym][j].close if j is not None else float(p.entry.price)
            open_pnl += p.direction * p.qty * (last - p.entry.price) - p.entry.cost
        dd = realized + open_pnl
        if not halted:
            trip = None
            if dd <= -risk.daily_loss_stop * day_start:
                trip = "daily_loss_stop"
            if trip:
                halted = True
                flatten = True
                c[f"halt_{trip}"] += 1
                c["halt_days"] += 1
    # positions still open after the last bar (missing 15:55 bar): exit at the last close, forced
    for sym in list(pos):
        close_pos(sym, pos[sym], arr[sym][-1].close, "forced_eod_lastclose", tss[sym][-1] + BAR_5M, K.FORCED)
        del pos[sym]
    c["pending_unfilled_eod"] += len(pending) + (len(inactive) - ai)
    return realized, entries, nonlocal_losers[0]
