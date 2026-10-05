"""Options overlays (SPEC section 7 + v1.1 ruling 2). EVERY NUMBER HERE IS MODEL-BASED, NO OPRA DATA.

1) Baseline overlay (9 variants = structure {long ATM, long 1 OTM, debit vertical to the equity target} x
   DTE bucket {0-1, 2-4, 5-7}); 0.5% of equity premium at risk; exits with the equity trade.
2) 0DTE scenario (9 variants = stop {-30,-40,-50%} x take-profit {+50,+65,+80%} of the option price);
   $2,000 premium (2% of $100k day-start equity); same-day expiry only; Black-Scholes reprice on each 5m bar
   from the underlying H/L/C; stop wins if both are reachable in a bar; time exit at the 15:45 bar open.
   Higher-risk, low-confidence: VIX9D-based IV understates near-expiry skew/gamma.

Expiry calendar (verified against Cboe/SEC filings, see EXPIRY_SOURCES):
  SPY  Fri weeklies; Wed since 2016; Mon since 2018-02-16; Tue from 2022-11-14 and Thu from 2022-11-16 (daily).
  QQQ  Fri weeklies; Mon/Wed present by SR-CBOE-2021-057 (2021-10-05; earlier start not verified -> used from
       2021-10-05, conservative); Tue from 2022-11-14, Thu from 2022-11-16 (daily).
  IWM  Fri weeklies; Mon/Wed from 2021-10-05 (SR-CBOE-2021-057); Tue from 2024-04-16, Thu from 2024-04-18.
  Single names: Friday weeklies only (Mon/Wed for qualifying stocks were only proposed in Jan 2026; ignored).
  Holidays: a Monday expiry moves to the next business day; Tue-Fri expiries move to the prior business day.

SPEC v1.3.2 O1 (dual-expiry books) is the scaffold in options_overlay.py. Listing dates
and the Friday-weekly holiday shift are applied from this module; the NYSE session
calendar and the daily/weekly book choice live in options_calendar.py; Black-Scholes
quotes, spreads and the stop-wins exit live in options_pricing.py. The DTE-bucket
helpers below stay until Developer 2 bumps grids.py — the hashed trial count is still
450, and GRID_SHA256 does not include the 24 O1 rows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from research.intraday_sr.harness import s0grids as _G

# Same binding as config.RiskCfg / PRIMARY: grids.PRIMARY_GUARDRAIL via s0grids, not a local 2/5.
_PG_DAY, _PG_WEEK = _G.primary_guardrail()

ETFS = ("SPY", "QQQ", "IWM")
EXPIRY_SOURCES = {
    "spy_mon_2018": "https://s202.q4cdn.com/174824971/files/doc_news/2018/02/cboe-global-markets-to-list-spy-monday-expiring-weekly-options-2-15-18-pdf1691636060926.pdf",
    "spy_wed_2016": "https://www.federalregister.gov/documents/2018/02/23/2018-03699 (Cboe EDGX: 'listing Wednesday expirations since 2016')",
    "spy_qqq_tue_thu_2022": "https://cdn.cboe.com/resources/product_update/2022/Update-Cboe-Options-to-List-SPY-and-QQQ-Tuesday-and-Thursday-Expiring-Weekly-Options.pdf ; https://www.sec.gov/files/rules/sro/cboe/2022/34-96315.pdf",
    "iwm_mon_wed_2021": "https://www.sec.gov/files/rules/sro/cboe/2021/34-93255.pdf (SR-CBOE-2021-057)",
    "iwm_tue_thu_2024": "https://cdn.cboe.com/resources/product_update/2024/Cboe-Options-to-List-Tuesday-and-Thursday-Expiring-Weekly-Options-on-IWM.pdf",
    "single_name_mon_wed_2026_proposal": "https://www.sec.gov/files/rules/sro/cboe/2026/34-104643.pdf",
}
# weekday -> first date that weekday's expiry existed (Mon=0 .. Fri=4)
_FIRST = {
    "SPY": {4: date(2005, 1, 1), 2: date(2016, 1, 1), 0: date(2018, 2, 16), 1: date(2022, 11, 14), 3: date(2022, 11, 16)},
    "QQQ": {4: date(2005, 1, 1), 0: date(2021, 10, 5), 2: date(2021, 10, 5), 1: date(2022, 11, 14), 3: date(2022, 11, 16)},
    "IWM": {4: date(2005, 1, 1), 0: date(2021, 10, 5), 2: date(2021, 10, 5), 1: date(2024, 4, 16), 3: date(2024, 4, 18)},
}
_SINGLE = {4: date(2005, 1, 1)}


@dataclass(frozen=True)
class OptionsCfg:
    rate: float = 0.04
    min_t_minutes: float = 15.0
    skew_put_etf: float = 0.15
    skew_put_single: float = 0.08
    skew_call_etf: float = 0.03
    skew_call_single: float = 0.0
    skew_floor: float = 0.85
    skew_mult: float = 1.0                  # sensitivity: 2.0
    exit_iv_mult: float = 1.0               # sensitivity: 0.90
    single_iv_clamp: tuple = (0.15, 1.50)
    etf_c_atm: float = 0.02
    etf_c_otm: float = 0.03
    etf_lo: float = 0.01
    etf_hi: float = 0.10
    etf_0dte_late_mult: float = 1.5         # 0DTE after 14:00
    single_c: float = 0.06
    single_lo: float = 0.05
    single_hi: float = 0.25
    fee_per_contract: float = 0.05          # sensitivity: 0.65
    premium_pct: float = 0.005              # baseline overlay
    label: str = "MODEL-BASED, NO OPRA DATA"


# Grid cells come from grids.OPTIONS / grids.OPTIONS_0DTE (single source, SPEC v1.3.1 C3); not redefined here.
from research.intraday_sr.harness import s0grids as _S0  # noqa: E402

BASELINE_GRID = [(r["structure"], r["dte_bucket"]) for r in _S0.grids().OPTIONS]                       # 9
STRUCTURES = tuple(dict.fromkeys(s for s, _ in BASELINE_GRID))
DTE_BUCKETS = {b: tuple(int(x) for x in b.split("-")) for b in dict.fromkeys(b for _, b in BASELINE_GRID)}


@dataclass(frozen=True)
class ZeroDteCfg:
    premium_usd_pct: float = 0.02           # $2,000 on $100k day-start equity
    stops: tuple = tuple(dict.fromkeys(r["stop_pct"] / 100 for r in _S0.grids().OPTIONS_0DTE))          # -0.30..
    tps: tuple = tuple(dict.fromkeys(r["take_profit_pct"] / 100 for r in _S0.grids().OPTIONS_0DTE))    # 0.50..
    time_exit: time = time(15, 45)
    time_exit_early: time = time(12, 45)
    label: str = "HIGHER-RISK SCENARIO, MODEL-BASED, NO OPRA DATA, LOW CONFIDENCE (VIX9D IV understates 0DTE skew/gamma)"


ZERO_DTE_GRID = [(r["stop_pct"] / 100, r["take_profit_pct"] / 100) for r in _S0.grids().OPTIONS_0DTE]   # 9


# --------------------------------------------------------------------- Black-Scholes (mirrors replay.credit.bs_price)
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, sigma: float, right: str, rate: float = 0.04) -> float:
    if spot <= 0 or strike <= 0:
        return 0.0
    intrinsic = max(spot - strike, 0.0) if right == "call" else max(strike - spot, 0.0)
    if t_years <= 1e-8 or sigma <= 1e-8:
        return intrinsic
    sq = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years) / sq
    d2 = d1 - sq
    dk = strike * math.exp(-rate * t_years)
    if right == "call":
        return spot * _ncdf(d1) - dk * _ncdf(d2)
    return dk * _ncdf(-d2) - spot * _ncdf(-d1)


# --------------------------------------------------------------------- calendar
class TradingCalendar:
    def __init__(self, trading_days: Iterable[date]):
        self.days = sorted(set(trading_days))
        self._set = set(self.days)
        self._pos = {d: i for i, d in enumerate(self.days)}

    def is_open(self, d: date) -> bool:
        return d in self._set

    def next_open(self, d: date) -> date:
        x = d + timedelta(days=1)
        while x not in self._set and x <= self.days[-1]:
            x += timedelta(days=1)
        return x

    def prev_open(self, d: date) -> date:
        x = d - timedelta(days=1)
        while x not in self._set and x >= self.days[0]:
            x -= timedelta(days=1)
        return x

    def sessions_between(self, a: date, b: date) -> int:
        """Trading sessions strictly after a and strictly before b."""
        if a not in self._pos or b not in self._pos:
            return sum(1 for d in self.days if a < d < b)
        return max(self._pos[b] - self._pos[a] - 1, 0)


def listed_weekdays(symbol: str, d: date) -> set[int]:
    table = _FIRST.get(symbol, _SINGLE)
    return {wd for wd, first in table.items() if d >= first}


def expiry_on(symbol: str, d: date, cal: TradingCalendar) -> bool:
    """True if `symbol` had an expiry settling on trading day d (holiday shifts applied)."""
    if not cal.is_open(d):
        return False
    wds = listed_weekdays(symbol, d)
    if d.weekday() in wds:
        return True
    # shifted expiries: a Monday holiday moves to Tuesday; a Tue-Fri holiday moves to the prior business day
    for k in range(1, 4):
        nom = d - timedelta(days=k)
        if nom.weekday() == 0 and 0 in wds and not cal.is_open(nom) and cal.next_open(nom) == d:
            return True
        nom = d + timedelta(days=k)
        if nom.weekday() in wds - {0} and nom.weekday() < 5 and not cal.is_open(nom) and cal.prev_open(nom) == d:
            return True
    return False


def expiries_in(symbol: str, d: date, lo: int, hi: int, cal: TradingCalendar) -> list[date]:
    return [d + timedelta(days=k) for k in range(lo, hi + 1) if expiry_on(symbol, d + timedelta(days=k), cal)]


# --------------------------------------------------------------------- IV, time, strikes, spreads
def sigma_atm(symbol: str, vix9d_prev: float, vix_prev: float, t_cal_days: float, rv20_sym: float,
              rv20_spy: float, cfg: OptionsCfg = OptionsCfg()) -> float:
    """Variance-time interpolation VIX9D (9d) -> VIX (30d); VIX9D flat below 9 days. QQQ/IWM and single names
    scale by RV20(sym)/RV20(SPY); single names clamped to [0.15, 1.50]. Inputs are the PRIOR session's closes."""
    v9, v30 = vix9d_prev / 100.0, vix_prev / 100.0
    if not np.isfinite(v9):
        v9 = v30
    if t_cal_days <= 9 or not np.isfinite(v30):
        s = v9
    elif t_cal_days >= 30:
        s = v30
    else:
        w = (t_cal_days - 9) / 21.0
        s = math.sqrt((v9 * v9 * 9 * (1 - w) + v30 * v30 * 30 * w) / t_cal_days)
    if symbol == "SPY":
        return s
    ratio = rv20_sym / rv20_spy if (np.isfinite(rv20_sym) and np.isfinite(rv20_spy) and rv20_spy > 0) else np.nan
    if not np.isfinite(ratio):
        return float("nan")
    s = s * ratio
    if symbol not in ETFS:
        s = min(max(s, cfg.single_iv_clamp[0]), cfg.single_iv_clamp[1])
    return s


