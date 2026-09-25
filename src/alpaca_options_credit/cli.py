"""CLI: run / observe / supervise.

  alpaca-options-credit observe --fixture          # no keys, zero orders
  alpaca-options-credit observe --config ...       # live data, zero orders (needs keys)
  alpaca-options-credit supervise --dry-run
  alpaca-options-credit run                        # paper orders (needs keys)
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional, Sequence

from dotenv import load_dotenv

from alpaca_options_credit.config import load_config
from alpaca_options_credit.errors import BotError, MissingCredentialsError


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default=None, help="YAML config (default: config/default.yaml)")
    p.add_argument("--log-level", default=None, help="DEBUG|INFO|WARNING|ERROR")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Observer mode: log proposed mleg payloads, place zero orders.",
    )
    p.add_argument(
        "--fixture",
        action="store_true",
        help="Use in-process bars/chain (no Alpaca keys). Implies dry-run.",
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alpaca-options-credit",
        description="Paper-only Alpaca bull-put / bear-call credit-spread bot.",
    )
    _add_common(p)
    sub = p.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="Engine loop (scan, arms, mleg, reconcile).")
    _add_common(run_p)
    run_p.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    run_p.add_argument("--once", action="store_true", help="Single tick then exit (tests / smoke).")

    observe_p = sub.add_parser("observe", help="Dry-run / observer: same signals, zero orders.")
    _add_common(observe_p)
    observe_p.add_argument("--once", action="store_true", help="Single tick then exit.")

    sup_p = sub.add_parser(
        "supervise",
        help="Parent: spawn run child and restart on death or stale heartbeat.",
    )
    _add_common(sup_p)

    backfill_p = sub.add_parser(
        "backfill-close-debits",
        help=(
            "Fill NULL close_debit on closed spreads from historical option "
            "quotes/bars at the journal close time. Does not flip dry_run."
        ),
    )
    _add_common(backfill_p)
    backfill_p.add_argument(
        "--journal",
        default=None,
        help="journal.sqlite path (default: the config var dir).",
    )
    return p


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_dotenv()
    args, unknown = _parser().parse_known_args(argv)
    # Allow flags after subcommand: `run --once --dry-run`
    if unknown:
        extra, _ = _parser().parse_known_args(unknown)
        for k, v in vars(extra).items():
            if getattr(args, k, None) in (None, False) and v not in (None, False):
                setattr(args, k, v)

    cmd = args.cmd or "observe"
    cfg = load_config(args.config)
    level = args.log_level or (cfg.get("bot") or {}).get("log_level", "INFO")
    setup_logging(str(level))
    dry_run = bool(args.dry_run) or cmd == "observe" or bool((cfg.get("bot") or {}).get("dry_run"))
    fixture = bool(args.fixture)
    if cmd == "observe":
        dry_run = True

    log = logging.getLogger("alpaca_options_credit")
    log.info(
        "cmd=%s dry_run=%s fixture=%s structure_bar=%s timing_bar=%s",
        cmd,
        dry_run,
        fixture,
        (cfg.get("timeframe") or {}).get("structure_bar", "1Day"),
        (cfg.get("timeframe") or {}).get("timing_bar", "1Hour"),
    )

    try:
        if cmd == "backfill-close-debits":
            return _run_backfill(args, cfg)

        if cmd == "supervise":
            from alpaca_options_credit.supervise import child_argv, supervise_forever

            child = child_argv(
                config=args.config,
                dry_run=dry_run,
                fixture=fixture,
                log_level=str(level),
            )
            return supervise_forever(cfg, child)

        from alpaca_options_credit.engine import build_engine

        engine = build_engine(cfg, dry_run=dry_run, fixture=fixture)
        once = bool(getattr(args, "once", False))
        if once:
            result = engine.tick()
            log.info(
                "tick status=%s proposals=%d exits=%s",
                result.status,
                len(result.proposals),
                result.exits,
            )
            return 0
        engine.run_forever()
        return 0
    except MissingCredentialsError as exc:
        logging.error("%s", exc)
        return 2
    except BotError as exc:
        logging.error("%s", exc)
        return 2


def _run_backfill(args, cfg) -> int:
    """Preview or write estimated close debits. Ignores bot.dry_run.

    The engine's dry_run flag stays whatever config says. This command's
    ``--dry-run`` only means "print the debits, do not write".
    """
    from pathlib import Path

    from alpaca_options_credit.backfill import backfill_close_debits, format_backfill_report
    from alpaca_options_credit.config import var_dir
    from alpaca_options_credit.journal import Journal

    journal_arg = getattr(args, "journal", None)
    journal_path = Path(journal_arg) if journal_arg else var_dir(cfg) / "journal.sqlite"
    journal = Journal(journal_path)
    preview = bool(getattr(args, "dry_run", False))
    log = logging.getLogger("alpaca_options_credit")
    missing = [s for s in journal.closed_spreads() if s.close_debit is None]
    if not missing:
        report = backfill_close_debits(journal, _no_price, dry_run=preview)
        text = format_backfill_report(report)
        print(text)
        log.info("%s", text.replace("\n", " | "))
        return 0

    from alpaca_options_credit.broker.alpaca import AlpacaMarketData
    from alpaca_options_credit.credentials import load_credentials

    creds = load_credentials()
    data = AlpacaMarketData(creds, cfg)

    def price_at(short: str, long: str, at):
        return data.historical_spread_debit(short, long, at)

    report = backfill_close_debits(journal, price_at, dry_run=preview)
    text = format_backfill_report(report)
    print(text)
    log.info("%s", text.replace("\n", " | "))
    return 0


def _no_price(*_args, **_kwargs):
    return None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
