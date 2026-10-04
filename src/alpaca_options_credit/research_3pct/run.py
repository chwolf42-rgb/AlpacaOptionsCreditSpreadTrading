"""Run the frozen study and write docs/research/options-3pct-redesign.md.

    PYTHONPATH=src python3 -m alpaca_options_credit.research_3pct.run \
        --cache var/replay-cache --out docs/research/options-3pct-redesign.md
"""

from __future__ import annotations

import argparse
import pickle
import traceback
from dataclasses import replace
from datetime import date
from pathlib import Path

from alpaca_options_credit.config import load_config
from alpaca_options_credit.replay.data import ensure_universe
from alpaca_options_credit.replay.engine import replay_universe
from alpaca_options_credit.replay.credit import implied_vol
from alpaca_options_credit.replay.regime import (
    RegimeSnap,
    _rv_series,
    percentile_and_rank,
)
from alpaca_options_credit.replay.study import structure_from_config
from alpaca_options_credit.research_3pct.accounting import (
    BookReport,
    build_book,
    chosen_name,
    in_window,
    report_book,
    train_row,
)
from alpaca_options_credit.research_3pct.assignment import first_assignment
from alpaca_options_credit.research_3pct.candidates import allow_candidate, candidates
from alpaca_options_credit.research_3pct.chains import collect
from alpaca_options_credit.research_3pct.protocol import (
    FOLDS,
    TEST_END,
    TEST_START,
    TRAIN_END,
    TRAIN_START,
)
from alpaca_options_credit.research_3pct.report import _ci, _pct, _usd, render
from alpaca_options_credit.research_3pct.surface import summarize_surface, surface_grid
from alpaca_options_credit.rth import as_et


def iv_regime(daily_by_symbol: dict) -> dict:
    """IV-proxy percentile without the redesign helper's per-day ATR scan.

    The confirm entry reads ``iv_pct`` off the snap. ATR is only used by the
    extension entry, which this study does not run.
    """
    out = {}
    for symbol, bars in daily_by_symbol.items():
        closes = [bar.close for bar in bars]
        rv20 = _rv_series(closes, 20)
        rv60 = _rv_series(closes, 60)
        ivs = [None if rv is None else implied_vol(rv) for rv in rv20]
        pct, rank = percentile_and_rank(ivs, 252)
        snaps = {}
        for i, bar in enumerate(bars):
            day = as_et(bar.ts).date()
            snaps[day] = RegimeSnap(
                iv_pct=pct[i],
                iv_rank=rank[i],
                rv20=rv20[i],
                rv60=rv60[i],
                iv=ivs[i],
                vix=None,
                vix_pct=None,
                spy_rv20=None,
                ema50=None,
                atr=None,
                close=bar.close,
            )
        out[symbol] = snaps
    return out


def _msft_spot(daily) -> dict | None:
    chosen = None
    for bar in daily:
        day = as_et(bar.ts).date()
        if day <= date(2026, 9, 30):
            chosen = (day, bar.close)
    if chosen is None:
        return None
    day, close = chosen
    if close <= 0:
        return None
    return {"day": day.isoformat(), "close": close, "otm_pct": (close - 490.0) / close * 100.0}


def _tape_note(bars: dict) -> str:
    hourly = (bars.get("SPY") or {}).get("1h") or []
    if not hourly:
        return "SPY hourly bars were missing."
    first = as_et(hourly[0].ts).date()
    last = as_et(hourly[-1].ts).date()
    return (
        f"SPY hourly bars in this run run from {first.isoformat()} through {last.isoformat()}. "
        f"Entries before {TRAIN_START.isoformat()} are warmup. Entries after {TEST_END.isoformat()} are excluded."
    )


def _apply_assignment(trades, daily_by_symbol) -> tuple[list, int]:
    changed = 0
    out = []
    for trade in trades:
        if trade.expiration is None or trade.long_strike <= 0:
            out.append(trade)
            continue
        right = "put" if trade.side == "bullish" else "call"
        hit = first_assignment(
            entry=trade.entry_time,
            exit_at=trade.exit_time,
            expiration=trade.expiration,
            short_k=trade.short_strike,
            long_k=trade.long_strike,
            iv=trade.iv,
            right=right,
            daily=daily_by_symbol.get(trade.symbol) or [],
        )
        if hit is None:
            out.append(trade)
            continue
        when, debit = hit
        changed += 1
        pnl = (trade.credit - debit) * trade.qty * 100
        out.append(
            replace(
                trade,
                exit_time=when,
                debit=debit,
                exit_reason="assignment",
                pnl=pnl,
            )
        )
    return out, changed


def _book_map(grouped, rows, start: date, end: date) -> dict[str, BookReport]:
    out = {}
    for row in rows:
        raw = in_window(grouped.get(row.name) or [], start, end)
        book = build_book(raw)
        out[row.name] = report_book(book, row.name, start, end)
    return out


