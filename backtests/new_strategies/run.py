"""Walk-forward driver. Holdout is evaluated in this process after OOS, once.

Usage:
  python3 -m backtests.new_strategies.run
  python3 -m backtests.new_strategies.run --refresh
  python3 -m backtests.new_strategies.run --stage oos
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

from backtests.new_strategies.data import ensure_cache, load_market
from backtests.new_strategies.engine import prepare, simulate
from backtests.new_strategies.metrics import select_winner, summarize_window
from backtests.new_strategies.specs import (
    ACCOUNT,
    BOOTSTRAP_BLOCK,
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    FOLDS,
    GRIDS,
    HOLDOUT_END,
    HOLDOUT_FIT_END,
    HOLDOUT_FIT_START,
    HOLDOUT_START,
    IS_END,
    IS_START,
    MAX_GROSS,
    MAX_OPEN_RISK_PCT,
    MAX_SPREADS,
    MIN_FIT_TRADES,
    OOS_END,
    OOS_START,
    UNIVERSE,
    params_id,
)

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "research" / "new_strategies_options"


def _budget(strategy: str) -> dict:
    """Standalone Alpaca data cost of one daily scan matching this backtest.

    Stock bars are two batched requests (daily + hourly). Each name then
    needs an option-chain snapshot. Cadence is once a day, after the cash
    close, because the signal is a closed daily bar. Vol indexes are one
    external HTTP get and are not part of the Alpaca 200/min budget.
    """
    names = len(UNIVERSE[strategy])
    per_scan = 2 + names
    return {
        "alpaca_requests_per_scan": per_scan,
        "scans_per_day": 1,
        "burst_requests_per_min": per_scan,
        "share_of_200_burst": per_scan / 200.0,
        "external_vol_requests_per_scan": 1,
        "note": (
            "Daily cadence matches the backtest. An intraday 0-7 DTE scan "
            "was not tested: Yahoo 5-minute history is about 60 days."
        ),
    }


def _jsonable(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, float):
        return value
    return value


def _score_row(strategy: str, params: dict, result: dict, start: date, end: date, grid_index: int) -> dict:
    summary = summarize_window(result["trades"], result["equity"], result["exposure"], start, end, ACCOUNT)
    mean = summary["monthly"]["mean"]
    return {
        "strategy": strategy,
        "grid_index": grid_index,
        "params": params,
        "params_id": params_id(strategy, params),
        "mean_monthly": mean if mean is not None else -1e9,
        "max_dd": summary["max_drawdown"]["pct"],
        "n_trades": summary["trade_stats"]["n"],
        "summary": summary,
        "orphan_trades": result["orphan_trades"],
        "structure_trades": result["structure_trades"],
    }


def _eval_grid(prep, strategy: str, start: date, end: date, iv_model: str = "primary") -> list[dict]:
    rows = []
    for i, params in enumerate(GRIDS[strategy]):
        result = simulate(prep, strategy, params, start, end, iv_model=iv_model)
        rows.append(_score_row(strategy, params, result, start, end, i))
        print(
            f"  {strategy} [{start} {end}] {i} trades={rows[-1]['n_trades']} "
            f"mean={rows[-1]['mean_monthly']:.4%}",
            flush=True,
        )
    return rows


def _path_summary(prep, strategy: str, params: dict, start: date, end: date, schedule=None, iv_model: str = "primary") -> dict:
    result = simulate(prep, strategy, params, start, end, iv_model=iv_model, schedule=schedule)
    summary = summarize_window(result["trades"], result["equity"], result["exposure"], start, end, ACCOUNT)
    summary["orphan_trades"] = result["orphan_trades"]
    summary["structure_trades"] = result["structure_trades"]
    summary["params_id"] = params_id(strategy, params)
    summary["trades"] = result["trades"]
    return summary


def run(stage: str) -> dict:
    market = load_market()
    prep = prepare(market)
    evaluations: list[dict] = []
    candidates = []
    for strategy, grid in GRIDS.items():
        print(f"== {strategy} IS grid", flush=True)
        is_rows = _eval_grid(prep, strategy, IS_START, IS_END)
        for row in is_rows:
            evaluations.append(
                {
                    "strategy": strategy,
                    "role": "is_grid",
                    "params_id": row["params_id"],
                    "window": [IS_START.isoformat(), IS_END.isoformat()],
                    "mean_monthly": row["mean_monthly"],
                    "n_trades": row["n_trades"],
                    "selected": False,
                }
            )
        is_winner = select_winner(is_rows, MIN_FIT_TRADES)
        for row in evaluations:
            if row["role"] == "is_grid" and row["strategy"] == strategy and row["params_id"] == is_winner["params_id"]:
                row["selected"] = True
        default_params = grid[0]
        print(f"== {strategy} IS default path", flush=True)
        is_default = _path_summary(prep, strategy, default_params, IS_START, IS_END)
        is_selected = is_winner["summary"]
        is_selected["params"] = is_winner["params"]
        is_selected["params_id"] = is_winner["params_id"]

        schedule = []
        fold_log = []
        for fit_start, fit_end, trade_start, trade_end in FOLDS:
            print(f"== {strategy} fit {fit_start} {fit_end}", flush=True)
            fit_rows = _eval_grid(prep, strategy, fit_start, fit_end)
            winner = select_winner(fit_rows, MIN_FIT_TRADES)
            for row in fit_rows:
                evaluations.append(
                    {
                        "strategy": strategy,
                        "role": "walk_forward_fit",
                        "params_id": row["params_id"],
                        "window": [fit_start.isoformat(), fit_end.isoformat()],
                        "mean_monthly": row["mean_monthly"],
                        "n_trades": row["n_trades"],
                        "selected": row["params_id"] == winner["params_id"],
                    }
                )
            schedule.append((trade_start, trade_end, winner["params"]))
            fold_log.append(
                {
                    "fit": [fit_start.isoformat(), fit_end.isoformat()],
                    "trade": [trade_start.isoformat(), trade_end.isoformat()],
                    "selected": winner["params_id"],
                    "fit_mean_monthly": winner["mean_monthly"],
                    "fit_trades": winner["n_trades"],
                }
            )
        print(f"== {strategy} OOS path", flush=True)
        oos = _path_summary(prep, strategy, default_params, OOS_START, OOS_END, schedule=schedule)
        oos["folds"] = fold_log

        print(f"== {strategy} stress flat RV", flush=True)
        stress_is = _path_summary(prep, strategy, default_params, IS_START, IS_END, iv_model="flat_rv")
        stress_oos = _path_summary(prep, strategy, default_params, OOS_START, OOS_END, iv_model="flat_rv")
        evaluations.append(
            {
                "strategy": strategy,
                "role": "stress_flat_rv_is",
                "params_id": params_id(strategy, default_params),
                "window": [IS_START.isoformat(), IS_END.isoformat()],
                "mean_monthly": stress_is["monthly"]["mean"],
                "n_trades": stress_is["trade_stats"]["n"],
                "selected": False,
            }
        )
        evaluations.append(
            {
                "strategy": strategy,
                "role": "stress_flat_rv_oos",
                "params_id": params_id(strategy, default_params),
                "window": [OOS_START.isoformat(), OOS_END.isoformat()],
                "mean_monthly": stress_oos["monthly"]["mean"],
                "n_trades": stress_oos["trade_stats"]["n"],
                "selected": False,
            }
        )

        holdout = None
        holdout_choice = None
        if stage == "all":
            print(f"== {strategy} holdout fit {HOLDOUT_FIT_START} {HOLDOUT_FIT_END}", flush=True)
            hold_rows = _eval_grid(prep, strategy, HOLDOUT_FIT_START, HOLDOUT_FIT_END)
            hold_winner = select_winner(hold_rows, MIN_FIT_TRADES)
            for row in hold_rows:
                evaluations.append(
                    {
                        "strategy": strategy,
                        "role": "holdout_fit",
                        "params_id": row["params_id"],
                        "window": [HOLDOUT_FIT_START.isoformat(), HOLDOUT_FIT_END.isoformat()],
                        "mean_monthly": row["mean_monthly"],
                        "n_trades": row["n_trades"],
                        "selected": row["params_id"] == hold_winner["params_id"],
                    }
                )
            holdout_choice = hold_winner["params_id"]
            print(f"== {strategy} HOLDOUT once params={holdout_choice}", flush=True)
            holdout = _path_summary(prep, strategy, hold_winner["params"], HOLDOUT_START, HOLDOUT_END)
            holdout["params"] = hold_winner["params"]

        tried = [e for e in evaluations if e["strategy"] == strategy]
        candidates.append(
            {
                "strategy": strategy,
                "unique_configs": len(grid),
                "variants_tried": len(tried),
                "data_budget": _budget(strategy),
                "is_default": _strip_trades(is_default),
                "is_selected": _strip_trades(is_selected),
                "oos": _strip_trades(oos),
                "stress_is": _strip_trades(stress_is),
                "stress_oos": _strip_trades(stress_oos),
                "holdout": _strip_trades(holdout) if holdout else None,
                "holdout_params": holdout_choice,
                "folds": fold_log,
                "_oos_trades": oos["trades"],
                "_holdout_trades": holdout["trades"] if holdout else [],
                "_is_trades": is_default["trades"],
            }
        )
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "protocol": {
            "is": [IS_START.isoformat(), IS_END.isoformat()],
            "oos": [OOS_START.isoformat(), OOS_END.isoformat()],
            "holdout": [HOLDOUT_START.isoformat(), HOLDOUT_END.isoformat()],
            "holdout_fit": [HOLDOUT_FIT_START.isoformat(), HOLDOUT_FIT_END.isoformat()],
            "bootstrap": {
                "block_months": BOOTSTRAP_BLOCK,
                "resamples": BOOTSTRAP_RESAMPLES,
                "seed": BOOTSTRAP_SEED,
                "interval": "90pct",
            },
            "account": ACCOUNT,
            "risk_pct": 0.005,
            "max_spreads": MAX_SPREADS,
            "max_open_risk_pct": MAX_OPEN_RISK_PCT,
            "max_gross": MAX_GROSS,
            "min_fit_trades": MIN_FIT_TRADES,
            "selection": "highest mean monthly return, then lower max drawdown, then lower grid index; default if none has min trades",
        },
        "evaluations": evaluations,
        "candidates": [{k: v for k, v in c.items() if not k.startswith("_")} for c in candidates],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(_jsonable(payload), indent=2))
    _write_month_csv(candidates)
    _write_trade_csv(candidates)
    if stage == "all":
        log = {
            "generated_at": payload["generated_at"],
            "note": "Holdout window evaluated once in this run, after walk-forward selection was frozen per strategy.",
            "holdout": [HOLDOUT_START.isoformat(), HOLDOUT_END.isoformat()],
            "choices": {c["strategy"]: c["holdout_params"] for c in candidates},
        }
        (OUT / "holdout_log.json").write_text(json.dumps(log, indent=2))
    print("wrote", OUT / "results.json", flush=True)
    return payload


def _strip_trades(summary: dict) -> dict:
    out = {k: v for k, v in summary.items() if k != "trades"}
    return out


def _write_month_csv(candidates: list[dict]) -> None:
    path = OUT / "monthly_returns.csv"
    lines = ["strategy,window,month,return,trades"]
    for cand in candidates:
        for window, key in (("is_default", "is_default"), ("oos", "oos"), ("holdout", "holdout")):
            block = cand.get(key)
            if not block:
                continue
            for month in block["months"]:
                lines.append(
                    f"{cand['strategy']},{window},{month['month']},{month['return']:.8f},{month['trades']}"
                )
    path.write_text("\n".join(lines) + "\n")


def _write_trade_csv(candidates: list[dict]) -> None:
    path = OUT / "trades.csv"
    fields = [
        "strategy",
        "window",
        "symbol",
        "entry_date",
        "exit_date",
        "pnl",
        "r",
        "reason",
        "qty",
        "risk_dollars",
        "role",
        "completed_structure",
        "params_id",
    ]
    lines = [",".join(fields)]
    for cand in candidates:
        for window, attr in (("is_default", "_is_trades"), ("oos", "_oos_trades"), ("holdout", "_holdout_trades")):
            for trade in cand.get(attr) or []:
                row = [
                    cand["strategy"],
                    window,
                    trade["symbol"],
                    trade["entry_date"].isoformat(),
                    trade["exit_date"].isoformat(),
                    f"{trade['pnl']:.4f}",
                    f"{trade['r']:.6f}",
                    trade["reason"],
                    str(trade["qty"]),
                    f"{trade['risk_dollars']:.2f}",
                    trade["role"],
                    "1" if trade["completed_structure"] else "0",
                    trade["params_id"].replace(",", ";"),
                ]
                lines.append(",".join(row))
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--stage", choices=("oos", "all"), default="all")
    args = parser.parse_args()
    ensure_cache(refresh=args.refresh)
    run(args.stage)


if __name__ == "__main__":
    main()
