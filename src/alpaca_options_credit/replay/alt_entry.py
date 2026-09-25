"""Extension and iron-condor entries on the same hourly tape as the confirm replay.

A confirm entry is a breakout retest. These two are the other timing:

- extension: sell the put after a down move into a support shelf, or the call
  after an up move into a resistance shelf. The short has to clear that shelf
  by the gap, then sit near the target delta.
- condor: sell both sides when the close is in the middle of the prior 20-day
  range and within one ATR of the EMA50. Each short clears its side of the range.

One name, one open structure. A credit skip leaves the episode up so a later
bar can still fill. A fill consumes the episode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional, Sequence

from alpaca_options_credit.models import Bar, Side
from alpaca_options_credit.replay.credit import (
    ReplayLimits,
    earnings_near,
    simulate_condor_exit,
    spread_mid,
    year_fraction,
)
from alpaca_options_credit.replay.engine import (
    StructureParams,
    VariantDiag,
    _filled_credit,
    _open_trade,
    bar_end,
    daily_session_close,
)
from alpaca_options_credit.replay.regime import RegimeSnap, extension_touch, range_anchors
from alpaca_options_credit.strategy.volume_profile import hvn_shelves
from alpaca_options_credit.replay.stats import ReplayTrade
from alpaca_options_credit.risk import size_contracts
from alpaca_options_credit.rth import as_et
from alpaca_options_credit.strategy.structure import StructureView, atr


@dataclass
class AltSpec:
    name: str
    kind: str  # extension | condor
    limits: ReplayLimits
    etf_only: bool = False
    stock_only: bool = False
    trend: bool = False
    iv_pct_min: Optional[float] = None
    iv_over_rv: bool = False
    vix_pct_min: Optional[float] = None
    vix_rich: bool = False


@dataclass
class _Slot:
    spec: AltSpec
    busy_until: Optional[datetime] = None
    consumed: set = field(default_factory=set)
    diag: VariantDiag = field(default_factory=VariantDiag)


def _filters_ok(spec: AltSpec, symbol: str, side: Optional[Side], snap: Optional[RegimeSnap], etfs: set[str]) -> bool:
    if spec.etf_only and symbol not in etfs:
        return False
    if spec.stock_only and symbol in etfs:
        return False
    if snap is None:
        if spec.iv_pct_min is not None or spec.iv_over_rv or spec.vix_pct_min is not None or spec.vix_rich:
            return False
        if spec.trend:
            return False
        return True
    if spec.trend:
        if side is None or snap.ema50 is None:
            return False
        if side is Side.BULLISH and not (snap.close > snap.ema50):
            return False
        if side is Side.BEARISH and not (snap.close < snap.ema50):
            return False
    if spec.iv_pct_min is not None and (snap.iv_pct is None or snap.iv_pct < spec.iv_pct_min):
        return False
    if spec.iv_over_rv and (snap.iv is None or snap.rv60 is None or not (snap.iv > snap.rv60)):
        return False
    if spec.vix_pct_min is not None and (snap.vix_pct is None or snap.vix_pct < spec.vix_pct_min):
        return False
    if spec.vix_rich and (snap.vix is None or snap.spy_rv20 is None or snap.vix / 100.0 <= snap.spy_rv20):
        return False
    return True


def _earnings_block(spec: AltSpec, earnings, symbol: str, *days) -> bool:
    if spec.limits.earnings_blackout_days <= 0 or not earnings:
        return False
    dates = earnings.get(symbol)
    return any(earnings_near(dates, day, spec.limits.earnings_blackout_days) for day in days)


def replay_alt_symbol(
    symbol: str,
    daily: Sequence[Bar],
    timing: Sequence[Bar],
    specs: Sequence[AltSpec],
    *,
    minutes: int,
    regime: dict,
    earnings: dict,
    etfs: set[str],
    structure: StructureParams,
) -> tuple[list[ReplayTrade], dict[str, VariantDiag]]:
    if not specs or len(daily) < 10 or len(timing) < 8:
        return [], {spec.name: VariantDiag() for spec in specs}

    slots = [_Slot(spec) for spec in specs]
    trades: list[ReplayTrade] = []
    price_cache: dict = {}
    shelf_cache: dict = {}
    d_ptr = 0
    completed: list[Bar] = []
    episodes: dict[tuple, int] = {}
    next_episode = 0
    snaps = regime.get(symbol) or {}

    for i, bar in enumerate(timing):
        end = bar_end(bar, minutes)
        while d_ptr < len(daily) and daily_session_close(daily[d_ptr]) <= end:
            completed.append(daily[d_ptr])
            d_ptr += 1
        today = as_et(bar.ts).date()
        prior = completed[:-1] if completed and as_et(completed[-1].ts).date() == today else completed
        if len(prior) < 60:
            continue
        prior_day = as_et(prior[-1].ts).date()
        snap = snaps.get(prior_day)
        decision_snap = snaps.get(as_et(completed[-1].ts).date()) if completed else snap
        atr_value = atr(prior, structure.atr_period)
        shelf_key = prior[-1].ts
        shelves = shelf_cache.get(shelf_key)
        if shelves is None:
            look = prior[-structure.vp_lookback :]
            shelves = hvn_shelves(
                look, bin_size=structure.vp_bin, percentile=structure.vp_percentile
            )
            shelf_cache[shelf_key] = shelves

        touch = None
        if len(prior) >= 6 and atr_value > 0:
            touch = extension_touch(
                prior_close=prior[-1].close,
                close_5=prior[-6].close,
                atr_value=atr_value,
                bar_high=bar.high,
                bar_low=bar.low,
                bar_close=bar.close,
                shelves=shelves,
            )
        live_keys = set()
        episode_id = None
        if touch is not None:
            key = (touch.side, round(touch.anchor, 2))
            live_keys.add(key)
            if key not in episodes:
                next_episode += 1
                episodes[key] = next_episode
            episode_id = episodes[key]
        for key in [k for k in episodes if k not in live_keys]:
            del episodes[key]

        last_hour = as_et(bar.ts).time() >= time(15, 30)
        anchors = None
        if last_hour:
            close_px = completed[-1].close if completed and as_et(completed[-1].ts).date() == today else bar.close
            box_snap = decision_snap or snap
            anchors = range_anchors(
                prior,
                close_px,
                None if box_snap is None else box_snap.ema50,
                None if box_snap is None else box_snap.atr,
            )

        for slot in slots:
            if slot.busy_until is not None and bar.ts >= slot.busy_until:
                slot.busy_until = None
            if slot.busy_until is not None:
                continue
            spec = slot.spec
            if spec.kind == "extension":
                if touch is None or episode_id is None:
                    continue
                if episode_id in slot.consumed:
                    continue
                slot.diag.ready += 1
                if not _filters_ok(spec, symbol, touch.side, snap, etfs):
                    slot.diag.filter_reject += 1
                    continue
                if _earnings_block(spec, earnings, symbol, today):
                    slot.diag.filter_reject += 1
                    continue
                trade = _open_vertical(
                    symbol,
                    spec,
                    touch.side,
                    touch.anchor,
                    touch.shelf_low,
                    touch.shelf_high,
                    bar.close,
                    end,
                    prior,
                    timing,
                    i,
                    minutes,
                    daily,
                    price_cache,
                    snap,
                )
                if trade is None:
                    slot.diag.credit_skip += 1
                    continue
                if _earnings_block(spec, earnings, symbol, trade.expiration):
                    slot.diag.filter_reject += 1
                    continue
                trades.append(trade)
                slot.diag.opened += 1
                slot.consumed.add(episode_id)
                slot.busy_until = trade.exit_time
            elif spec.kind == "condor" and last_hour:
                day_id = today.toordinal()
                if day_id in slot.consumed:
                    continue
                slot.diag.ready += 1
                if anchors is None:
                    slot.diag.filter_reject += 1
                    slot.consumed.add(day_id)
                    continue
                box_snap = decision_snap or snap
                if not _filters_ok(spec, symbol, None, box_snap, etfs):
                    slot.diag.filter_reject += 1
                    slot.consumed.add(day_id)
                    continue
                if _earnings_block(spec, earnings, symbol, today):
                    slot.diag.filter_reject += 1
                    slot.consumed.add(day_id)
                    continue
                trade = _open_condor(
                    symbol,
                    spec,
                    anchors[0],
                    anchors[1],
                    bar.close,
                    end,
                    prior,
                    timing,
                    i,
                    minutes,
                    daily,
                    price_cache,
                    box_snap,
                )
                slot.consumed.add(day_id)
                if trade is None:
                    slot.diag.credit_skip += 1
                    continue
                if trade.expiration is not None and _earnings_block(spec, earnings, symbol, trade.expiration):
                    slot.diag.filter_reject += 1
                    continue
                trades.append(trade)
                slot.diag.opened += 1
                slot.busy_until = trade.exit_time

    return trades, {slot.spec.name: slot.diag for slot in slots}


def _open_vertical(
    symbol,
    spec: AltSpec,
    side: Side,
    anchor: float,
    shelf_low: float,
    shelf_high: float,
    spot: float,
    when: datetime,
    prior: Sequence[Bar],
    timing: Sequence[Bar],
    index: int,
    minutes: int,
    daily: Sequence[Bar],
    cache: dict,
    snap: Optional[RegimeSnap],
) -> Optional[ReplayTrade]:
    if snap is None or snap.iv is None or snap.iv <= 0:
        return None
    key = (
        "v",
        spec.name,
        side,
        round(anchor, 4),
        round(spot, 4),
        when,
    )
    priced = cache.get(key)
    if priced is None and key not in cache:
        view = StructureView(
            side=side,
            confirmed=True,
            invalidation=anchor,
            zone_low=shelf_low,
            zone_high=shelf_high,
            confirm_index=0,
            reason="extension",
            last_close=spot,
            atr=snap.atr or 0.0,
            confirm_ts=when,
        )
        from alpaca_options_credit.replay.engine import _price

        priced = _price(symbol, view, spot, when, prior, spec.limits)
        cache[key] = priced
    else:
        priced = cache.get(key)
    if priced is None or priced.proposal.skip:
        return None
    view = StructureView(
        side=side,
        confirmed=True,
        invalidation=anchor,
        zone_low=shelf_low,
        zone_high=shelf_high,
        confirm_index=0,
        reason="extension",
        last_close=spot,
        atr=snap.atr or 0.0,
        confirm_ts=when,
    )
    return _open_trade(
        symbol,
        spec.name,
        view,
        priced,
        spec.limits,
        timing,
        index,
        minutes,
        daily,
    )


def _open_condor(
    symbol,
    spec: AltSpec,
    range_low: float,
    range_high: float,
    spot: float,
    when: datetime,
    prior: Sequence[Bar],
    timing: Sequence[Bar],
    index: int,
    minutes: int,
    daily: Sequence[Bar],
    cache: dict,
    snap: Optional[RegimeSnap],
) -> Optional[ReplayTrade]:
    if snap is None or snap.iv is None or snap.iv <= 0 or range_high <= range_low:
        return None
    from alpaca_options_credit.replay.engine import _price

    def _one(side: Side, anchor: float):
        key = ("c", spec.name, side, round(anchor, 4), round(spot, 4), when)
        if key not in cache:
            view = StructureView(
                side=side,
                confirmed=True,
                invalidation=anchor,
                zone_low=anchor,
                zone_high=anchor,
                confirm_index=0,
                reason="condor",
                last_close=spot,
                atr=snap.atr or 0.0,
                confirm_ts=when,
            )
            cache[key] = _price(symbol, view, spot, when, prior, spec.limits)
        return cache[key]

    put = _one(Side.BULLISH, range_low)
    call = _one(Side.BEARISH, range_high)
    if put is None or call is None or put.proposal.skip or call.proposal.skip:
        return None
    if put.expiration != call.expiration:
        return None
    put_credit = put.proposal.credit
    call_credit = call.proposal.credit
    natural = put_credit + call_credit
    right_put = "put"
    t_years = year_fraction(when, put.expiration)
    put_mid = spread_mid(spot, put.proposal.short.strike, put.proposal.long.strike, t_years, put.iv, right_put)
    call_mid = spread_mid(
        spot, call.proposal.short.strike, call.proposal.long.strike, t_years, call.iv, "call"
    )
    credit = _filled_credit(natural, put_mid + call_mid, spec.limits.fill_mode)
    width = put.proposal.width
    if credit <= 0 or credit >= width:
        return None
    qty = size_contracts(spec.limits.equity, spec.limits.risk_pct, width, credit, spec.limits.multiplier)
    if qty < 1:
        return None
    if spec.limits.spot_stop == "short":
        put_level = put.proposal.short.strike
        call_level = call.proposal.short.strike
    elif spec.limits.spot_stop == "none":
        put_level = -1e18
        call_level = 1e18
    else:
        put_level = range_low
        call_level = range_high

    daily_by_date = {as_et(bar.ts).date(): bar.close for bar in daily}

    def _daily_close_at(moment: datetime):
        local = as_et(moment)
        if local.time() < time(16, 0):
            return None
        return daily_by_date.get(local.date())

    fill = simulate_condor_exit(
        put_short=put.proposal.short.strike,
        put_long=put.proposal.long.strike,
        call_short=call.proposal.short.strike,
        call_long=call.proposal.long.strike,
        credit=credit,
        iv=put.iv,
        expiration=put.expiration,
        put_level=put_level,
        call_level=call_level,
        bars=timing,
        start_index=index,
        bar_end_fn=lambda item: bar_end(item, minutes),
        daily_close_at=_daily_close_at,
        limits=spec.limits,
    )
    pnl = (credit - fill.debit) * qty * spec.limits.multiplier
    max_loss = (width - credit) * qty * spec.limits.multiplier
    return ReplayTrade(
        symbol=symbol,
        side="condor",
        variant=spec.name,
        entry_time=when,
        exit_time=fill.when,
        exit_reason=fill.reason,
        credit=credit,
        debit=fill.debit,
        width=width,
        qty=qty,
        max_loss=max_loss,
        pnl=pnl,
        short_strike=put.proposal.short.strike,
        iv=put.iv,
        long_strike=call.proposal.short.strike,
        expiration=put.expiration,
        entry_spot=spot,
        entry_mid=put_mid + call_mid,
        short_delta=(abs(put.short_delta) + abs(call.short_delta)) / 2.0,
    )


def replay_alt_universe(
    bars_by_symbol: dict[str, dict[str, list[Bar]]],
    symbols: Sequence[str],
    specs: Sequence[AltSpec],
    *,
    regime: dict,
    earnings: dict,
    etfs: set[str],
    structure: StructureParams,
    minutes: int = 60,
) -> tuple[dict[str, list[ReplayTrade]], dict[str, VariantDiag]]:
    grouped: dict[str, list[ReplayTrade]] = {spec.name: [] for spec in specs}
    diags = {spec.name: VariantDiag() for spec in specs}
    for symbol in symbols:
        print(f"  alt {symbol}", flush=True)
        series = bars_by_symbol.get(symbol) or {}
        trades, symbol_diags = replay_alt_symbol(
            symbol,
            series.get("1d") or [],
            series.get("1h") or [],
            specs,
            minutes=minutes,
            regime=regime,
            earnings=earnings,
            etfs=etfs,
            structure=structure,
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
