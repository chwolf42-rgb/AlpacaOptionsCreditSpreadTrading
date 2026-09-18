"""Atomic heartbeat file for the supervise watchdog."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Heartbeat:
    ts: str
    pid: int
    status: str
    loop: int = 0
    dry_run: bool = True
    detail: str = ""


class HeartbeatWriter:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, beat: Heartbeat) -> None:
        payload = json.dumps(asdict(beat), separators=(",", ":"))
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)


def read_heartbeat(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def heartbeat_age_seconds(path: Path, now: Optional[datetime] = None) -> Optional[float]:
    data = read_heartbeat(path)
    if not data or "ts" not in data:
        if not path.is_file():
            return None
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        now = now or _utcnow()
        return (now - mtime).total_seconds()
    try:
        ts = datetime.fromisoformat(data["ts"].replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    now = now or _utcnow()
    return (now - ts).total_seconds()


def is_stale(
    age_seconds: Optional[float],
    timeout: float,
) -> bool:
    if age_seconds is None:
        return True
    return age_seconds > timeout