def sigma_k(symbol: str, s_atm: float, spot: float, strike: float, t_years: float, cfg: OptionsCfg = OptionsCfg()) -> float:
    if t_years <= 0:
        return s_atm
    z = math.log(strike / spot) / (s_atm * math.sqrt(t_years))
    etf = symbol in ETFS
    a = (cfg.skew_put_etf if etf else cfg.skew_put_single) if z < 0 else (cfg.skew_call_etf if etf else cfg.skew_call_single)
    return max(s_atm * (1 - cfg.skew_mult * a * z), cfg.skew_floor * s_atm)


def t_years(now: datetime, expiry: date, cal: TradingCalendar, early_closes: set = frozenset(),
            cfg: OptionsCfg = OptionsCfg()) -> float:
    """Trading minutes to the expiry close / (252 x 390), floor 15 minutes."""
    def close_min(d):
        return 13 * 60 if d in early_closes else 16 * 60
    d0 = now.date()
    now_min = now.hour * 60 + now.minute + now.second / 60
    if expiry == d0:
        mins = close_min(d0) - now_min
    else:
        mins = max(close_min(d0) - now_min, 0) + 390 * cal.sessions_between(d0, expiry) + (close_min(expiry) - 570)
    return max(mins, cfg.min_t_minutes) / (252 * 390)


