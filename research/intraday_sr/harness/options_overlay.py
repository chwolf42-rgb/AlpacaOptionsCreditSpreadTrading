"""Dual-expiry options overlay scaffold (SPEC v1.3.2 O1). MODEL-BASED. No orders, no data pulls.

Every scenario is run as two books, ``daily`` and ``weekly``, from the same equity
signals. Structure, stop, take-profit, time exit and sizing match across the two
books; only the listed expiry differs. Each ``(scenario, book)`` is its own portfolio
under the primary guardrail. Losses are the option trades' own R < 0 after spread and
fees. A missing expiry is a skip, not a loss, and a loss in one book does not count
in the other.

The 24 rows (6 baseline + 18 A2) and program N = 456 are read through
``s0grids.overlay_scaffold()``. They are not part of ``GRID_SHA256``. The hashed grid
stays at N = 450 until Developer 2's grids bump.

Baseline vs A2, where the task text and the spec differ: O1.8 keeps the baseline exit
on the equity trade (stop, target, or the 15:55 forced exit) and the 0.5% premium.
A2 is the 9 option-price stops × take-profits, the 15:45 ET time exit, and the 2%
premium ($2,000 on $100k). Both are flat before the cash close, so neither book holds
overnight. This scaffold follows that split.

Open parameters (each is read by the code; see the field docs on ``OverlayParams``):

- TODO_DAILY_DTE_UNIT
- TODO_WEEKLY_DTE_UNIT
- TODO_EARLY_CLOSE_TIME_EXIT
- TODO_PREMIUM_BUDGET_MODE
- TODO_RV20_DEFINITION (on ``options_pricing.atm_vol``)
- TODO_INTRABAR_OPEN
- TODO_LATE_SPREAD_CLAMP_ORDER
- TODO_LATE_SPREAD_SCOPE
- TODO_STOP_BASIS
- TODO_VERTICAL_SAME_STRIKE
- TODO_TIME_EXIT_BAR_RANGE
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Mapping, Optional, Sequence

import pandas as pd

from research.intraday_sr.harness import options as O
from research.intraday_sr.harness import options_calendar as cal
from research.intraday_sr.harness import options_pricing as px
from research.intraday_sr.harness import s0grids

OPEN_TODOS = (
    "TODO_DAILY_DTE_UNIT",
    "TODO_WEEKLY_DTE_UNIT",
    "TODO_EARLY_CLOSE_TIME_EXIT",
    "TODO_PREMIUM_BUDGET_MODE",
    "TODO_RV20_DEFINITION",
    "TODO_INTRABAR_OPEN",
    "TODO_LATE_SPREAD_CLAMP_ORDER",
    "TODO_LATE_SPREAD_SCOPE",
    "TODO_STOP_BASIS",
    "TODO_VERTICAL_SAME_STRIKE",
    "TODO_TIME_EXIT_BAR_RANGE",
)


@dataclass(frozen=True)
class OverlayParams:
    """Knobs for the bits §7 / O1 leave as a choice. Defaults are the spec's reading."""

    # TODO_DAILY_DTE_UNIT: O1.3 says "calendar DTE ≤ 1". Counted as calendar days,
    # (expiry − session).days, not trading sessions. Friday → Monday is 3 calendar days
    # and is not a daily-book expiry. Set this to 0 to require a same-day listing.
    daily_calendar_dte_max: int = 1
    # TODO_WEEKLY_DTE_UNIT: O1.4 says "calendar DTE in 2–10 trading days [P]". Counted as
    # trading sessions in (session, expiry], including the expiry session. Both the unit
    # and the 2–10 ends are provisional.
    weekly_trading_dte_min: int = 2
    weekly_trading_dte_max: int = 10
    # TODO_EARLY_CLOSE_TIME_EXIT: A2 names 15:45 ET and does not name the 13:00 session.
    # 12:45 is fifteen minutes before 13:00, the same offset as 15:45 before 16:00.
    time_exit_early: time = time(12, 45)
    # TODO_PREMIUM_BUDGET_MODE: A2 is "$2,000" and "2% of $100k". "pct_of_day_start"
    # uses the row's premium_pct, which is $2,000 at $100k and scales when equity compounds.
    premium_budget_mode: str = "pct_of_day_start"
    # TODO_INTRABAR_OPEN: a gap through the stop fills at the open bid (§4.2). False uses
    # only the adverse/favorable prints from the bar's high and low.
    gap_uses_open: bool = True
    # TODO_LATE_SPREAD_CLAMP_ORDER: see options_pricing.half_spread.
    late_spread_after_clamp: bool = True
    # TODO_LATE_SPREAD_SCOPE: see options_pricing.zero_dte_late.
    late_spread_on_expiry_session: bool = True
    # TODO_STOP_BASIS: see options_pricing.resolve_option_exit.
    stop_basis: str = "entry_ask"
    # TODO_VERTICAL_SAME_STRIKE: see options_pricing.structure_legs.
    vertical_min_width_increments: int = 1
    # TODO_TIME_EXIT_BAR_RANGE: see options_pricing.resolve_option_exit.
    time_exit_at_bar_open: bool = True


