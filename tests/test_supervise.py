from datetime import datetime, timezone
from pathlib import Path

from alpaca_options_credit.heartbeat import HeartbeatWriter, Heartbeat, is_stale, heartbeat_age_seconds
from alpaca_options_credit.supervise import should_restart_child


def test_stale_heartbeat_requests_restart(tmp_path: Path):
    path = tmp_path / "heartbeat.json"
    w = HeartbeatWriter(path)
    w.write(
        Heartbeat(
            ts="2020-01-01T00:00:00+00:00",
            pid=1,
            status="rth_scan",
        )
    )
    now = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)
    age = heartbeat_age_seconds(path, now=now)
    assert age is not None and age > 90
    restart, why = should_restart_child(
        alive=True, heartbeat_path=path, timeout=90, now=now
    )
    assert restart is True
    assert why == "stale_heartbeat"


def test_fresh_heartbeat_ok(tmp_path: Path):
    path = tmp_path / "heartbeat.json"
    now = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)
    HeartbeatWriter(path).write(Heartbeat(ts=now.isoformat(), pid=1, status="rth_scan"))
    restart, why = should_restart_child(
        alive=True, heartbeat_path=path, timeout=90, now=now
    )
    assert restart is False
    assert why == "ok"


def test_dead_child_restarts_even_with_fresh_beat(tmp_path: Path):
    path = tmp_path / "heartbeat.json"
    now = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)
    HeartbeatWriter(path).write(Heartbeat(ts=now.isoformat(), pid=1, status="rth_scan"))
    restart, why = should_restart_child(
        alive=False, heartbeat_path=path, timeout=90, now=now
    )
    assert restart is True
    assert why == "child_exit"


def test_is_stale_none_age():
    assert is_stale(None, 90) is True
    assert is_stale(10, 90) is False