def strike_increment(symbol: str, spot_raw: float) -> float:
    if symbol in ETFS or spot_raw < 200:
        return 1.0
    if spot_raw < 500:
        return 2.5
    return 5.0


def atm_strike(symbol: str, spot_raw: float) -> float:
    inc = strike_increment(symbol, spot_raw)
    return round(spot_raw / inc) * inc


def half_spread(symbol: str, mid: float, otm: bool, zero_dte_late: bool, cfg: OptionsCfg = OptionsCfg()) -> float:
    if symbol in ETFS:
        c = cfg.etf_c_otm if otm else cfg.etf_c_atm
        h = min(max(c * mid, cfg.etf_lo), cfg.etf_hi)
        return h * (cfg.etf_0dte_late_mult if zero_dte_late else 1.0)
    return min(max(cfg.single_c * mid, cfg.single_lo), cfg.single_hi)


@dataclass(frozen=True)
class Leg:
    strike: float
    right: str          # call | put
    qty: int            # +1 long, -1 short (per spread unit)
    otm: bool


def legs_for(structure: str, symbol: str, direction: int, spot_raw: float, target_raw: float) -> list[Leg]:
    right = "call" if direction > 0 else "put"
    inc = strike_increment(symbol, spot_raw)
    atm = atm_strike(symbol, spot_raw)
    if structure == "long_atm":
        return [Leg(atm, right, 1, False)]
    if structure == "long_otm_1":
        return [Leg(atm + direction * inc, right, 1, True)]
    k2 = round(target_raw / inc) * inc
    k2 = max(k2, atm + inc) if direction > 0 else min(k2, atm - inc)
    return [Leg(atm, right, 1, False), Leg(k2, right, -1, True)]