@dataclass(frozen=True)
class Scenario:
    """One (exit scenario, book) row. ``stop_pct`` / ``take_profit_pct`` are fractions (−0.30, +0.50)."""

    variant_id: str
    book: str
    kind: str                 # baseline | a2
    structure: str
    stop_pct: Optional[float]
    take_profit_pct: Optional[float]
    time_exit: time
    premium_pct: float
    exit_mode: str            # equity | option_price

    def time_exit_on(self, session: date, early_closes: Iterable[date], params: OverlayParams) -> time:
        if session in early_closes and self.exit_mode == "option_price":
            return params.time_exit_early
        return self.time_exit


@dataclass
class PortfolioResult:
    scenario: Scenario
    priced: list
    skips: dict
    trades: pd.DataFrame
    counters: dict
    sessions: pd.DataFrame


def load_scenarios(params: Optional[OverlayParams] = None) -> tuple[Scenario, ...]:
    """The 24 O1 rows. Percent points on the A2 rows match ``grids.OPTIONS_0DTE`` (−30, not −0.30)."""
    del params  # scenarios don't depend on OverlayParams; the signature stays stable for callers
    baseline, a2, _n = s0grids.overlay_scaffold()
    rows: list[Scenario] = []
    for row in baseline:
        rows.append(_scenario_from_row(row, kind="baseline"))
    for row in a2:
        rows.append(_scenario_from_row(row, kind="a2"))
    if len(rows) != 24:
        raise s0grids.GridsUnavailable(f"O1 scaffold produced {len(rows)} scenarios, expected 24")
    return tuple(rows)


def paired_scenarios(scenarios: Optional[Sequence[Scenario]] = None) -> tuple[tuple[Scenario, Scenario], ...]:
    """Each scenario beside its other book (O1.6). A daily-only or weekly-only row is a protocol breach."""
    scenarios = tuple(scenarios) if scenarios is not None else load_scenarios()
    groups: dict[tuple, dict[str, Scenario]] = {}
    for scenario in scenarios:
        key = (scenario.kind, scenario.structure, scenario.stop_pct, scenario.take_profit_pct, scenario.exit_mode,
               scenario.time_exit, scenario.premium_pct)
        groups.setdefault(key, {})[scenario.book] = scenario
    pairs = []
    for key, books in groups.items():
        if set(books) != {"daily", "weekly"}:
            raise s0grids.GridsUnavailable(f"O1 scenario {key} is not side-by-side: {sorted(books)}")
        pairs.append((books["daily"], books["weekly"]))
    pairs.sort(key=lambda pair: pair[0].variant_id)
    return tuple(pairs)


def _scenario_from_row(row: Mapping, *, kind: str) -> Scenario:
    book = str(row["book"])
    if book not in ("daily", "weekly"):
        raise s0grids.GridsUnavailable(f"O1 book must be daily or weekly, got {book!r}")
    exit_mode = str(row["exit_mode"])
    stop = take = None
    if exit_mode == "option_price":
        stop = float(row["stop_pct"]) / 100.0
        take = float(row["take_profit_pct"]) / 100.0
    return Scenario(
        variant_id=str(row["variant_id"]),
        book=book,
        kind=kind,
        structure=str(row["structure"]),
        stop_pct=stop,
        take_profit_pct=take,
        time_exit=time.fromisoformat(str(row["time_exit_et"])),
        premium_pct=float(row["premium_pct"]),
        exit_mode=exit_mode,
    )


