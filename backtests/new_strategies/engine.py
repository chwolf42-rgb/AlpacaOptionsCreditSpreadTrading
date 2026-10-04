"""Daily portfolio simulator for defined-risk, atomic two-leg structures.

Decisions use only bars that have closed. A signal on session T fills at
the next session's open. Intrabar stops fill at the stop value; a gap
through the open fills at the open. If a stop and a target are both
inside the same bar, the stop wins. Implied vol used to mark session T
is the curve from the previous session's close.

Iron condors and butterflies are two atomic two-leg orders. The second
wing is filled on the following open, or not at all. Each wing counts
as one spread. Open risk adds both wings and reserves the second wing's
0.5% budget while it is waiting. That is the leg-timing model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from backtests.new_strategies.pricing import (
    bs_delta,
    bs_price,
    buy_fill,
    full_width,
    interp_variance_vol,
    realized_vol,
    rolling_mean,
    sell_fill,
    skew_iv,
    snap_strike,
    strike_step,
)
from backtests.new_strategies.specs import (
    ACCOUNT,
    DIVIDENDS,
    EXIT_DTE,
    FEE_PER_CONTRACT_SIDE,
    INDEX_ETFS,
    MAX_CONTRACTS,
    MAX_GROSS,
    MAX_OPEN_RISK_PCT,
    MAX_SPREADS,
    MULTIPLIER,
    RISK_PCT,
    UNIVERSE,
    params_id,
)


@dataclass
class SymbolTape:
    dates: list[date]
    opens: list[float]
    highs: list[float]
    lows: list[float]
    closes: list[float]
    index: dict[date, int]


@dataclass
class Curve:
    vix: float | None
    vix9d: float | None
    vix3m: float | None
    vxn: float | None
    rvx: float | None
    rate: float
    vix_sma20: float | None
    vix_pct: float | None


@dataclass
class Prepared:
    calendar: list[date]
    symbols: dict[str, SymbolTape]
    curves: list[Curve]
    spy_rv20: list[float | None]


@dataclass
class Position:
    pid: int
    symbol: str
    strategy: str
    params_id: str
    group_id: int | None
    role: str
    kind: str  # debit or credit
    right: str
    adverse: str  # down, up, either
    long_strike: float
    short_strike: float
    long_expiry: date
    short_expiry: date
    qty: int
    cost: float
    max_loss_ps: float
    risk_dollars: float
    target_value: float
    stop_value: float
    exit_dte: int
    entry_date: date
    signal_date: date
    entry_fee: float
    entry_bar: bool = True
    params: dict = field(default_factory=dict)


@dataclass
class Group:
    gid: int
    symbol: str
    strategy: str
    params_id: str
    params: dict
    signal_date: date
    signal_close: float
    atr: float
    stage: str
    reserved: bool
    reserve_dollars: float
    body: tuple[float, float, float] | None = None
    put_short: float | None = None
    call_short: float | None = None


@dataclass
class Intent:
    symbol: str
    strategy: str
    params_id: str
    params: dict
    kind: str
    signal_date: date
    signal_close: float
    atr: float
    right: str | None = None
    group_id: int | None = None
    wing: int = 1
    body: tuple[float, float, float] | None = None


def _asof_list(pairs: list[tuple[date, float]], calendar: list[date]) -> list[float | None]:
    pairs = sorted(pairs)
    out: list[float | None] = []
    j = 0
    last: float | None = None
    for day in calendar:
        while j < len(pairs) and pairs[j][0] <= day:
            last = pairs[j][1]
            j += 1
        out.append(last)
    return out


def _percentile(history: list[float | None], i: int, window: int = 252) -> float | None:
    """Share of the prior ``window`` points strictly below curves[i]. Not including i."""
    if i < window:
        return None
    current = history[i]
    if current is None:
        return None
    prev = [v for v in history[i - window : i] if v is not None]
    if len(prev) < window:
        return None
    return sum(1 for v in prev if v < current) / len(prev)


def prepare(market: dict) -> Prepared:
    spy = market["underlyings"]["SPY"]
    calendar = [row["date"] for row in spy]
    symbols: dict[str, SymbolTape] = {}
    for name, rows in market["underlyings"].items():
        dates = [r["date"] for r in rows]
        symbols[name] = SymbolTape(
            dates=dates,
            opens=[r["open"] for r in rows],
            highs=[r["high"] for r in rows],
            lows=[r["low"] for r in rows],
            closes=[r["close"] for r in rows],
            index={d: i for i, d in enumerate(dates)},
        )
    series = market["series"]
    # CBOE and FRED publish these indexes in percentage points (16.3 = 16.3%).
    def _pct(pairs: list[tuple[date, float]]) -> list[tuple[date, float]]:
        return [(d, v / 100.0) for d, v in pairs]

    vix = _asof_list(_pct(series["vix"]), calendar)
    vix9d = _asof_list(_pct(series["vix9d"]), calendar)
    vix3m = _asof_list(_pct(series["vix3m"]), calendar)
    vxn = _asof_list(_pct(series["vxn"]), calendar)
    rvx = _asof_list(_pct(series["rvx"]), calendar)
    rate_pct = _asof_list(series["rate"], calendar)
    vix_sma: list[float | None] = []
    for i, value in enumerate(vix):
        if value is None or i < 19:
            vix_sma.append(None)
            continue
        window = [v for v in vix[i - 19 : i + 1] if v is not None]
        vix_sma.append(sum(window) / len(window) if len(window) == 20 else None)
    curves: list[Curve] = []
    for i, day in enumerate(calendar):
        raw_rate = rate_pct[i]
        rate = 0.02 if raw_rate is None else max(0.0, raw_rate / 100.0)
        curves.append(
            Curve(
                vix=vix[i],
                vix9d=vix9d[i],
                vix3m=vix3m[i],
                vxn=vxn[i],
                rvx=rvx[i],
                rate=rate,
                vix_sma20=vix_sma[i],
                vix_pct=_percentile(vix, i),
            )
        )
    spy_tape = symbols["SPY"]
    spy_rv20 = [realized_vol(spy_tape.closes, i, 20) for i in range(len(spy_tape.closes))]
    # SPY tape and calendar are the same length by construction.
    return Prepared(calendar=calendar, symbols=symbols, curves=curves, spy_rv20=spy_rv20)


def _pick_friday(today: date, target: int, min_dte: int, max_dte: int) -> date | None:
    best: date | None = None
    best_dist = 10**9
    for add in range(max(1, min_dte), max_dte + 1):
        day = today + timedelta(days=add)
        if day.weekday() != 4:
            continue
        dist = abs(add - target)
        if dist < best_dist:
            best = day
            best_dist = dist
    return best


def _atr(tape: SymbolTape, i: int, n: int = 14) -> float | None:
    if i < n:
        return None
    total = 0.0
    for j in range(i - n + 1, i + 1):
        prev = tape.closes[j - 1]
        total += max(
            tape.highs[j] - tape.lows[j],
            abs(tape.highs[j] - prev),
            abs(tape.lows[j] - prev),
        )
    return total / n


def _level_30(symbol: str, curve: Curve, rv20: float | None, spy_rv: float | None) -> float | None:
    if symbol == "SPY":
        return curve.vix
    if symbol == "QQQ":
        if curve.vxn:
            return curve.vxn
        if curve.vix and rv20 and spy_rv and spy_rv > 0:
            return curve.vix * (rv20 / spy_rv)
        return curve.vix
    if symbol == "IWM":
        if curve.rvx:
            return curve.rvx
        if curve.vix and rv20 and spy_rv and spy_rv > 0:
            return curve.vix * (rv20 / spy_rv)
        return curve.vix
    if rv20 is None or curve.vix is None:
        return None
    if spy_rv and spy_rv > 0.05:
        prem = min(2.0, max(0.8, curve.vix / spy_rv))
    else:
        prem = 1.15
    return min(1.50, max(0.12, rv20 * prem))


def _iv_at(
    symbol: str,
    dte: int,
    spot: float,
    strike: float,
    t_years: float,
    curve: Curve,
    rv20: float | None,
    spy_rv: float | None,
    iv_model: str,
) -> float | None:
    if iv_model == "flat_rv":
        if rv20 is None:
            return None
        return min(1.25, max(0.15, rv20 * 1.15))
    level = _level_30(symbol, curve, rv20, spy_rv)
    if level is None or level <= 0:
        return None
    shaped = level
    if curve.vix and curve.vix9d and curve.vix3m and curve.vix > 0:
        raw = interp_variance_vol(
            [(9, curve.vix9d), (30, curve.vix), (93, curve.vix3m)],
            dte,
        )
        shaped = level * (raw / curve.vix)
    return skew_iv(shaped, spot, strike, t_years)


def _quote(
    symbol: str,
    spot: float,
    strike: float,
    expiry: date,
    today: date,
    right: str,
    curve: Curve,
    rv20: float | None,
    spy_rv: float | None,
    iv_model: str,
) -> tuple[float, float, float] | None:
    dte = (expiry - today).days
    if dte < 0:
        return None
    t_years = dte / 365.0
    iv = _iv_at(symbol, dte, spot, strike, t_years, curve, rv20, spy_rv, iv_model)
    if iv is None or iv <= 0:
        return None
    div = DIVIDENDS.get(symbol, 0.0)
    mid = bs_price(spot, strike, t_years, iv, right, curve.rate, div)
    delta = bs_delta(spot, strike, t_years, iv, right, curve.rate, div)
    width = full_width(symbol, mid, delta, dte)
    return mid, delta, width


def _nearest_delta(
    symbol: str,
    spot: float,
    expiry: date,
    today: date,
    right: str,
    target_abs: float,
    curve: Curve,
    rv20: float | None,
    spy_rv: float | None,
    iv_model: str,
) -> float | None:
    step = strike_step(symbol, spot)
    span = min(120, max(25, int(0.45 * spot / step)))
    center = snap_strike(spot, step)
    t_years = max(1, (expiry - today).days) / 365.0
    best_k = None
    best_err = 1e9
    div = DIVIDENDS.get(symbol, 0.0)
    for i in range(-span, span + 1):
        strike = center + i * step
        if strike <= 0:
            continue
        iv = _iv_at(symbol, (expiry - today).days, spot, strike, t_years, curve, rv20, spy_rv, iv_model)
        if iv is None:
            continue
        delta = bs_delta(spot, strike, t_years, iv, right, curve.rate, div)
        err = abs(abs(delta) - target_abs)
        if err < best_err:
            best_err = err
            best_k = strike
    # No listed strike near the target. Taking the edge of the scan is not a fill.
    if best_k is None or best_err > 0.08:
        return None
    return best_k


def _leg_close_proceeds(mid: float, width: float, side: str) -> float:
    """What one leg contributes to liquidation value. Worthless legs are zero."""
    if mid <= 0:
        return 0.0
    if side == "long":
        px = sell_fill(mid, width)
        return 0.0 if px is None else px
    px = buy_fill(mid, width)
    return 0.0 if px is None else -px


def _leg_open_cost(mid: float, width: float, side: str) -> float | None:
    if side == "long":
        px = buy_fill(mid, width)
        return None if px is None else px
    px = sell_fill(mid, width)
    return None if px is None else -px


def position_value(
    pos: Position,
    spot: float,
    today: date,
    curve: Curve,
    rv20: float | None,
    spy_rv: float | None,
    iv_model: str,
) -> float | None:
    long_q = _quote(
        pos.symbol, spot, pos.long_strike, pos.long_expiry, today, pos.right, curve, rv20, spy_rv, iv_model
    )
    short_q = _quote(
        pos.symbol, spot, pos.short_strike, pos.short_expiry, today, pos.right, curve, rv20, spy_rv, iv_model
    )
    if long_q is None or short_q is None:
        return None
    long_part = _leg_close_proceeds(long_q[0], long_q[2], "long")
    short_part = _leg_close_proceeds(short_q[0], short_q[2], "short")
    return long_part + short_part


def _fee(qty: int) -> float:
    return qty * 2 * FEE_PER_CONTRACT_SIDE


def _signal(
    strategy: str,
    params: dict,
    tape: SymbolTape,
    i: int,
    curve: Curve,
) -> dict | None:
    close = tape.closes[i]
    if strategy == "debit_momentum":
        sma = rolling_mean(tape.closes, i, int(params["sma"]))
        mom_n = int(params["mom"])
        if sma is None or i < mom_n or tape.closes[i - mom_n] <= 0:
            return None
        mom = close / tape.closes[i - mom_n] - 1.0
        if close > sma and mom > 0:
            right = "call"
        elif close < sma and mom < 0:
            right = "put"
        else:
            return None
        if params.get("cheap_iv"):
            if curve.vix_pct is None or curve.vix_pct >= 0.50:
                return None
        return {"right": right}
    if strategy == "short_dated":
        lb = int(params["lookback"])
        sma = rolling_mean(tape.closes, i, int(params["sma"]))
        if sma is None or i < lb or tape.closes[i - lb] <= 0:
            return None
        mom = close / tape.closes[i - lb] - 1.0
        thresh = float(params["thresh"])
        if mom >= thresh and close > sma:
            return {"right": "call"}
        if mom <= -thresh and close < sma:
            return {"right": "put"}
        return None
    if strategy == "condor":
        mode = params["iv_mode"]
        if mode == "vix_gt_sma20":
            if curve.vix is None or curve.vix_sma20 is None or curve.vix <= curve.vix_sma20:
                return None
        elif mode == "vix_pct_60":
            if curve.vix_pct is None or curve.vix_pct < 0.60:
                return None
        elif mode == "contango":
            if curve.vix is None or curve.vix3m is None or not (curve.vix3m > curve.vix):
                return None
        else:
            return None
        sma = rolling_mean(tape.closes, i, 20)
        if sma is None or sma <= 0 or abs(close / sma - 1.0) >= 0.02:
            return None
        return {"right": "put"}
    if strategy == "butterfly":
        filt = params["rv_filter"]
        if filt == "rv10_lt_rv60":
            rv10 = realized_vol(tape.closes, i, 10)
            rv60 = realized_vol(tape.closes, i, 60)
            if rv10 is None or rv60 is None or rv10 >= rv60:
                return None
        elif filt == "vix_pct_lt_40":
            if curve.vix_pct is None or curve.vix_pct >= 0.40:
                return None
        elif filt == "near_sma":
            sma = rolling_mean(tape.closes, i, 20)
            if sma is None or sma <= 0 or abs(close / sma - 1.0) >= 0.01:
                return None
        else:
            return None
        return {"right": "call"}
    if strategy == "calendar":
        if curve.vix9d is None or curve.vix3m is None or curve.vix3m <= 0:
            return None
        if curve.vix9d / curve.vix3m < float(params["ratio"]):
            return None
        sma = rolling_mean(tape.closes, i, 20)
        if sma is None or sma <= 0 or abs(close / sma - 1.0) > float(params["band"]):
            return None
        return {"right": "put"}
    if strategy == "diagonal":
        sma_n = int(params["sma"])
        sma = rolling_mean(tape.closes, i, sma_n)
        if sma is None or i < 20 or tape.closes[i - 20] <= 0:
            return None
        mom = close / tape.closes[i - 20] - 1.0
        if close > sma and mom > 0:
            return {"right": "call"}
        if close < sma and mom < 0:
            return {"right": "put"}
        return None
    return None


def _dte_window(strategy: str, params: dict) -> tuple[int, int, int]:
    if strategy == "debit_momentum":
        dte = int(params["dte"])
        return dte, max(14, dte - 10), dte + 14
    if strategy == "short_dated":
        # Daily bars cannot mark a 0 DTE path. The short-dated book is 2-9 DTE.
        return int(params["dte"]), 2, 9
    if strategy == "condor":
        dte = int(params["dte"])
        return dte, max(21, dte - 10), dte + 14
    if strategy == "butterfly":
        dte = int(params["dte"])
        return dte, max(14, dte - 7), dte + 14
    return 30, 21, 45


def _build_from_intent(
    intent: Intent,
    spot: float,
    today: date,
    curve: Curve,
    rv20: float | None,
    spy_rv: float | None,
    iv_model: str,
) -> dict | None:
    symbol = intent.symbol
    strategy = intent.strategy
    params = intent.params
    step = strike_step(symbol, spot)
    div_unused = DIVIDENDS.get(symbol, 0.0)
    del div_unused

    if strategy in {"debit_momentum", "short_dated"}:
        target, min_dte, max_dte = _dte_window(strategy, params)
        expiry = _pick_friday(today, target, min_dte, max_dte)
        if expiry is None or intent.right is None:
            return None
        k_long = _nearest_delta(
            symbol, spot, expiry, today, intent.right, float(params["long_delta"]), curve, rv20, spy_rv, iv_model
        )
        k_short = _nearest_delta(
            symbol, spot, expiry, today, intent.right, float(params["short_delta"]), curve, rv20, spy_rv, iv_model
        )
        if k_long is None or k_short is None:
            return None
        if intent.right == "call" and k_short <= k_long:
            k_short = k_long + step
        if intent.right == "put" and k_short >= k_long:
            k_short = k_long - step
        built = _vertical_prices(
            symbol, spot, today, intent.right, k_long, k_short, expiry, expiry, curve, rv20, spy_rv, iv_model, "debit"
        )
        if built is None:
            return None
        built["role"] = "debit"
        built["adverse"] = "down" if intent.right == "call" else "up"
        built["exit_dte"] = EXIT_DTE[strategy]
        return built

    if strategy == "condor" and intent.wing == 1:
        target, min_dte, max_dte = _dte_window(strategy, params)
        expiry = _pick_friday(today, target, min_dte, max_dte)
        if expiry is None:
            return None
        right = "put"
        k_short = _nearest_delta(
            symbol, spot, expiry, today, right, float(params["short_delta"]), curve, rv20, spy_rv, iv_model
        )
        if k_short is None:
            return None
        k_long = snap_strike(k_short - float(params["width"]), step)
        if k_long >= k_short:
            k_long = k_short - step
        built = _vertical_prices(
            symbol, spot, today, right, k_long, k_short, expiry, expiry, curve, rv20, spy_rv, iv_model, "credit"
        )
        if built is None:
            return None
        built["role"] = "put_wing"
        built["adverse"] = "down"
        built["exit_dte"] = EXIT_DTE[strategy]
        return built

    if strategy == "condor" and intent.wing == 2:
        # Same expiry as the open put wing is stored on the intent via params copy.
        expiry = intent.params.get("_wing_expiry")
        if not isinstance(expiry, date):
            return None
        right = "call"
        k_short = _nearest_delta(
            symbol, spot, expiry, today, right, float(params["short_delta"]), curve, rv20, spy_rv, iv_model
        )
        if k_short is None:
            return None
        k_long = snap_strike(k_short + float(params["width"]), step)
        if k_long <= k_short:
            k_long = k_short + step
        if k_short <= spot:
            return None
        built = _vertical_prices(
            symbol, spot, today, right, k_long, k_short, expiry, expiry, curve, rv20, spy_rv, iv_model, "credit"
        )
        if built is None:
            return None
        built["role"] = "call_wing"
        built["adverse"] = "up"
        built["exit_dte"] = EXIT_DTE[strategy]
        return built

    if strategy == "butterfly" and intent.wing == 1:
        target, min_dte, max_dte = _dte_window(strategy, params)
        expiry = _pick_friday(today, target, min_dte, max_dte)
        if expiry is None:
            return None
        width = float(params["width"])
        # Body at the rounded spot. Wings are one width below and above.
        k2 = snap_strike(spot, step)
        k1 = k2 - width
        k3 = k2 + width
        built = _vertical_prices(
            symbol, spot, today, "call", k1, k2, expiry, expiry, curve, rv20, spy_rv, iv_model, "debit"
        )
        if built is None:
            return None
        built["role"] = "fly_debit"
        built["adverse"] = "down"
        built["exit_dte"] = EXIT_DTE[strategy]
        built["body"] = (k1, k2, k3, expiry)
        return built

    if strategy == "butterfly" and intent.wing == 2:
        body = intent.body
        if body is None:
            return None
        k1, k2, k3 = body
        expiry = intent.params.get("_wing_expiry")
        if not isinstance(expiry, date):
            return None
        # Locked strikes from the first fill. A gap through the far wing skips.
        if spot >= k3 or spot <= k1:
            return None
        built = _vertical_prices(
            symbol, spot, today, "call", k3, k2, expiry, expiry, curve, rv20, spy_rv, iv_model, "credit"
        )
        if built is None:
            return None
        built["role"] = "fly_credit"
        built["adverse"] = "up"
        built["exit_dte"] = EXIT_DTE[strategy]
        return built

    if strategy == "calendar":
        front = _pick_friday(today, int(params["front_dte"]), max(4, int(params["front_dte"]) - 6), int(params["front_dte"]) + 8)
        back = _pick_friday(today, int(params["back_dte"]), int(params["front_dte"]) + 14, int(params["back_dte"]) + 21)
        if front is None or back is None or back <= front:
            return None
        strike = snap_strike(spot, step)
        built = _vertical_prices(
            symbol, spot, today, "put", strike, strike, back, front, curve, rv20, spy_rv, iv_model, "debit"
        )
        if built is None:
            return None
        # _vertical_prices uses width = |strikes|. Same strike needs its own max-loss.
        built["width"] = built["cost"]
        built["max_loss_ps"] = built["cost"]
        built["target_value"] = built["cost"] * 1.25
        built["stop_value"] = built["cost"] * 0.50
        built["role"] = "calendar"
        built["adverse"] = "either"
        built["exit_dte"] = EXIT_DTE[strategy]
        built["front_expiry"] = front
        return built

    if strategy == "diagonal":
        front = _pick_friday(today, int(params["front_dte"]), max(4, int(params["front_dte"]) - 5), int(params["front_dte"]) + 7)
        back = _pick_friday(today, int(params["back_dte"]), int(params["front_dte"]) + 14, int(params["back_dte"]) + 21)
        if front is None or back is None or back <= front or intent.right is None:
            return None
        k_long = _nearest_delta(
            symbol, spot, back, today, intent.right, float(params["back_delta"]), curve, rv20, spy_rv, iv_model
        )
        k_short = _nearest_delta(
            symbol, spot, front, today, intent.right, float(params["front_delta"]), curve, rv20, spy_rv, iv_model
        )
        if k_long is None or k_short is None:
            return None
        if intent.right == "call" and k_short <= k_long:
            k_short = k_long + step
        if intent.right == "put" and k_short >= k_long:
            k_short = k_long - step
        built = _vertical_prices(
            symbol,
            spot,
            today,
            intent.right,
            k_long,
            k_short,
            back,
            front,
            curve,
            rv20,
            spy_rv,
            iv_model,
            "debit",
            debit_width_cap=False,
        )
        if built is None:
            return None
        built["width"] = built["cost"]
        built["max_loss_ps"] = built["cost"]
        built["target_value"] = built["cost"] * 1.50
        built["stop_value"] = built["cost"] * 0.50
        built["role"] = "diagonal"
        built["adverse"] = "either"
        built["exit_dte"] = EXIT_DTE[strategy]
        built["front_expiry"] = front
        return built
    return None


def _vertical_prices(
    symbol: str,
    spot: float,
    today: date,
    right: str,
    k_long: float,
    k_short: float,
    long_expiry: date,
    short_expiry: date,
    curve: Curve,
    rv20: float | None,
    spy_rv: float | None,
    iv_model: str,
    kind: str,
    *,
    debit_width_cap: bool = True,
) -> dict | None:
    long_q = _quote(symbol, spot, k_long, long_expiry, today, right, curve, rv20, spy_rv, iv_model)
    short_q = _quote(symbol, spot, k_short, short_expiry, today, right, curve, rv20, spy_rv, iv_model)
    if long_q is None or short_q is None:
        return None
    long_cost = _leg_open_cost(long_q[0], long_q[2], "long")
    short_cost = _leg_open_cost(short_q[0], short_q[2], "short")
    if long_cost is None or short_cost is None:
        return None
    cost = long_cost + short_cost
    width = abs(k_short - k_long)
    if kind == "debit":
        if width <= 0 and long_expiry == short_expiry:
            return None
        if cost <= 0.10:
            return None
        # A vertical's max value is its width. A diagonal's long leg is not
        # capped by the strike gap, so that gate does not apply there.
        if debit_width_cap and width > 0 and cost >= 0.85 * width:
            return None
        max_loss_ps = cost
        target = cost + 0.50 * (width - cost) if width > 0 else cost * 1.5
        stop = 0.50 * cost
    else:
        if width <= 0 or cost >= 0:
            return None
        credit = -cost
        if credit < max(0.20, 0.08 * width):
            return None
        max_loss_ps = width - credit
        if max_loss_ps <= 0.05:
            return None
        target = 0.50 * cost
        stop = 2.0 * cost
    return {
        "right": right,
        "long_strike": k_long,
        "short_strike": k_short,
        "long_expiry": long_expiry,
        "short_expiry": short_expiry,
        "front_expiry": short_expiry,
        "cost": cost,
        "width": width,
        "max_loss_ps": max_loss_ps,
        "target_value": target,
        "stop_value": stop,
        "kind": kind,
    }


def _busy_symbols(positions: list[Position], pending: list[Intent], groups: dict[int, Group]) -> set[str]:
    names = {p.symbol for p in positions}
    names.update(intent.symbol for intent in pending)
    names.update(g.symbol for g in groups.values() if g.stage == "need_second")
    return names


def _open_risk(positions: list[Position], groups: dict[int, Group]) -> float:
    return sum(p.risk_dollars for p in positions) + sum(
        g.reserve_dollars for g in groups.values() if g.reserved
    )


def _spread_count(positions: list[Position], groups: dict[int, Group]) -> int:
    return len(positions) + sum(1 for g in groups.values() if g.reserved)


def _rv(prep: Prepared, symbol: str, day: date) -> float | None:
    tape = prep.symbols[symbol]
    i = tape.index.get(day)
    if i is None:
        return None
    return realized_vol(tape.closes, i, 20)


def _spy_rv_on(prep: Prepared, day: date) -> float | None:
    i = prep.symbols["SPY"].index.get(day)
    if i is None:
        return None
    return prep.spy_rv20[i]


def _try_exit_one(
    pos: Position,
    tape: SymbolTape,
    i: int,
    today: date,
    curve: Curve,
    rv20: float | None,
    spy_rv: float | None,
    iv_model: str,
) -> tuple[str, float] | None:
    def value_at(spot: float) -> float | None:
        return position_value(pos, spot, today, curve, rv20, spy_rv, iv_model)

    v_open = value_at(tape.opens[i])
    v_high = value_at(tape.highs[i])
    v_low = value_at(tape.lows[i])
    v_close = value_at(tape.closes[i])
    if None in (v_open, v_high, v_low, v_close):
        return None
    assert v_open is not None and v_high is not None and v_low is not None and v_close is not None
    if pos.adverse == "down":
        adverse, favorable = v_low, v_high
    elif pos.adverse == "up":
        adverse, favorable = v_high, v_low
    else:
        adverse, favorable = min(v_low, v_high), max(v_low, v_high)
    if not pos.entry_bar:
        if v_open <= pos.stop_value:
            return ("stop_gap", v_open)
        if v_open >= pos.target_value:
            return ("target", pos.target_value)
    if adverse <= pos.stop_value:
        return ("stop", pos.stop_value)
    if favorable >= pos.target_value:
        return ("target", pos.target_value)
    if (pos.front_expiry if False else pos.short_expiry) and (pos.short_expiry - today).days <= pos.exit_dte:
        return ("time", v_close)
    return None


def _close_position(
    pos: Position,
    reason: str,
    value: float,
    today: date,
    trades: list[dict],
    *,
    completed: bool,
) -> float:
    pnl = (value - pos.cost) * MULTIPLIER * pos.qty - pos.entry_fee - _fee(pos.qty)
    r = pnl / pos.risk_dollars if pos.risk_dollars else 0.0
    trades.append(
        {
            "symbol": pos.symbol,
            "strategy": pos.strategy,
            "params_id": pos.params_id,
            "role": pos.role,
            "group_id": pos.group_id,
            "entry_date": pos.entry_date,
            "exit_date": today,
            "signal_date": pos.signal_date,
            "pnl": pnl,
            "r": r,
            "reason": reason,
            "qty": pos.qty,
            "risk_dollars": pos.risk_dollars,
            "cost": pos.cost,
            "exit_value": value,
            "completed_structure": completed,
            "right": pos.right,
            "long_strike": pos.long_strike,
            "short_strike": pos.short_strike,
        }
    )
    return pnl


def _group_levels(group: Group, members: list[Position]) -> tuple[float, float] | None:
    if len(members) < 2:
        return None
    net = members[0].cost + members[1].cost
    if group.strategy == "condor":
        return 0.5 * net, 2.0 * net
    width = float(group.params.get("width", 0.0))
    if net > 0 and width > 0:
        return net + 0.5 * max(0.0, width - net), 0.5 * net
    return 0.5 * net, 2.0 * net


def simulate(
    prep: Prepared,
    strategy: str,
    params: dict,
    start: date,
    end: date,
    *,
    iv_model: str = "primary",
    schedule: list[tuple[date, date, dict]] | None = None,
    account: float = ACCOUNT,
) -> dict:
    """Flat-start account. ``schedule`` swaps entry parameters by signal date.

    Open positions keep the exit rules they were opened with.
    """
    cal = prep.calendar
    positions: list[Position] = []
    pending: list[Intent] = []
    groups: dict[int, Group] = {}
    trades: list[dict] = []
    exposure: list[dict] = []
    equity_curve: list[float] = []
    realized = account
    next_id = 1
    next_gid = 1
    pid = params_id(strategy, params)

    def params_on(day: date) -> dict | None:
        if schedule is None:
            if start <= day <= end:
                return params
            return None
        for a, b, chosen in schedule:
            if a <= day <= b:
                return chosen
        return None

    def prior_curve(i: int) -> Curve | None:
        if i - 1 < 0:
            return None
        return prep.curves[i - 1]

    def mtm(today_i: int, curve: Curve) -> float:
        today = cal[today_i]
        unreal = 0.0
        for pos in positions:
            tape = prep.symbols[pos.symbol]
            j = tape.index.get(today)
            if j is None:
                continue
            rv20 = realized_vol(tape.closes, max(0, j - 1), 20) if j else None
            # RV for the mark uses the prior close index on that symbol.
            prev_day = cal[today_i - 1] if today_i else today
            rv20 = _rv(prep, pos.symbol, prev_day)
            spy_rv = _spy_rv_on(prep, prev_day)
            value = position_value(pos, tape.closes[j], today, curve, rv20, spy_rv, iv_model)
            if value is None:
                value = pos.cost
            unreal += (value - pos.cost) * MULTIPLIER * pos.qty - pos.entry_fee - _fee(pos.qty)
        return realized + unreal

    indexes = [i for i, day in enumerate(cal) if start <= day <= end]
    if not indexes:
        return _empty_result(strategy, pid, start, end)

    for i in indexes:
        today = cal[i]
        curve = prior_curve(i)
        if curve is None:
            continue
        prev_day = cal[i - 1]
        closed: set[int] = set()
        positions, realized = _exit_singles(
            positions, groups, prep, i, today, curve, prev_day, iv_model, trades, realized, entry_only=False
        )
        realized_box = [realized]
        _apply_group_exits(
            positions, groups, prep, i, today, curve, prev_day, iv_model, trades, realized_box, closed, False
        )
        realized = realized_box[0]
        positions = [p for p in positions if p.pid not in closed]
        pending = [
            item
            for item in pending
            if item.wing != 2
            or (item.group_id is not None and groups.get(item.group_id) and groups[item.group_id].stage == "need_second")
        ]

        # Fills at the open, alphabetical. Wing-2 intents created today wait until tomorrow.
        day_pending = list(pending)
        pending = []
        for intent in sorted(day_pending, key=lambda item: (item.symbol, item.wing)):
            filled = _fill_intent(
                intent,
                prep,
                i,
                today,
                curve,
                prev_day,
                iv_model,
                positions,
                groups,
                realized,
                realized if realized > 1000 else 1000.0,
            )
            if filled is None:
                _drop_failed_wing(intent, groups)
                continue
            pos, follow = filled
            pos.pid = next_id
            next_id += 1
            if follow is not None:
                gid = next_gid
                next_gid += 1
                pos.group_id = gid
                follow.group_id = gid
                groups[gid] = Group(
                    gid=gid,
                    symbol=pos.symbol,
                    strategy=pos.strategy,
                    params_id=pos.params_id,
                    params=_public_params(pos.params),
                    signal_date=pos.signal_date,
                    signal_close=follow.signal_close,
                    atr=follow.atr,
                    stage="need_second",
                    reserved=True,
                    reserve_dollars=float(follow.params.get("_reserve") or 0.0),
                    body=follow.body,
                    put_short=pos.short_strike if pos.role == "put_wing" else None,
                )
                pending.append(follow)
            elif intent.wing == 2 and intent.group_id is not None and intent.group_id in groups:
                group = groups[intent.group_id]
                group.stage = "complete"
                group.reserved = False
                group.reserve_dollars = 0.0
                pos.group_id = intent.group_id
                if pos.role == "call_wing":
                    group.call_short = pos.short_strike
            positions.append(pos)

        closed = set()
        positions, realized = _exit_singles(
            positions, groups, prep, i, today, curve, prev_day, iv_model, trades, realized, entry_only=True
        )
        realized_box = [realized]
        _apply_group_exits(
            positions, groups, prep, i, today, curve, prev_day, iv_model, trades, realized_box, closed, True
        )
        realized = realized_box[0]
        positions = [p for p in positions if p.pid not in closed]
        pending = [
            item
            for item in pending
            if item.wing != 2
            or (item.group_id is not None and groups.get(item.group_id) and groups[item.group_id].stage == "need_second")
        ]

        # 4. New signals at the close. IV for the decision is today's close.
        close_curve = prep.curves[i]
        chosen = params_on(today)
        next_day = cal[i + 1] if i + 1 < len(cal) else None
        if chosen is not None and next_day is not None and next_day <= end:
            busy = _busy_symbols(positions, pending, groups)
            for symbol in UNIVERSE[strategy]:
                if symbol in busy:
                    continue
                tape = prep.symbols.get(symbol)
                if tape is None or today not in tape.index:
                    continue
                j = tape.index[today]
                sig = _signal(strategy, chosen, tape, j, close_curve)
                if sig is None:
                    continue
                atr = _atr(tape, j) or 0.0
                pending.append(
                    Intent(
                        symbol=symbol,
                        strategy=strategy,
                        params_id=params_id(strategy, _public_params(chosen)),
                        params=dict(chosen),
                        kind=strategy,
                        signal_date=today,
                        signal_close=tape.closes[j],
                        atr=atr,
                        right=sig.get("right"),
                        wing=1,
                    )
                )
                busy.add(symbol)

        for pos in positions:
            pos.entry_bar = False
        equity_curve.append(mtm(i, curve))
        exposure.append(
            {
                "date": today,
                "spreads": _spread_count(positions, groups),
                "open_risk": _open_risk(positions, groups),
                "gross": _open_risk(positions, groups),
            }
        )

    # Mark anything still open on the last in-window close. One look at that close.
    if indexes and positions:
        last_i = indexes[-1]
        last_day = cal[last_i]
        curve = prior_curve(last_i)
        if curve is not None:
            prev_day = cal[last_i - 1]
            for pos in list(positions):
                tape = prep.symbols[pos.symbol]
                j = tape.index.get(last_day)
                if j is None:
                    continue
                value = position_value(
                    pos, tape.closes[j], last_day, curve, _rv(prep, pos.symbol, prev_day), _spy_rv_on(prep, prev_day), iv_model
                )
                if value is None:
                    value = pos.cost
                completed = bool(pos.group_id and groups.get(pos.group_id) and groups[pos.group_id].stage == "complete")
                realized += _close_position(pos, "window_mtm", value, last_day, trades, completed=completed)
            positions = []
            equity_curve.append(realized)

    orphans = [t for t in trades if t["strategy"] in {"condor", "butterfly"} and not t["completed_structure"]]
    structures = [t for t in trades if t["strategy"] in {"condor", "butterfly"}]
    return {
        "strategy": strategy,
        "params_id": pid,
        "trades": trades,
        "equity": equity_curve,
        "exposure": exposure,
        "realized": realized,
        "orphan_trades": len(orphans),
        "structure_trades": len(structures),
    }


def _public_params(params: dict) -> dict:
    return {k: v for k, v in params.items() if not str(k).startswith("_")}


def _exit_singles(
    positions: list[Position],
    groups: dict[int, Group],
    prep: Prepared,
    i: int,
    today: date,
    curve: Curve,
    prev_day: date,
    iv_model: str,
    trades: list[dict],
    realized: float,
    *,
    entry_only: bool,
) -> tuple[list[Position], float]:
    """Individual exits. Complete packages are left for the package rules."""
    still: list[Position] = []
    for pos in positions:
        if entry_only and not pos.entry_bar:
            still.append(pos)
            continue
        group = groups.get(pos.group_id) if pos.group_id is not None else None
        if group is not None and group.stage == "complete":
            still.append(pos)
            continue
        tape = prep.symbols[pos.symbol]
        j = tape.index.get(today)
        if j is None:
            still.append(pos)
            continue
        outcome = _try_exit_one(
            pos, tape, j, today, curve, _rv(prep, pos.symbol, prev_day), _spy_rv_on(prep, prev_day), iv_model
        )
        if outcome is None:
            still.append(pos)
            continue
        reason, value = outcome
        realized += _close_position(pos, reason, value, today, trades, completed=False)
        _release_group_member(groups, pos)
    return still, realized


def _release_group_member(groups: dict[int, Group], pos: Position) -> None:
    if pos.group_id is None or pos.group_id not in groups:
        return
    group = groups[pos.group_id]
    if group.stage == "need_second":
        group.reserved = False
        group.reserve_dollars = 0.0
        group.stage = "orphan"


def account_equity(
    realized: float,
    positions: list[Position],
    prep: Prepared,
    i: int,
    today: date,
    curve: Curve,
    prev_day: date,
    iv_model: str,
) -> float:
    unreal = 0.0
    for pos in positions:
        tape = prep.symbols[pos.symbol]
        j = tape.index.get(today)
        if j is None:
            continue
        value = position_value(
            pos, tape.opens[j], today, curve, _rv(prep, pos.symbol, prev_day), _spy_rv_on(prep, prev_day), iv_model
        )
        if value is None:
            continue
        unreal += (value - pos.cost) * MULTIPLIER * pos.qty - pos.entry_fee - _fee(pos.qty)
    equity = realized + unreal
    return equity if equity > 1000 else 1000.0


def _fill_intent(
    intent: Intent,
    prep: Prepared,
    i: int,
    today: date,
    curve: Curve,
    prev_day: date,
    iv_model: str,
    positions: list[Position],
    groups: dict[int, Group],
    realized: float,
    sizing_equity: float,
) -> tuple[Position, Intent | None] | None:
    tape = prep.symbols[intent.symbol]
    j = tape.index.get(today)
    if j is None:
        return None
    if intent.wing == 2 and intent.atr > 0:
        if abs(tape.opens[j] - intent.signal_close) > 1.5 * intent.atr:
            return None
    rv20 = _rv(prep, intent.symbol, prev_day)
    spy_rv = _spy_rv_on(prep, prev_day)
    built = _build_from_intent(intent, tape.opens[j], today, curve, rv20, spy_rv, iv_model)
    if built is None:
        return None
    if intent.wing == 2 and intent.group_id is not None and intent.group_id in groups:
        # The reserve was this wing's budget. Free it before sizing the fill.
        groups[intent.group_id].reserved = False
        groups[intent.group_id].reserve_dollars = 0.0
    risk_now = _open_risk(positions, groups)
    spreads = _spread_count(positions, groups)
    if spreads >= MAX_SPREADS:
        return None
    room_risk = MAX_OPEN_RISK_PCT * sizing_equity - risk_now
    room_gross = MAX_GROSS - risk_now
    per = built["max_loss_ps"] * MULTIPLIER
    if per <= 0:
        return None
    qty = int((sizing_equity * RISK_PCT) // per)
    qty = min(
        qty,
        MAX_CONTRACTS,
        int(room_risk // per) if per else 0,
        int(room_gross // per) if per else 0,
    )
    if qty < 1:
        return None
    # One structure per symbol. The paired wing is the only same-symbol add.
    if any(p.symbol == intent.symbol for p in positions) and intent.wing == 1:
        return None
    risk_dollars = per * qty
    pos = Position(
        pid=0,
        symbol=intent.symbol,
        strategy=intent.strategy,
        params_id=intent.params_id,
        group_id=intent.group_id,
        role=built["role"],
        kind=built["kind"],
        right=built["right"],
        adverse=built["adverse"],
        long_strike=built["long_strike"],
        short_strike=built["short_strike"],
        long_expiry=built["long_expiry"],
        short_expiry=built["short_expiry"],
        qty=qty,
        cost=built["cost"],
        max_loss_ps=built["max_loss_ps"],
        risk_dollars=risk_dollars,
        target_value=built["target_value"],
        stop_value=built["stop_value"],
        exit_dte=built["exit_dte"],
        entry_date=today,
        signal_date=intent.signal_date,
        entry_fee=_fee(qty),
        entry_bar=True,
        params=dict(intent.params),
    )
    # Front expiry for calendars/diagonals is the short leg.
    follow: Intent | None = None
    if intent.strategy in {"condor", "butterfly"} and intent.wing == 1:
        reserve = sizing_equity * RISK_PCT
        spreads_after = spreads + 1
        risk_after = risk_now + risk_dollars
        can_reserve = (
            spreads_after + 1 <= MAX_SPREADS
            and risk_after + reserve <= MAX_OPEN_RISK_PCT * sizing_equity
            and risk_after + reserve <= MAX_GROSS
        )
        gid = intent.group_id if intent.group_id is not None else -1
        # gid assigned by caller if -1. Use a placeholder group created here
        # only when we can reserve; the caller overwrites pid/gid.
        if can_reserve:
            body = built.get("body")
            expiry = built["short_expiry"]
            follow_params = dict(intent.params)
            follow_params["_wing_expiry"] = expiry
            follow = Intent(
                symbol=intent.symbol,
                strategy=intent.strategy,
                params_id=intent.params_id,
                params=follow_params,
                kind=intent.strategy,
                signal_date=intent.signal_date,
                signal_close=intent.signal_close,
                atr=intent.atr,
                right="call",
                group_id=None,
                wing=2,
                body=body[:3] if body else None,
            )
            # Group is created by the caller once gid is known. Stash on follow.
            follow.params["_reserve"] = reserve
            follow.params["_body_expiry"] = expiry
            if body:
                follow.params["_k1"], follow.params["_k2"], follow.params["_k3"] = body[0], body[1], body[2]
    return pos, follow


def _drop_failed_wing(intent: Intent, groups: dict[int, Group]) -> None:
    if intent.wing == 2 and intent.group_id is not None and intent.group_id in groups:
        group = groups[intent.group_id]
        group.reserved = False
        group.reserve_dollars = 0.0
        group.stage = "orphan"


def _apply_group_exits(
    positions: list[Position],
    groups: dict[int, Group],
    prep: Prepared,
    i: int,
    today: date,
    curve: Curve,
    prev_day: date,
    iv_model: str,
    trades: list[dict],
    realized_box: list[float],
    closed: set[int],
    require_entry_bar: bool,
) -> None:
    """Close both wings together when the package stop, target, or short strike is hit.

    Individual wing exits are handled by the caller only when the group is
    not complete. A complete package is marked here so one wing cannot
    take profit while the other wing is left on by accident inside the
    same bar: the package rules run first, and if they do not fire each
    wing is then eligible on a later pass. This function only fires
    package-level exits.
    """
    by_group: dict[int, list[Position]] = {}
    for pos in positions:
        if pos.group_id is None:
            continue
        group = groups.get(pos.group_id)
        if group is None or group.stage != "complete":
            continue
        by_group.setdefault(pos.group_id, []).append(pos)
    for gid, members in by_group.items():
        if len(members) < 2:
            continue
        if require_entry_bar and not any(pos.entry_bar for pos in members):
            continue
        group = groups[gid]
        levels = _group_levels(group, members)
        if levels is None:
            continue
        target, stop = levels
        tape = prep.symbols[members[0].symbol]
        j = tape.index.get(today)
        if j is None:
            continue
        rv20 = _rv(prep, members[0].symbol, prev_day)
        spy_rv = _spy_rv_on(prep, prev_day)

        def combo(spot: float) -> float | None:
            total = 0.0
            for pos in members:
                value = position_value(pos, spot, today, curve, rv20, spy_rv, iv_model)
                if value is None:
                    return None
                total += value
            return total

        v_open = combo(tape.opens[j])
        v_high = combo(tape.highs[j])
        v_low = combo(tape.lows[j])
        v_close = combo(tape.closes[j])
        if None in (v_open, v_high, v_low, v_close):
            continue
        assert v_open is not None and v_high is not None and v_low is not None and v_close is not None
        reason = None
        fill_value = None
        touched = False
        if group.strategy == "condor" and group.put_short and group.call_short:
            if tape.opens[j] <= group.put_short or tape.opens[j] >= group.call_short:
                touched = True
                reason, fill_value = "touch_gap", v_open
            elif tape.lows[j] <= group.put_short or tape.highs[j] >= group.call_short:
                touched = True
                spot = group.put_short if tape.lows[j] <= group.put_short else group.call_short
                if tape.lows[j] <= (group.put_short or 0) and tape.highs[j] >= (group.call_short or 1e18):
                    v_put = combo(group.put_short)
                    v_call = combo(group.call_short)
                    fill_value = v_put if v_call is None or (v_put is not None and v_put < v_call) else v_call
                else:
                    fill_value = combo(spot)
                reason = "touch"
        if group.strategy == "butterfly" and group.body:
            k1, k2, k3 = group.body
            if tape.opens[j] <= k1 or tape.opens[j] >= k3:
                touched = True
                reason, fill_value = "touch_gap", v_open
            elif tape.lows[j] <= k1 or tape.highs[j] >= k3:
                touched = True
                reason = "touch"
                fill_value = min(v_low, v_high)
        if not touched:
            if v_open <= stop:
                reason, fill_value = "stop_gap", v_open
            elif v_open >= target:
                reason, fill_value = "target", target
            elif min(v_low, v_high) <= stop:
                reason, fill_value = "stop", stop
            elif max(v_low, v_high) >= target:
                reason, fill_value = "target", target
            elif (members[0].short_expiry - today).days <= members[0].exit_dte:
                reason, fill_value = "time", v_close
        if reason is None or fill_value is None:
            continue
        # Split the package fill across wings in proportion to each wing's
        # own value at the same spot when we have it; otherwise use the
        # package value times each wing's share of the open value.
        shares = []
        for pos in members:
            own = position_value(pos, tape.opens[j], today, curve, rv20, spy_rv, iv_model)
            shares.append(0.0 if own is None else own)
        base = sum(shares)
        for pos, own in zip(members, shares):
            if reason in {"target", "stop"} and base != 0:
                # The limit is on the package. Attribute by the open share
                # so each leg keeps its own cost basis. This is an
                # accounting split, not a better fill.
                part = fill_value * (own / base)
            elif reason in {"target", "stop"}:
                part = fill_value / 2.0
            else:
                own_spot = tape.opens[j]
                if reason == "touch" and group.put_short and tape.lows[j] <= group.put_short:
                    own_spot = group.put_short
                valued = position_value(pos, own_spot, today, curve, rv20, spy_rv, iv_model)
                part = own if valued is None else valued
                if reason == "time":
                    valued_c = position_value(pos, tape.closes[j], today, curve, rv20, spy_rv, iv_model)
                    part = own if valued_c is None else valued_c
            realized_box[0] += _close_position(pos, reason, part, today, trades, completed=True)
            closed.add(pos.pid)
        group.stage = "done"


def _empty_result(strategy: str, pid: str, start: date, end: date) -> dict:
    return {
        "strategy": strategy,
        "params_id": pid,
        "trades": [],
        "equity": [],
        "exposure": [],
        "realized": ACCOUNT,
        "orphan_trades": 0,
        "structure_trades": 0,
    }


def _install_group_on_fill(
    pos: Position,
    follow: Intent | None,
    groups: dict[int, Group],
    gid: int,
) -> None:
    """Caller helper kept for tests that build a group explicitly."""
    if follow is None:
        return
    reserve = float(follow.params.get("_reserve", 0.0))
    body = follow.body
    groups[gid] = Group(
        gid=gid,
        symbol=pos.symbol,
        strategy=pos.strategy,
        params_id=pos.params_id,
        params=_public_params(pos.params),
        signal_date=pos.signal_date,
        signal_close=follow.signal_close,
        atr=follow.atr,
        stage="need_second",
        reserved=True,
        reserve_dollars=reserve,
        body=body,
        put_short=pos.short_strike if pos.role == "put_wing" else None,
        call_short=None,
    )
    pos.group_id = gid
    follow.group_id = gid