def price_legs(legs: Sequence[Leg], symbol: str, spot_raw: float, ty: float, s_atm: float, buy: bool,
               zero_dte_late: bool, cfg: OptionsCfg = OptionsCfg(), iv_mult: float = 1.0) -> float:
    """Executable package price per share.
    Opening (buy=True):  debit  = sum(long legs at ask) - sum(short legs at bid).
    Closing (buy=False): credit = sum(long legs at bid) - sum(short legs at ask), floored at 0."""
    tot = 0.0
    for lg in legs:
        sig = sigma_k(symbol, s_atm * iv_mult, spot_raw, lg.strike, ty, cfg)
        mid = bs_price(spot_raw, lg.strike, ty, sig, lg.right, cfg.rate)
        h = half_spread(symbol, mid, lg.otm, zero_dte_late, cfg)
        ask, bid = mid + h, max(mid - h, 0.0)
        if buy:
            tot += ask if lg.qty > 0 else -bid
        else:
            tot += bid if lg.qty > 0 else -ask
    return tot if buy else max(tot, 0.0)


# --------------------------------------------------------------------- overlay runners
@dataclass
class IVContext:
    """Per-day inputs, all from the PRIOR session (decision on day d uses the close of d-1)."""
    calendar: TradingCalendar
    vix9d_prev: dict            # date -> VIX9D close of the prior session (index points)
    vix_prev: dict              # date -> VIX close of the prior session
    rv20: dict                  # (symbol, date) -> RV20 from completed daily bars through d-1
    adj_factor: dict = field(default_factory=dict)   # (symbol, date) -> raw/adj
    early_closes: frozenset = frozenset()

    def s_atm(self, symbol: str, d: date, t_cal_days: float, cfg: OptionsCfg) -> float:
        return sigma_atm(symbol, self.vix9d_prev.get(d, np.nan), self.vix_prev.get(d, np.nan), t_cal_days,
                         self.rv20.get((symbol, d), np.nan), self.rv20.get(("SPY", d), np.nan), cfg)

    def f(self, symbol: str, d: date) -> float:
        return float(self.adj_factor.get((symbol, d), 1.0))