def select_expiry(book: str, symbol: str, session: date, calendar: O.TradingCalendar,
                  params: OverlayParams) -> Optional[date]:
    """Listed expiry for one book, or None when that book skips the signal."""
    if book == "daily":
        return cal.daily_book_expiry(symbol, session, calendar, max_calendar_dte=params.daily_calendar_dte_max)
    if book == "weekly":
        return cal.weekly_book_expiry(
            symbol, session, calendar,
            dte_min=params.weekly_trading_dte_min, dte_max=params.weekly_trading_dte_max)
    raise ValueError(f"unknown book {book!r}")


def run_portfolios(priced: Mapping[str, Sequence[Mapping]], sessions: Sequence[date], *,
                   scenarios: Optional[Sequence[Scenario]] = None,
                   skips: Optional[Mapping[str, Mapping]] = None,
                   params: Optional[OverlayParams] = None) -> dict[str, PortfolioResult]:
    """One ``options_account`` per scenario. The guardrail is read here, from grids, on every call.

    ``priced`` maps ``variant_id`` to already-built option records (spread and fees in the cash
    columns). Passing the daily book's records into the weekly book's account is the caller's
    mistake; this function never pools them.
    """
    params = params or OverlayParams()
    if params.premium_budget_mode != "pct_of_day_start":
        raise NotImplementedError(
            "TODO_PREMIUM_BUDGET_MODE: only pct_of_day_start is implemented "
            f"(got {params.premium_budget_mode!r}). A2's 2% is $2,000 at $100k day-start equity.")
    scenarios = tuple(scenarios) if scenarios is not None else load_scenarios(params)
    day_limit, week_limit = s0grids.primary_guardrail()
    out: dict[str, PortfolioResult] = {}
    for scenario in scenarios:
        records = list(priced.get(scenario.variant_id, ()))
        _daily, trades, counters, sess = O.options_account(
            records, list(sessions), scenario.premium_pct,
            max_concurrent=int(s0grids.fixed("max_concurrent")),
            max_entries_per_day=int(s0grids.fixed("entries_per_day")),
            daily_loss_stop=float(s0grids.fixed("daily_loss_stop")),
            max_losses_day=day_limit,
            max_losses_week=week_limit,
        )
        scenario_skips = dict(skips.get(scenario.variant_id, {})) if skips else {}
        out[scenario.variant_id] = PortfolioResult(
            scenario=scenario, priced=records, skips=scenario_skips, trades=trades, counters=counters, sessions=sess)
    return out


def run_overlay(trades: Sequence, bars, ctx: O.IVContext, sessions: Optional[Sequence[date]] = None, *,
                params: Optional[OverlayParams] = None, cfg: Optional[O.OptionsCfg] = None,
                ) -> dict[str, PortfolioResult]:
    """Price every book of every scenario, then run each portfolio on its own guardrail counters.

    ``trades`` are equity fills (the same objects ``options.baseline_records`` accepts). ``bars``
    provides ``session_frame(symbol, session)`` with columns ts/open/high/low/close. No metric
    is written down; the return value is the per-book portfolios for tests and a later readout.
    """
    params = params or OverlayParams()
    cfg = cfg or O.OptionsCfg()
    scenarios = load_scenarios(params)
    if sessions is None:
        sessions = sorted({tr.entry.ts.date() for tr in trades})
    priced: dict[str, list] = {s.variant_id: [] for s in scenarios}
    skips: dict[str, dict] = {
        s.variant_id: {"no_expiry": 0, "no_iv": 0, "unpriceable": 0} for s in scenarios}
    by_book: dict[str, list[Scenario]] = {}
    for scenario in scenarios:
        by_book.setdefault(scenario.book, []).append(scenario)

    for trade in trades:
        symbol = trade.entry.symbol
        session = trade.entry.ts.date()
        for book, group in by_book.items():
            expiry = select_expiry(book, symbol, session, ctx.calendar, params)
            if expiry is None:
                for scenario in group:
                    skips[scenario.variant_id]["no_expiry"] += 1
                continue
            sigma = px.atm_vol(
                symbol, session, expiry,
                ctx.vix9d_prev.get(session, float("nan")), ctx.vix_prev.get(session, float("nan")),
                ctx.rv20.get((symbol, session), float("nan")), ctx.rv20.get(("SPY", session), float("nan")),
                cfg)
            if not math.isfinite(sigma):
                for scenario in group:
                    skips[scenario.variant_id]["no_iv"] += 1
                continue
            factor = ctx.f(symbol, session)
            _price_book(trade, bars, ctx, book, expiry, sigma, factor, group, priced, skips, params, cfg)
    return run_portfolios(priced, list(sessions), scenarios=scenarios, skips=skips, params=params)


