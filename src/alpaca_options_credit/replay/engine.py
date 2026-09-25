"""Replay the locked hybrid entry on underlying bars.

Each symbol carries its own arm, the same way the live engine does: a daily
confirm, then the first timing-bar pullback into the daily shelf and a
timing-bar reconfirm. Filters are applied at that moment. A rejection leaves
the arm up. Take-profit, the 1.5× credit stop, and the structure-break close
are not filtered — they always run.

One spread per underlying is enforced inside the symbol. The book-level
concurrency cap is applied later, on the finished trade list, so a filter
can be scored without the slot race deciding which name got filled.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Optional, Sequence

from alpaca_options_credit.models import Bar, Side
from alpaca_options_credit.replay.credit import (
    ExitFill,
    PricedSpread,
    ReplayLimits,
    earnings_near,
    implied_vol,
    modeled_proposal,
    realized_vol,
    simulate_exit,
    spread_mid,
    year_fraction,
)
from alpaca_options_credit.replay.filters import (
    EMA_PERIOD,
    RS_LOOKBACK,
    VOLUME_LOOKBACK,
    ema_last,
    prior_average,
    relative_strength_allows,
    session_allows,
    trailing_return,
    trend_allows,
    two_hvn_allows,
    volume_allows,
)
from alpaca_options_credit.replay.stats import ReplayTrade
from alpaca_options_credit.risk import size_contracts
from alpaca_options_credit.rth import ET, as_et
from alpaca_options_credit.strategy.structure import (
    DAILY_STRUCTURE_BREAK,
    StructureView,
    daily_close_through_invalidation,
    hybrid_entry,
)

Predicate = Callable[["EntryFeatures"], bool]


@dataclass
class StructureParams:
    left: int = 2
    right: int = 2
    atr_period: int = 14
    vp_lookback: int = 25
    vp_bin: float = 0.5
    vp_percentile: float = 0.70
    no_chase_atr: float = 0.5
    arm_timeout_bars: int = 10
    daily_lookback: int = 60
    timing_lookback: int = 120


@dataclass
class EntryFeatures:
    side: Side
    session_ok: bool
    rs_ok: bool
    two_hvn_ok: bool
    ema_daily_ok: bool
    ema_4h_ok: bool
    volume_ok: bool
    symbol: str = ""
    iv_pct: Optional[float] = None
    rv20: Optional[float] = None
    rv60: Optional[float] = None
    iv: Optional[float] = None
    vix: Optional[float] = None
    vix_pct: Optional[float] = None
    spy_rv20: Optional[float] = None
    earnings_soon: bool = False


@dataclass
class VariantDiag:
    ready: int = 0
    filter_reject: int = 0
    credit_skip: int = 0
    blocked: int = 0
    opened: int = 0


@dataclass
class _Arm:
    side: Side
    invalidation: float
    zone_low: float
    zone_high: float
    confirm_ts: datetime
    reason: str
    atr: float


@dataclass
class _Variant:
    name: str
    allow: Predicate
    arm: Optional[_Arm] = None
    busy_until: Optional[datetime] = None
    diag: VariantDiag = field(default_factory=VariantDiag)


@dataclass(frozen=True)
class _Block:
    end: datetime
    close: float


def bar_end(bar: Bar, minutes: int) -> datetime:
    """UTC close of a regular-session bar. The 15:30 hour ends at 16:00."""
    start = as_et(bar.ts)
    if minutes >= 60 and start.time() >= time(15, 30):
        end = datetime.combine(start.date(), time(16, 0), tzinfo=ET)
    else:
        end = start + timedelta(minutes=minutes)
    return end.astimezone(timezone.utc)


def daily_session_close(bar: Bar) -> datetime:
    local = as_et(bar.ts)
    close = datetime.combine(local.date(), time(16, 0), tzinfo=ET)
    return close.astimezone(timezone.utc)


def four_hour_blocks(timing: Sequence[Bar], minutes: int) -> list[_Block]:
    """Session blocks: 09:30–13:30 and 13:30–16:00, each known at its last bar.

    The morning block is not visible to a decision that happens before its
    last bar closes. That is the 4-hour trend the filter is allowed to see.
    """
    by_day: dict = {}
    for bar in timing:
        by_day.setdefault(as_et(bar.ts).date(), []).append(bar)
    blocks: list[_Block] = []
    for day in sorted(by_day):
        bars = by_day[day]
        morning = [b for b in bars if as_et(b.ts).time() < time(13, 30)]
        afternoon = [b for b in bars if as_et(b.ts).time() >= time(13, 30)]
        if morning:
            last = morning[-1]
            blocks.append(_Block(bar_end(last, minutes), last.close))
        if afternoon:
            last = afternoon[-1]
            blocks.append(_Block(bar_end(last, minutes), last.close))
    return blocks


def replay_symbol(
    symbol: str,
    daily: Sequence[Bar],
    timing: Sequence[Bar],
    spy_daily: Sequence[Bar],
    variants: dict[str, Predicate],
    *,
    minutes: int,
    limits: ReplayLimits,
    structure: StructureParams,
    limits_by_variant: Optional[dict[str, ReplayLimits]] = None,
    regime_by_symbol: Optional[dict] = None,
    earnings_by_symbol: Optional[dict] = None,
) -> tuple[list[ReplayTrade], dict[str, VariantDiag]]:
    """Walk one name. Returns every filled spread (all dates) plus diagnostics.

    ``limits_by_variant`` overrides entry or exit knobs for named variants.
    Variants that share the entry knobs share one priced proposal.
    """
    if len(daily) < 10 or len(timing) < structure.left + structure.right + 8:
        return [], {name: VariantDiag() for name in variants}

    slots = [_Variant(name, allow) for name, allow in variants.items()]
    spy_by_date = {as_et(bar.ts).date(): bar.close for bar in spy_daily}
    blocks = four_hour_blocks(timing, minutes)
    trades: list[ReplayTrade] = []
    cache: dict = {}
    price_cache: dict = {}

    def limits_for(name: str) -> ReplayLimits:
        if limits_by_variant and name in limits_by_variant:
            return limits_by_variant[name]
        return limits
    d_ptr = 0
    b_ptr = 0
    completed: list[Bar] = []
    block_closes: list[float] = []
    daily_sorted = list(daily)
    kwargs = {
        "left": structure.left,
        "right": structure.right,
        "atr_period": structure.atr_period,
        "vp_lookback": structure.vp_lookback,
        "vp_bin": structure.vp_bin,
        "vp_percentile": structure.vp_percentile,
        "no_chase_atr": structure.no_chase_atr,
    }

    for i, bar in enumerate(timing):
        end = bar_end(bar, minutes)
        while d_ptr < len(daily_sorted) and daily_session_close(daily_sorted[d_ptr]) <= end:
            completed.append(daily_sorted[d_ptr])
            d_ptr += 1
        while b_ptr < len(blocks) and blocks[b_ptr].end <= end:
            block_closes.append(blocks[b_ptr].close)
            b_ptr += 1
        if len(completed) < 10:
            continue

        structure_bars = completed[-structure.daily_lookback :]
        timing_bars = list(timing[max(0, i - structure.timing_lookback + 1) : i + 1])
        shared = _shared_features(
            completed,
            block_closes,
            timing_bars,
            bar,
            end,
            spy_by_date,
        )
        asof = as_et(completed[-1].ts).date()
        entry_day = as_et(end).date()
        snap = None
        if regime_by_symbol is not None:
            snap = (regime_by_symbol.get(symbol) or {}).get(asof)

        for slot in slots:
            if slot.busy_until is not None and bar.ts >= slot.busy_until:
                slot.busy_until = None
            _advance_arm(slot, structure_bars, timing_bars, completed, cache, kwargs, structure)
            if slot.arm is None or slot.busy_until is not None:
                continue
            view, ready = _ready_view(slot, structure_bars, timing_bars, cache, kwargs)
            if not ready or view.side is None or view.invalidation is None:
                continue
            slot.diag.ready += 1
            features_base = _features_for_side(shared, view.side, symbol=symbol, snap=snap)
            slot_limits = limits_for(slot.name)
            if slot_limits.earnings_blackout_days > 0 and _earnings_soon(
                earnings_by_symbol, symbol, entry_day, slot_limits.earnings_blackout_days
            ):
                slot.diag.filter_reject += 1
                continue
            # Strike-dependent HVN is filled after the credit model, because
            # the short is chosen by the same gate the live bot uses.
            if not _allow_before_strike(slot, features_base):
                slot.diag.filter_reject += 1
                continue
            price_key = (
                view.side,
                round(float(view.invalidation), 4),
                round(bar.close, 4),
                end,
                _entry_key(slot_limits),
            )
            if price_key not in price_cache:
                price_cache[price_key] = _price(
                    symbol, view, bar.close, end, completed, slot_limits
                )
            priced = price_cache[price_key]
            if priced is None or priced.proposal.skip:
                slot.diag.credit_skip += 1
                continue
            features = replace(
                features_base,
                two_hvn_ok=two_hvn_allows(
                    completed[-structure.vp_lookback :],
                    priced.proposal.short.strike,
                    view.atr,
                    bin_size=structure.vp_bin,
                    percentile=structure.vp_percentile,
                ),
            )
            if not slot.allow(features):
                slot.diag.filter_reject += 1
                continue
            if slot_limits.earnings_blackout_days > 0 and _earnings_soon(
                earnings_by_symbol,
                symbol,
                priced.expiration,
                slot_limits.earnings_blackout_days,
            ):
                slot.diag.filter_reject += 1
                continue
            if daily_close_through_invalidation(
                view.side, float(view.invalidation), structure_bars[-1].close
            ):
                slot.arm = None
                slot.diag.blocked += 1
                continue
            trade = _open_trade(
                symbol,
                slot.name,
                view,
                priced,
                slot_limits,
                timing,
                i,
                minutes,
                daily_sorted,
            )
            if trade is None:
                slot.diag.credit_skip += 1
                continue
            trades.append(trade)
            slot.diag.opened += 1
            slot.busy_until = trade.exit_time
            slot.arm = None

    return trades, {slot.name: slot.diag for slot in slots}


def replay_universe(
    bars_by_symbol: dict[str, dict[str, list[Bar]]],
    symbols: Sequence[str],
    variants: dict[str, Predicate],
    *,
    timing_key: str,
    minutes: int,
    limits: ReplayLimits,
    structure: StructureParams,
    spy_symbol: str = "SPY",
    limits_by_variant: Optional[dict[str, ReplayLimits]] = None,
    regime_by_symbol: Optional[dict] = None,
    earnings_by_symbol: Optional[dict] = None,
) -> tuple[dict[str, list[ReplayTrade]], dict[str, VariantDiag]]:
    spy_daily = bars_by_symbol.get(spy_symbol, {}).get("1d") or []
    grouped: dict[str, list[ReplayTrade]] = {name: [] for name in variants}
    diags = {name: VariantDiag() for name in variants}
    for symbol in symbols:
        print(f"  {symbol} {timing_key}", flush=True)
        series = bars_by_symbol.get(symbol) or {}
        daily = series.get("1d") or []
        timing = series.get(timing_key) or []
        trades, symbol_diags = replay_symbol(
            symbol,
            daily,
            timing,
            spy_daily,
            variants,
            minutes=minutes,
            limits=limits,
            structure=structure,
            limits_by_variant=limits_by_variant,
            regime_by_symbol=regime_by_symbol,
            earnings_by_symbol=earnings_by_symbol,
        )
        for trade in trades:
            grouped[trade.variant].append(trade)
        for name, diag in symbol_diags.items():
            total = diags[name]
            total.ready += diag.ready
            total.filter_reject += diag.filter_reject
            total.credit_skip += diag.credit_skip
            total.blocked += diag.blocked
            total.opened += diag.opened
    for name in grouped:
        grouped[name].sort(key=lambda t: (t.entry_time, t.symbol))
    return grouped, diags


@dataclass
class _Shared:
    session_ok: bool
    sym_ret: Optional[float]
    spy_ret: Optional[float]
    daily_close: Optional[float]
    daily_ema: Optional[float]
    h4_close: Optional[float]
    h4_ema: Optional[float]
    bar_volume: float
    vol_avg: Optional[float]


def _shared_features(completed, block_closes, timing_bars, bar, end, spy_by_date) -> _Shared:
    closes = [b.close for b in completed]
    sym_ret = trailing_return(closes, RS_LOOKBACK)
    spy_ret = _spy_return(completed, spy_by_date, RS_LOOKBACK)
    return _Shared(
        session_ok=session_allows(bar.ts, end),
        sym_ret=sym_ret,
        spy_ret=spy_ret,
        daily_close=closes[-1] if closes else None,
        daily_ema=ema_last(closes, EMA_PERIOD),
        h4_close=block_closes[-1] if block_closes else None,
        h4_ema=ema_last(block_closes, EMA_PERIOD),
        bar_volume=bar.volume,
        vol_avg=prior_average([b.volume for b in timing_bars[:-1]], VOLUME_LOOKBACK),
    )


def _spy_return(completed: Sequence[Bar], spy_by_date: dict, lookback: int) -> Optional[float]:
    if len(completed) < lookback + 1:
        return None
    tail = completed[-(lookback + 1) :]
    d0 = as_et(tail[0].ts).date()
    d1 = as_et(tail[-1].ts).date()
    p0 = spy_by_date.get(d0)
    p1 = spy_by_date.get(d1)
    if p0 is None or p1 is None or p0 <= 0:
        return None
    return p1 / p0 - 1.0


def _features_for_side(shared: _Shared, side: Side, *, symbol: str = "", snap=None) -> EntryFeatures:
    return EntryFeatures(
        side=side,
        session_ok=shared.session_ok,
        rs_ok=relative_strength_allows(side, shared.sym_ret, shared.spy_ret),
        two_hvn_ok=False,
        ema_daily_ok=trend_allows(side, shared.daily_close, shared.daily_ema),
        ema_4h_ok=trend_allows(side, shared.h4_close, shared.h4_ema),
        volume_ok=volume_allows(shared.bar_volume, shared.vol_avg),
        symbol=symbol,
        iv_pct=None if snap is None else snap.iv_pct,
        rv20=None if snap is None else snap.rv20,
        rv60=None if snap is None else snap.rv60,
        iv=None if snap is None else snap.iv,
        vix=None if snap is None else snap.vix,
        vix_pct=None if snap is None else snap.vix_pct,
        spy_rv20=None if snap is None else snap.spy_rv20,
    )


def _allow_before_strike(slot: _Variant, features: EntryFeatures) -> bool:
    """Reject on filters that do not need the short strike, without a chain."""
    if slot.name == "two_hvn_at_short":
        return True
    return slot.allow(replace(features, two_hvn_ok=True))


def _earnings_soon(earnings_by_symbol, symbol: str, day: date, days: int) -> bool:
    if not earnings_by_symbol or days <= 0:
        return False
    return earnings_near(earnings_by_symbol.get(symbol), day, days)


def _advance_arm(slot, structure_bars, timing_bars, completed, cache, kwargs, structure: StructureParams) -> None:
    existing = _view_from_arm(slot.arm, structure_bars[-1].close) if slot.arm else None
    view, _ready, why = _hybrid(cache, structure_bars, timing_bars, existing, kwargs)
    if slot.arm is not None:
        if why == DAILY_STRUCTURE_BREAK:
            slot.arm = None
            return
        n_after = sum(1 for bar in completed if bar.ts > slot.arm.confirm_ts)
        if n_after > structure.arm_timeout_bars:
            slot.arm = None
            return
        return
    if view.confirmed and view.side is not None and view.invalidation is not None and view.confirm_ts is not None:
        slot.arm = _Arm(
            side=view.side,
            invalidation=float(view.invalidation),
            zone_low=float(view.zone_low if view.zone_low is not None else view.invalidation),
            zone_high=float(view.zone_high if view.zone_high is not None else view.invalidation),
            confirm_ts=view.confirm_ts,
            reason=view.reason,
            atr=float(view.atr or 0.0),
        )


def _ready_view(slot, structure_bars, timing_bars, cache, kwargs):
    existing = _view_from_arm(slot.arm, structure_bars[-1].close) if slot.arm else None
    view, ready, why = _hybrid(cache, structure_bars, timing_bars, existing, kwargs)
    if why == DAILY_STRUCTURE_BREAK:
        return view, False
    return view, ready


def _view_from_arm(arm: _Arm, last_close: float) -> StructureView:
    return StructureView(
        side=arm.side,
        confirmed=True,
        invalidation=arm.invalidation,
        zone_low=arm.zone_low,
        zone_high=arm.zone_high,
        confirm_index=0,
        reason=arm.reason,
        last_close=last_close,
        atr=arm.atr,
        confirm_ts=arm.confirm_ts,
    )


def _hybrid(cache, structure_bars, timing_bars, existing, kwargs):
    arm_key = None
    if existing is not None:
        arm_key = (
            existing.side,
            round(float(existing.invalidation or 0.0), 4),
            existing.confirm_ts,
            round(float(existing.zone_low or 0.0), 4),
            round(float(existing.zone_high or 0.0), 4),
        )
    key = (structure_bars[-1].ts, len(structure_bars), timing_bars[-1].ts, len(timing_bars), arm_key)
    hit = cache.get(key)
    if hit is None:
        hit = hybrid_entry(structure_bars, timing_bars, existing=existing, **kwargs)
        cache[key] = hit
    return hit


def _entry_key(limits: ReplayLimits) -> tuple:
    """Entry knobs only. Stop and take-profit do not change the credit."""
    return (
        limits.width,
        limits.dte_min,
        limits.dte_max,
        limits.dte_target,
        round(limits.min_credit_pct, 6),
        round(limits.max_credit_pct, 6),
        round(limits.min_short_inv_gap, 6),
        limits.target_abs_delta,
        limits.width_pct,
        limits.delta_tol,
    )


def _filled_credit(natural: float, mid: float, fill_mode: str) -> float:
    """Credit actually booked. The gate still uses the natural credit."""
    if fill_mode == "mid":
        return mid
    if fill_mode == "nickel":
        return natural - 0.05
    return natural


def _price(symbol, view, spot, when, completed, limits: ReplayLimits) -> Optional[PricedSpread]:
    rv = realized_vol([bar.close for bar in completed])
    if rv is None:
        return None
    iv = implied_vol(rv)
    return modeled_proposal(
        symbol=symbol,
        side=view.side,
        invalidation=float(view.invalidation),
        spot=spot,
        when=when,
        iv=iv,
        limits=limits,
    )


def _open_trade(
    symbol: str,
    variant: str,
    view: StructureView,
    priced: PricedSpread,
    limits: ReplayLimits,
    timing: Sequence[Bar],
    index: int,
    minutes: int,
    daily: Sequence[Bar],
) -> Optional[ReplayTrade]:
    proposal = priced.proposal
    # Full daily tape, revealed only at each session close. The entry-time
    # slice would hide a later structure break.
    daily_by_date = {as_et(bar.ts).date(): bar.close for bar in daily}

    def _daily_close_at(when: datetime):
        local = as_et(when)
        if local.time() < time(16, 0):
            return None
        return daily_by_date.get(local.date())

    right = "put" if view.side is Side.BULLISH else "call"
    bar_close = timing[index].close
    entry_mid = spread_mid(
        bar_close,
        proposal.short.strike,
        proposal.long.strike,
        year_fraction(bar_end(timing[index], minutes), priced.expiration),
        priced.iv,
        right,
    )
    credit = _filled_credit(proposal.credit, entry_mid, limits.fill_mode)
    if credit <= 0:
        return None
    qty = size_contracts(
        limits.equity,
        limits.risk_pct,
        proposal.width,
        credit,
        limits.multiplier,
    )
    if qty < 1:
        return None
    if view.side is Side.BULLISH:
        shelf = None if view.zone_low is None else float(view.zone_low)
    else:
        shelf = None if view.zone_high is None else float(view.zone_high)
    fill: ExitFill = simulate_exit(
        side=view.side,
        short_k=proposal.short.strike,
        long_k=proposal.long.strike,
        credit=credit,
        iv=priced.iv,
        expiration=priced.expiration,
        invalidation=float(view.invalidation),
        bars=timing,
        start_index=index,
        bar_end_fn=lambda bar: bar_end(bar, minutes),
        daily_close_at=_daily_close_at,
        limits=limits,
        shelf=shelf,
    )
    pnl = (credit - fill.debit) * qty * limits.multiplier
    max_loss = (proposal.width - credit) * qty * limits.multiplier
    return ReplayTrade(
        symbol=symbol,
        side=view.side.value,
        variant=variant,
        entry_time=bar_end(timing[index], minutes),
        exit_time=fill.when,
        exit_reason=fill.reason,
        credit=credit,
        debit=fill.debit,
        width=proposal.width,
        qty=qty,
        max_loss=max_loss,
        pnl=pnl,
        short_strike=proposal.short.strike,
        iv=priced.iv,
        long_strike=proposal.long.strike,
        expiration=priced.expiration,
        entry_spot=bar_close,
        entry_mid=entry_mid,
        short_delta=priced.short_delta,
        open_mid=fill.open_mid,
        adverse_mid=fill.adverse_mid,
        close_mid=fill.close_mid,
        open_natural=fill.open_natural,
        adverse_natural=fill.adverse_natural,
        close_natural=fill.close_natural,
    )