HALF_BAR = timedelta(minutes=2, seconds=30)


def _fill_time(fill, kind: str) -> datetime:
    """Entry/exit clock for option repricing: bar open for opens/gaps/forced exits, mid-bar for intrabar fills."""
    return fill.ts if kind in ("forced_eod", "daily_stop", "forced_eod_lastclose") else fill.ts + HALF_BAR


def baseline_records(trades, ctx: IVContext, structure: str, bucket: str, cfg: OptionsCfg = OptionsCfg()):
    lo, hi = DTE_BUCKETS[bucket]
    recs, skips = [], {"no_expiry_in_bucket": 0, "no_iv": 0, "unpriceable": 0}
    for tr in trades:
        sym, d = tr.entry.symbol, tr.entry.ts.date()
        exps = expiries_in(sym, d, lo, hi, ctx.calendar)
        if not exps:
            skips["no_expiry_in_bucket"] += 1
            continue
        exp = exps[0]
        f = ctx.f(sym, d)
        s_in, s_out = tr.entry.price * f, tr.exit.price * f
        direction = int(tr.entry.side)
        tgt = getattr(tr.signal, "targets", {}) or {}
        target_raw = (tr.entry.price + direction * abs(tr.entry.price - tr.signal.stop)) * f
        zk = "zone" if "zone" in tgt else ("next_zone" if "next_zone" in tgt else None)   # "next_zone" = alias
        if zk and np.isfinite(tgt.get(zk, np.nan)):
            target_raw = float(tgt[zk]) * f
        t_in_clock = tr.entry.ts + HALF_BAR
        t_out_clock = _fill_time(tr.exit, tr.exit.reason)
        sa = ctx.s_atm(sym, d, (exp - d).days, cfg)
        if not np.isfinite(sa):
            skips["no_iv"] += 1
            continue
        legs = legs_for(structure, sym, direction, s_in, target_raw)
        ty0 = t_years(t_in_clock, exp, ctx.calendar, ctx.early_closes, cfg)
        ty1 = t_years(t_out_clock, exp, ctx.calendar, ctx.early_closes, cfg)
        z0 = exp == d
        late_in, late_out = z0 and t_in_clock.time() >= time(14, 0), z0 and t_out_clock.time() >= time(14, 0)
        debit = price_legs(legs, sym, s_in, ty0, sa, True, late_in, cfg)
        if not debit > 0:
            skips["unpriceable"] += 1
            continue
        credit = price_legs(legs, sym, s_out, ty1, sa, False, late_out, cfg, iv_mult=cfg.exit_iv_mult)
        nleg = len(legs)
        recs.append({"symbol": sym, "session": d, "entry_ts": t_in_clock, "exit_ts": t_out_clock, "expiry": exp,
                     "debit_pc": debit * 100 + cfg.fee_per_contract * nleg,
                     "credit_pc": credit * 100 - cfg.fee_per_contract * nleg, "reason": tr.exit.reason,
                     "direction": direction})
    return recs, skips


