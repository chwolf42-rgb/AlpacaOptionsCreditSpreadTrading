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
        "cmd=%s dry_run=%s fixture=%s timeframe=%s",
        cmd,
        dry_run,
        fixture,
        (cfg.get("timeframe") or {}).get("bar", "1Hour"),
    )

    try:
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