def _fold_line(grouped, rows, train_end: date, test_start: date, test_end: date) -> dict:
    train_books = _book_map(grouped, rows, TRAIN_START, train_end)
    pick = chosen_name([train_row(row.name, row.searchable, train_books[row.name]) for row in rows])
    label = f"{test_start.isoformat()} to {test_end.isoformat()}"
    base_raw = in_window(grouped.get("base") or [], test_start, test_end)
    base_book = report_book(build_book(base_raw), "base", test_start, test_end)
    name = pick or "none"
    if pick:
        raw = in_window(grouped.get(pick) or [], test_start, test_end)
        book = report_book(build_book(raw), pick, test_start, test_end)
    else:
        book = base_book
        name = "none"
    return {
        "label": label,
        "selected": name,
        "n": 0 if pick is None else book.n,
        "monthly": "n/a" if pick is None else _ci(book.monthly, "month"),
        "dd": "n/a" if pick is None else f"{_usd(book.max_dd_dollars)} ({_pct(book.max_dd_frac, 1)})",
        "tpm": 0.0 if pick is None else book.trades_per_month,
        "base_monthly": _ci(base_book.monthly, "month"),
    }


def _scenarios(grouped) -> list[BookReport]:
    specs = (
        ("base_risk1", "base", dict(risk_pct=0.01)),
        ("base_cap10", "base", dict(max_concurrent=10)),
        ("d16_c10_risk1", "d16_c10", dict(risk_pct=0.01)),
        ("d16_c10_cap10", "d16_c10", dict(max_concurrent=10)),
        ("d16_c10", "d16_c10", dict()),
        ("base_tp25", "base_tp25", dict()),
        ("base_mid", "base_mid", dict()),
    )
    out = []
    for label, source, kwargs in specs:
        raw = in_window(grouped.get(source) or [], TEST_START, TEST_END)
        book = build_book(raw, **kwargs)
        out.append(report_book(book, label, TEST_START, TEST_END))
    return out


def run(cache: Path, chain_cache: Path, out: Path) -> str:
    cfg = load_config("config/default.yaml")
    symbols = list((cfg.get("universe") or {}).get("symbols") or [])
    print(f"loading bars for {len(symbols)} names", flush=True)
    bars = ensure_universe(cache, symbols, include_15m=False)
    structure = structure_from_config(cfg)
    daily = {symbol: (bars.get(symbol) or {}).get("1d") or [] for symbol in symbols}
    print("building iv regime", flush=True)
    regime = iv_regime(daily)
    rows = candidates()
    print(f"replay {len(rows)} candidates", flush=True)
    grouped, diags = replay_universe(
        bars,
        symbols,
        {row.name: allow_candidate(row) for row in rows},
        timing_key="1h",
        minutes=60,
        limits=rows[0].limits,
        structure=structure,
        limits_by_variant={row.name: row.limits for row in rows},
        regime_by_symbol=regime,
    )
    checkpoint = chain_cache.parent / "trades.pkl"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    with checkpoint.open("wb") as fh:
        pickle.dump({"grouped": grouped, "diags": diags}, fh)
    print(f"checkpoint {checkpoint}", flush=True)
    return _finish(
        grouped,
        diags,
        rows,
        bars,
        chain_cache,
        out,
    )


def _finish(grouped, diags, rows, bars, chain_cache: Path, out: Path) -> str:
    print("accounting", flush=True)
    train = _book_map(grouped, rows, TRAIN_START, TRAIN_END)
    test = _book_map(grouped, rows, TEST_START, TEST_END)
    winner = chosen_name([train_row(row.name, row.searchable, train[row.name]) for row in rows])
    print(f"winner {winner}", flush=True)
    folds = [_fold_line(grouped, rows, *fold) for fold in FOLDS]
    print("chains", flush=True)
    try:
        chains = collect(chain_cache)
    except Exception as exc:  # noqa: BLE001 — the report records a failed pull
        chains = {}
        print(f"chains failed: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
    print("surface", flush=True)
    surface = summarize_surface(surface_grid())
    daily = {symbol: (series.get("1d") or []) for symbol, series in bars.items()}
    base_all = list(grouped.get("base") or [])
    adjusted, n_assigned = _apply_assignment(base_all, daily)
    base_test_raw = in_window(base_all, TEST_START, TEST_END)
    adj_test_raw = in_window(adjusted, TEST_START, TEST_END)
    base_book = build_book(base_test_raw)
    adj_book = build_book(adj_test_raw)
    msft = _msft_spot(daily.get("MSFT") or [])
    ctx = {
        "winner": winner,
        "train": train,
        "test": test,
        "diags": diags,
        "candidates": rows,
        "folds": folds,
        "scenarios": _scenarios(grouped),
        "surface": surface,
        "chains": chains,
        "assignment": {
            "checked": len(base_all),
            "assigned": n_assigned,
            "base_pnl": sum(t.pnl for t in base_book),
            "assigned_pnl": sum(t.pnl for t in adj_book),
        },
        "msft_spot": msft,
        "tape_note": _tape_note(bars),
    }
    text = render(ctx)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}", flush=True)
    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=Path("var/replay-cache"))
    parser.add_argument("--chain-cache", type=Path, default=Path("var/research-3pct/chains"))
    parser.add_argument("--out", type=Path, default=Path("docs/research/options-3pct-redesign.md"))
    args = parser.parse_args()
    run(args.cache, args.chain_cache, args.out)


if __name__ == "__main__":
    main()