def zero_dte_records(trades, bars, ctx: IVContext, zcfg: ZeroDteCfg = ZeroDteCfg(), cfg: OptionsCfg = OptionsCfg()):
    """Returns {(stop, tp): [records]}, skips. Same-day expiry only; long ATM call (long) / put (short)."""
    out = {cell: [] for cell in ZERO_DTE_GRID}
    skips = {"no_same_day_expiry": 0, "no_iv": 0, "unpriceable": 0}
    for tr in trades:
        sym, d = tr.entry.symbol, tr.entry.ts.date()
        if not expiry_on(sym, d, ctx.calendar):
            skips["no_same_day_expiry"] += 1
            continue
        f = ctx.f(sym, d)
        direction = int(tr.entry.side)
        right = "call" if direction > 0 else "put"
        s_in = tr.entry.price * f
        K = atm_strike(sym, s_in)
        leg = [Leg(K, right, 1, False)]
        sa = ctx.s_atm(sym, d, 0.5, cfg)
        if not np.isfinite(sa):
            skips["no_iv"] += 1
            continue
        t0 = tr.entry.ts + HALF_BAR
        ty = lambda when: t_years(when, d, ctx.calendar, ctx.early_closes, cfg)
        ask = price_legs(leg, sym, s_in, ty(t0), sa, True, t0.time() >= time(14, 0), cfg)
        if not ask > 0.01:
            skips["unpriceable"] += 1
            continue
        frame = bars.session_frame(sym, d)
        after = frame[frame["ts"] > tr.entry.ts]
        texit = zcfg.time_exit_early if d in ctx.early_closes else zcfg.time_exit
        path = []                      # (ts, bid_open, bid_adverse, bid_favorable, is_time_exit)
        for b in after.itertuples(index=False):
            late = b.ts.time() >= time(14, 0)
            bo = price_legs(leg, sym, b.open * f, ty(b.ts), sa, False, late, cfg)
            if b.ts.time() >= texit:
                path.append((b.ts, bo, bo, bo, True))
                break
            tc = b.ts + timedelta(minutes=5)
            adv, fav = (b.low, b.high) if direction > 0 else (b.high, b.low)
            ba = price_legs(leg, sym, adv * f, ty(tc), sa, False, late, cfg)
            bf = price_legs(leg, sym, fav * f, ty(tc), sa, False, late, cfg)
            path.append((b.ts, bo, ba, bf, False))
        if not path:
            last = frame.iloc[-1]
            tl = last["ts"] + timedelta(minutes=5)
            bl = price_legs(leg, sym, float(last["close"]) * f, ty(tl), sa, False, True, cfg)
            path = [(tl, bl, bl, bl, True)]
        for cell in ZERO_DTE_GRID:
            sl, tp = ask * (1 + cell[0]), ask * (1 + cell[1])
            ex = None
            for ts, bo, ba, bf, is_t in path:
                if is_t:
                    ex = (ts, bo, "time_exit")
                    break
                if bo <= sl:
                    ex = (ts, bo, "stop")
                    break
                if bo >= tp:
                    ex = (ts, bo, "take_profit")
                    break
                if ba <= sl:
                    ex = (ts + HALF_BAR, sl, "stop")          # stop wins if both are reachable
                    break
                if bf >= tp:
                    ex = (ts + HALF_BAR, tp, "take_profit")
                    break
            if ex is None:
                ts, bo = path[-1][0], path[-1][1]
                ex = (ts, bo, "time_exit")
            out[cell].append({"symbol": sym, "session": d, "entry_ts": t0, "exit_ts": ex[0],
                              "debit_pc": ask * 100 + cfg.fee_per_contract, "credit_pc": ex[1] * 100 - cfg.fee_per_contract,
                              "reason": ex[2], "direction": direction})
    return out, skips