def _price_book(trade, bars, ctx, book, expiry, sigma, factor, group, priced, skips, params, cfg) -> None:
    if book not in ("daily", "weekly"):
        raise ValueError(f"unknown book {book!r}")
    symbol = trade.entry.symbol
    session = trade.entry.ts.date()
    direction = int(trade.entry.side)
    spot = float(trade.entry.price) * factor
    baselines = [s for s in group if s.exit_mode == "equity"]
    a2 = [s for s in group if s.exit_mode == "option_price"]
    for scenario in baselines:
        record = _baseline_record(trade, ctx, scenario, expiry, sigma, factor, spot, direction, params, cfg)
        if record is None:
            skips[scenario.variant_id]["unpriceable"] += 1
        else:
            priced[scenario.variant_id].append(record)
    if not a2:
        return
    by_structure: dict[str, list[Scenario]] = {}
    for scenario in a2:
        by_structure.setdefault(scenario.structure, []).append(scenario)
    frame = _session_frame(bars, symbol, session)
    for structure, scenarios in by_structure.items():
        built = _a2_path(trade, frame, ctx, expiry, sigma, factor, spot, direction, structure, scenarios[0], params, cfg)
        if built is None:
            for scenario in scenarios:
                skips[scenario.variant_id]["unpriceable"] += 1
            continue
        entry_ask, marks, entry_clock, legs = built
        for scenario in scenarios:
            exit_fill = px.resolve_option_exit(
                marks, entry_ask, float(scenario.stop_pct), float(scenario.take_profit_pct),
                gap_uses_open=params.gap_uses_open, stop_basis=params.stop_basis,
                time_exit_at_bar_open=params.time_exit_at_bar_open)
            priced[scenario.variant_id].append(_record(
                trade, book, scenario, expiry, entry_clock, exit_fill.ts, entry_ask, exit_fill.bid,
                exit_fill.reason, direction, len(legs), cfg))


def _baseline_record(trade, ctx, scenario, expiry, sigma, factor, spot, direction, params, cfg):
    target = _equity_target(trade, factor, direction)
    legs = px.structure_legs(
        scenario.structure, trade.entry.symbol, direction, spot, target,
        min_width_increments=params.vertical_min_width_increments)
    entry_clock = trade.entry.ts + px.HALF_BAR
    exit_ts, reason = _same_session_exit(trade, scenario.time_exit)
    exit_clock = px.fill_clock(exit_ts, reason)
    session = trade.entry.ts.date()
    entry_ask = _executable(legs, trade, spot, entry_clock, expiry, sigma, True, session, ctx, params, cfg, iv_mult=1.0)
    exit_spot = float(trade.exit.price) * factor
    exit_bid = _executable(
        legs, trade, exit_spot, exit_clock, expiry, sigma, False, session, ctx, params, cfg,
        iv_mult=cfg.exit_iv_mult)
    if not entry_ask > 0.0:
        return None
    return _record(
        trade, scenario.book, scenario, expiry, entry_clock, exit_ts, entry_ask, exit_bid, reason,
        direction, len(legs), cfg)


