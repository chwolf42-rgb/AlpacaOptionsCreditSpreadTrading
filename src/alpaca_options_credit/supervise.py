"""Parent supervisor: restart the engine child on death or stale heartbeat.

US equity options RTH is 9:30–16:00 America/New_York. The child writes a
heartbeat every loop, including off-hours (`idle_off_hours`), so overnight
silence is not treated as a crash. During RTH a heartbeat older than
`heartbeat.stale_after_seconds_rth` triggers kill + restart.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from alpaca_options_credit.config import var_dir
from alpaca_options_credit.heartbeat import heartbeat_age_seconds, is_stale
from alpaca_options_credit.rth import is_rth

log = logging.getLogger(__name__)


def child_argv(
    *,
    config: Optional[str],
    dry_run: bool,
    fixture: bool,
    log_level: str,
) -> list[str]:
    argv = [sys.executable, "-m", "alpaca_options_credit", "run", "--child"]
    if config:
        argv.extend(["--config", config])
    if dry_run:
        argv.append("--dry-run")
    if fixture:
        argv.append("--fixture")
    argv.extend(["--log-level", log_level])
    return argv


def stale_timeout(cfg: dict[str, Any], now) -> float:
    hb = cfg.get("heartbeat") or {}
    if is_rth(now):
        return float(hb.get("stale_after_seconds_rth", 90))
    return float(hb.get("stale_after_seconds_off_hours", 600))


def should_restart_child(
    *,
    alive: bool,
    heartbeat_path: Path,
    timeout: float,
    now=None,
) -> tuple[bool, str]:
    if not alive:
        return True, "child_exit"
    age = heartbeat_age_seconds(heartbeat_path, now=now)
    if is_stale(age, timeout):
        return True, "stale_heartbeat"
    return False, "ok"


def supervise_forever(cfg: dict[str, Any], child_args: list[str]) -> int:
    vdir = var_dir(cfg)
    hb_path = vdir / str((cfg.get("heartbeat") or {}).get("file", "heartbeat.json"))
    poll = float((cfg.get("heartbeat") or {}).get("supervise_poll_seconds", 5))
    backoffs = list((cfg.get("heartbeat") or {}).get("restart_backoff_seconds") or [2, 5, 15, 30])
    attempt = 0
    stop = False

    def _stop(signum, _frame):  # pragma: no cover
        nonlocal stop
        stop = True
        log.info("supervisor received signal %s", signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while not stop:
        log.info("supervisor spawning child: %s", " ".join(child_args))
        proc = subprocess.Popen(child_args)
        while not stop:
            time.sleep(poll)
            alive = proc.poll() is None
            timeout = stale_timeout(cfg, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
            restart, why = should_restart_child(
                alive=alive, heartbeat_path=hb_path, timeout=timeout
            )
            if not restart:
                continue
            log.warning("supervisor restart (%s) pid=%s", why, proc.pid)
            if alive:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            break
        if stop:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            return 0
        delay = backoffs[min(attempt, len(backoffs) - 1)]
        attempt += 1
        time.sleep(delay)
    return 0