def options_account(records: list, sessions: Sequence[date], premium_pct: float, *, max_concurrent: int = 4,
                    max_entries_per_day: int = 12, daily_loss_stop: float = -0.015,
                    max_losses_day: Optional[int] = _PG_DAY, max_losses_week: Optional[int] = _PG_WEEK,
                    window_starts: Optional[Iterable[date]] = None, start_equity: float = 100_000.0):
    """Premium-sized options portfolio fed by overlay records (one per equity signal that produced an option trade).
    SPEC v1.3 G6: the primary guardrail (grids.PRIMARY_GUARDRAIL, via s0grids.primary_guardrail) applies to this
    portfolio's OWN closed option trades: a loss is an option trade with R < 0 after spread and fees
    (R = P&L / premium paid). Counts update at each option exit, in exit order (exit time, then symbol), before any
    entry at or after that time; reaching a limit blocks new entries for the rest of the session / Mon-Fri week.
    Signals skipped for lack of an expiry never reach here (not trades). Caps: 1 per symbol, max concurrent,
    entries/day, realized daily loss stop (option marks between records are not modelled). Returns (daily Series,
    trades DataFrame, counters, sessions DataFrame). daily_loss_stop is signed (-0.015)."""
    from research.intraday_sr.harness.walkforward import WINDOW_STARTS
    ws = sorted(set(WINDOW_STARTS if window_starts is None else window_starts))
    by = {}
    for r in sorted(records, key=lambda r: (r["entry_ts"], r["symbol"])):
        by.setdefault(r["session"], []).append(r)
    eq = start_equity
    rets, taken, srows = [], [], []
    c = {"skip_concurrency": 0, "skip_symbol_busy": 0, "skip_daily_stop": 0, "skip_daily_entry_cap": 0,
         "signals_cancelled_at_trip": 0, "signals_arrived_blocked": 0,
         "skip_too_expensive": 0}
    wk, wk_losses, prev = None, 0, None
    for d in sessions:
        starts_window = any((prev is None or w > prev) and w <= d for w in ws)
        prev = d
        if d.isocalendar()[:2] != wk or starts_window:
            wk, wk_losses = d.isocalendar()[:2], 0
        st = {"session": d, "week_losses_start": wk_losses, "day_limit_trip_ts": None, "week_limit_trip_ts": None,
              "daily_stop_ts": None, "signals_cancelled_at_trip": 0,
              "signals_arrived_blocked": 0}   # options: records are already-filled trades, nothing is armed -> 0
        day_start, day_pnl, open_, entries, day_losses = eq, 0.0, [], 0, 0

        def close_until(t):
            nonlocal day_pnl, day_losses, wk_losses, open_
            done = sorted((o for o in open_ if t is None or o["exit_ts"] <= t), key=lambda o: (o["exit_ts"], o["symbol"]))
            for o in done:
                day_pnl += o["pnl"]
                if o["R"] < 0:
                    day_losses += 1
                    wk_losses += 1
                    if max_losses_day is not None and day_losses == max_losses_day:
                        st["day_limit_trip_ts"] = o["exit_ts"]
                    if max_losses_week is not None and wk_losses == max_losses_week:
                        st["week_limit_trip_ts"] = o["exit_ts"]
                if st["daily_stop_ts"] is None and day_pnl <= daily_loss_stop * day_start:
                    st["daily_stop_ts"] = o["exit_ts"]
            open_ = [o for o in open_ if o not in done]

        for r in by.get(d, []):
            close_until(r["entry_ts"])
            if st["daily_stop_ts"] is not None:
                c["skip_daily_stop"] += 1
                continue
            if (max_losses_day is not None and day_losses >= max_losses_day) or \
                    (max_losses_week is not None and wk_losses >= max_losses_week):
                c["signals_arrived_blocked"] += 1
                st["signals_arrived_blocked"] += 1
                continue
            if any(o["symbol"] == r["symbol"] for o in open_):
                c["skip_symbol_busy"] += 1
                continue
            if len(open_) >= max_concurrent:
                c["skip_concurrency"] += 1
                continue
            if entries >= max_entries_per_day:
                c["skip_daily_entry_cap"] += 1
                continue
            n = int((premium_pct * day_start) // r["debit_pc"])
            if n < 1:
                c["skip_too_expensive"] += 1
                continue
            pnl = n * (r["credit_pc"] - r["debit_pc"])
            rec = dict(r, contracts=n, premium=n * r["debit_pc"], pnl=pnl, R=pnl / (n * r["debit_pc"]),
                       day_losses_before=day_losses, week_losses_before=wk_losses)
            open_.append(rec)
            taken.append(rec)
            entries += 1
        close_until(None)
        st.update(pnl=day_pnl, day_losses=day_losses, week_losses_end=wk_losses, entries=entries)
        srows.append(st)
        eq = day_start + day_pnl
        rets.append(day_pnl / day_start)
    sess = pd.DataFrame(srows)
    for k, col in (("days_halted_day_limit", "day_limit_trip_ts"), ("weeks_halted_week_limit", "week_limit_trip_ts"),
                   ("daily_stop_days", "daily_stop_ts")):
        c[k] = int(sess[col].notna().sum()) if len(sess) else 0
    return (pd.Series(rets, index=pd.Index(list(sessions), name="session"), dtype=float), pd.DataFrame(taken), c, sess)