def _a2_path(trade, frame, ctx, expiry, sigma, factor, spot, direction, structure, sample, params, cfg):
    """Entry ask plus the bid path for one (book, structure). Shared by every stop×TP of that book."""
    if not params.time_exit_at_bar_open:
        raise NotImplementedError(
            "TODO_TIME_EXIT_BAR_RANGE: the time exit is the deadline bar's open; holding that bar is not implemented")
    target = _equity_target(trade, factor, direction)
    legs = px.structure_legs(
        structure, trade.entry.symbol, direction, spot, target,
        min_width_increments=params.vertical_min_width_increments)
    session = trade.entry.ts.date()
    entry_clock = trade.entry.ts + px.HALF_BAR
    entry_ask = _executable(legs, trade, spot, entry_clock, expiry, sigma, True, session, ctx, params, cfg, iv_mult=1.0)
    if not entry_ask > 0.01:
        return None
    deadline = sample.time_exit_on(session, ctx.early_closes, params)
    marks: list[px.BarMark] = []
    if frame is not None and len(frame):
        after = frame[frame["ts"] > trade.entry.ts]
        for bar in after.itertuples(index=False):
            if bar.ts.date() != session:
                break
            open_bid = _executable(
                legs, trade, float(bar.open) * factor, bar.ts, expiry, sigma, False, session, ctx, params, cfg,
                iv_mult=cfg.exit_iv_mult if bar.ts.time() >= deadline else 1.0)
            if bar.ts.time() >= deadline:
                marks.append(px.BarMark(bar.ts, open_bid, open_bid, open_bid, True))
                break
            # Call price rises with the underlying, so the close lies between low and high and
            # cannot breach a stop or target the high/low missed. Puts are the mirror image.
            # The close is therefore not a third trigger; the next bar's open is the next fill.
            adverse, favorable = (float(bar.low), float(bar.high)) if direction > 0 else (float(bar.high), float(bar.low))
            close_clock = bar.ts + timedelta(minutes=5)
            bid_adverse = _executable(
                legs, trade, adverse * factor, close_clock, expiry, sigma, False, session, ctx, params, cfg)
            bid_favorable = _executable(
                legs, trade, favorable * factor, close_clock, expiry, sigma, False, session, ctx, params, cfg)
            marks.append(px.BarMark(bar.ts, open_bid, bid_adverse, bid_favorable, False))
    if not marks:
        return None
    return entry_ask, marks, entry_clock, legs


def _executable(legs, trade, spot, when, expiry, sigma, buy, session, ctx, params, cfg, iv_mult: float = 1.0) -> float:
    late = px.zero_dte_late(
        expiry, session, when, only_on_expiry_session=params.late_spread_on_expiry_session)
    return px.package_price(
        legs, trade.entry.symbol, spot, px.year_fraction(when, expiry, ctx.calendar, ctx.early_closes, cfg),
        sigma, buy, late, cfg, iv_mult=iv_mult, late_mult_after_clamp=params.late_spread_after_clamp)


def _record(trade, book, scenario, expiry, entry_ts, exit_ts, entry_per_share, exit_per_share, reason, direction,
            n_legs, cfg) -> dict:
    return {
        "symbol": trade.entry.symbol,
        "session": trade.entry.ts.date(),
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "expiry": expiry,
        "book": book,
        "variant_id": scenario.variant_id,
        "structure": scenario.structure,
        "debit_pc": px.per_contract_cash(entry_per_share, n_legs, cfg.fee_per_contract, opening=True),
        "credit_pc": px.per_contract_cash(exit_per_share, n_legs, cfg.fee_per_contract, opening=False),
        "reason": reason,
        "direction": direction,
    }


def _equity_target(trade, factor: float, direction: int) -> float:
    """As-traded target for the vertical's short strike. Zone (or its alias) wins when the signal has one."""
    targets = getattr(trade.signal, "targets", {}) or {}
    fallback = (float(trade.entry.price) + direction * abs(float(trade.entry.price) - float(trade.signal.stop))) * factor
    key = "zone" if "zone" in targets else ("next_zone" if "next_zone" in targets else None)
    if key is None:
        return fallback
    value = targets.get(key, float("nan"))
    if value != value:  # NaN
        return fallback
    return float(value) * factor


def _same_session_exit(trade, flat_at: time) -> tuple[datetime, str]:
    """Baseline follows the equity exit, but never past ``flat_at`` on the entry session.

    Equity trades are flat by the 15:55 bar. A later timestamp is clamped so the option
    cannot be held overnight if a bad equity fill is passed in.
    """
    entry = trade.entry.ts
    exit_ts = trade.exit.ts
    flat_ts = datetime.combine(entry.date(), flat_at, tzinfo=entry.tzinfo)
    if exit_ts.date() != entry.date() or exit_ts > flat_ts:
        return flat_ts, "time_exit"
    return exit_ts, trade.exit.reason


def _session_frame(bars, symbol: str, session: date):
    frame = bars.session_frame(symbol, session)
    if frame is None or len(frame) == 0:
        return frame
    return frame.sort_values("ts")
