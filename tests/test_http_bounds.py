"""Hung Alpaca REST calls must time out inside the stale-heartbeat budget."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from alpaca_options_credit.broker.alpaca import AlpacaBroker
from alpaca_options_credit.broker.dry_run import DryRunBroker
from alpaca_options_credit.config import load_config
from alpaca_options_credit.engine import Engine
from alpaca_options_credit.errors import SubmitUnconfirmed
from alpaca_options_credit.heartbeat import Heartbeat, HeartbeatWriter
from alpaca_options_credit.http_bounds import (
    HttpPolicy,
    install_http_bounds,
    policy_from_config,
    set_inflight_hook,
)
from alpaca_options_credit.journal import Journal
from alpaca_options_credit.supervise import log_restart, stale_block_detail

UTC = timezone.utc


class _StopLoop(RuntimeError):
    pass


def _mleg_payload() -> dict:
    return {
        "order_class": "mleg",
        "qty": "1",
        "type": "limit",
        "limit_price": "-1.05",
        "time_in_force": "day",
        "legs": [
            {
                "symbol": "SPY260417P00100000",
                "ratio_qty": "1",
                "side": "sell",
                "position_intent": "sell_to_open",
            },
            {
                "symbol": "SPY260417P00095000",
                "ratio_qty": "1",
                "side": "buy",
                "position_intent": "buy_to_open",
            },
        ],
    }


def _broker(trading) -> AlpacaBroker:
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._trading = trading
    broker._unconfirmed_submits = set()
    return broker


class _CountingSession:
    def __init__(self, *, status: int = 200, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.status = status
        self.error = error

    def request(self, method, url, *args, **kwargs):
        self.calls.append((str(method).upper(), kwargs.get("timeout")))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(status_code=self.status, headers={})


def test_default_read_budget_stays_under_stale_heartbeat():
    policy = policy_from_config({"heartbeat": {"stale_after_seconds_rth": 90}})
    assert 10 <= policy.timeout_seconds <= 20
    assert policy.read_attempts >= 2
    assert policy.worst_case_seconds() <= policy.read_budget_seconds
    assert policy.worst_case_seconds() < 90

    huge = policy_from_config(
        {
            "http": {
                "timeout_seconds": 30,
                "read_attempts": 10,
                "read_backoff_seconds": [1, 1, 1, 1],
                "read_budget_seconds": 80,
            },
            "heartbeat": {"stale_after_seconds_rth": 90},
        }
    )
    assert huge.worst_case_seconds() <= huge.read_budget_seconds
    assert huge.worst_case_seconds() < 90


def test_alpaca_clients_are_bounded_and_do_not_retry_posts():
    """Trading, stock, and option clients share the timeout wrapper.

    alpaca-py retries POST on HTTP 504. That retry is disabled so a multi-leg
    submit cannot be sent twice by the SDK.
    """
    from alpaca_options_credit.broker.alpaca import AlpacaMarketData
    from alpaca_options_credit.credentials import Credentials
    from alpaca_options_credit.market_data_limit import reset_limiter

    reset_limiter()
    creds = Credentials(
        api_key_id="test-key",
        api_secret_key="test-secret",
        base_url="https://paper-api.alpaca.markets",
        data_url="https://data.alpaca.markets",
        expected_account_number=None,
    )
    data = AlpacaMarketData(creds, {})
    try:
        for client in (data._stock, data._opt_data, data._trading):
            assert client._retry == 0
            assert getattr(client._session, "_options_http_bounded", False)
        assert getattr(data._stock._session, "_options_md_limited", False)
        assert not getattr(data._trading._session, "_options_md_limited", False)
    finally:
        reset_limiter()


def test_default_yaml_documents_http_keys():
    http = load_config()["http"]
    assert http["timeout_seconds"] == 15
    assert http["read_attempts"] == 3
    assert http["read_backoff_seconds"] == [0.5, 1.0]
    assert http["read_budget_seconds"] == 50


def test_read_retries_are_bounded_and_posts_are_not():
    policy = HttpPolicy(
        timeout_seconds=0.2,
        read_attempts=3,
        read_backoff_seconds=(0.0, 0.0),
        read_budget_seconds=5,
    )
    session = _CountingSession(error=requests.Timeout("simulated hang"))
    install_http_bounds(session, policy)
    with pytest.raises(requests.Timeout):
        session.request("GET", "https://data.alpaca.markets/v2/stocks/bars", params={"symbols": "SPY"})
    assert len(session.calls) == 3
    assert all(timeout == (0.2, 0.2) for _, timeout in session.calls)

    post = _CountingSession(error=requests.Timeout("simulated hang"))
    install_http_bounds(post, policy)
    with pytest.raises(requests.Timeout):
        post.request("POST", "https://paper-api.alpaca.markets/v2/orders")
    assert len(post.calls) == 1

    retry_status = _CountingSession(status=503)
    install_http_bounds(retry_status, policy)
    response = retry_status.request("GET", "https://data.alpaca.markets/v2/stocks/bars")
    assert response.status_code == 503
    assert len(retry_status.calls) == 3


def test_hanging_http_times_out_and_loop_keeps_beating(tmp_path: Path, monkeypatch):
    """A server that accepts and never answers must not stall the poll loop."""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(0.5)
    port = server.getsockname()[1]
    stop = threading.Event()

    def _accept() -> None:
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
            except OSError:
                continue
            try:
                # Stay open and silent so the client blocks in the read,
                # which is the stall alpaca-py hits with timeout=None.
                conn.settimeout(0.2)
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and not stop.is_set():
                    try:
                        chunk = conn.recv(64)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()

    hb_path = tmp_path / "heartbeat.json"
    seen: dict = {}
    session = requests.Session()
    original = session.request

    def _spy(method, url, *args, **kwargs):
        if hb_path.is_file() and "during" not in seen:
            seen["during"] = json.loads(hb_path.read_text(encoding="utf-8"))
            seen["timeout"] = kwargs.get("timeout")
        return original(method, url, *args, **kwargs)

    session.request = _spy  # type: ignore[method-assign]
    policy = HttpPolicy(
        timeout_seconds=0.25,
        read_attempts=2,
        read_backoff_seconds=(0.01,),
        read_budget_seconds=5,
    )
    install_http_bounds(session, policy)

    class _Data:
        def __init__(self) -> None:
            self.calls = 0

        def bars(self, symbol, timeframe, limit):
            self.calls += 1
            if self.calls == 1:
                session.request(
                    "GET",
                    f"http://127.0.0.1:{port}/v2/stocks/bars",
                    params={"symbols": symbol},
                )
            return []

        def chain(self, *args, **kwargs):
            return []

        def spread_mark(self, *args, **kwargs):
            return None

    cfg = load_config()
    cfg["bot"]["dry_run"] = True
    cfg["bot"]["var_dir"] = str(tmp_path / "var")
    cfg["universe"]["symbols"] = ["SPY"]
    cfg["rth"]["scan_only_rth"] = False
    cfg["calendar"]["skip_fomc"] = False
    cfg["calendar"]["skip_earnings"] = False
    journal = Journal(tmp_path / "journal.sqlite")
    writer = HeartbeatWriter(hb_path)
    now = datetime(2026, 3, 4, 15, 0, tzinfo=UTC)
    engine = Engine(
        cfg,
        journal,
        DryRunBroker(),
        _Data(),
        dry_run=True,
        heartbeat=writer,
        now_fn=lambda: now,
        calendar={"fomc": [], "earnings": {}},
    )
    sleeps: list[float] = []

    def _fast_sleep(seconds: float) -> None:
        # Patching engine.time.sleep patches the stdlib sleep. Ignore the
        # short read-retry backoff and only stop after two poll-loop sleeps.
        if seconds < 1:
            return
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise _StopLoop

    monkeypatch.setattr("alpaca_options_credit.engine.time.sleep", _fast_sleep)
    started = time.monotonic()
    try:
        with pytest.raises(_StopLoop):
            engine.run_forever()
    finally:
        stop.set()
        server.close()
        set_inflight_hook(None)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"hung HTTP was not bounded ({elapsed:.2f}s)"
    assert engine.loop >= 2
    assert seen["timeout"] == (0.25, 0.25)
    assert seen["during"]["inflight_op"] == "get_stock_bars"
    assert seen["during"]["inflight_symbol"] == "SPY"
    after = json.loads(hb_path.read_text(encoding="utf-8"))
    assert after["loop"] >= 1
    assert after["status"] in {"rth_scan", "idle_off_hours"}


def test_timed_out_mleg_submit_reconciles_by_client_order_id_once():
    class _Trading:
        def __init__(self) -> None:
            self.submits = 0
            self.lookups: list[str] = []
            self.request = None

        def submit_order(self, req):
            self.submits += 1
            self.request = req
            raise requests.Timeout("submit hung")

        def get_order_by_client_id(self, client_id):
            self.lookups.append(str(client_id))
            return SimpleNamespace(id="ord-99", client_order_id=client_id)

    trading = _Trading()
    broker = _broker(trading)
    payload = _mleg_payload()
    order_id = broker._submit_mleg(payload)
    assert order_id == "ord-99"
    assert trading.submits == 1
    assert trading.lookups == [payload["client_order_id"]]
    assert trading.request.client_order_id == payload["client_order_id"]
    assert len(trading.request.legs) == 2


def test_timed_out_mleg_submit_is_not_resent_when_lookup_also_fails():
    class _Trading:
        def __init__(self) -> None:
            self.submits = 0
            self.lookups: list[str] = []

        def submit_order(self, req):
            self.submits += 1
            self.request = req
            raise requests.Timeout("submit hung")

        def get_order_by_client_id(self, client_id):
            self.lookups.append(str(client_id))
            raise requests.Timeout("lookup hung")

    trading = _Trading()
    broker = _broker(trading)
    payload = _mleg_payload()
    with pytest.raises(SubmitUnconfirmed):
        broker._submit_mleg(payload)
    assert trading.submits == 1
    assert len(trading.request.legs) == 2
    with pytest.raises(SubmitUnconfirmed):
        broker._submit_mleg(dict(payload))
    assert trading.submits == 1
    assert len(trading.lookups) == 2
    assert trading.lookups[0] == trading.lookups[1]


def test_rejected_mleg_submit_is_not_reconciled_or_retried():
    class _Reject(Exception):
        def __init__(self) -> None:
            super().__init__("invalid order")
            self.response = SimpleNamespace(status_code=422)

    class _Trading:
        def __init__(self) -> None:
            self.submits = 0
            self.lookups = 0

        def submit_order(self, req):
            self.submits += 1
            raise _Reject()

        def get_order_by_client_id(self, client_id):
            self.lookups += 1
            raise AssertionError("rejected submit must not be looked up")

    trading = _Trading()
    broker = _broker(trading)
    with pytest.raises(_Reject):
        broker._submit_mleg(_mleg_payload())
    assert trading.submits == 1
    assert trading.lookups == 0


class _HttpStatus(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"http {status}")
        self.response = SimpleNamespace(status_code=status)


class _OrderMissing(Exception):
    def __init__(self) -> None:
        super().__init__("order not found")
        self.response = SimpleNamespace(status_code=404)


def _ambiguous_failure(kind: int | str) -> BaseException:
    if kind == "timeout":
        return requests.Timeout("submit hung")
    return _HttpStatus(int(kind))


@pytest.mark.parametrize("failure", [429, 500, 503, 504, "timeout"])
@pytest.mark.parametrize("move_limit", [False, True])
def test_ambiguous_mleg_is_found_by_client_order_id_on_the_next_poll(failure, move_limit):
    """The engine rebuilds the payload on the next poll. A moved close debit
    must not POST again until the earlier client_order_id has been looked up.
    """

    class _Trading:
        def __init__(self) -> None:
            self.submits = 0
            self.events: list[tuple[str, str]] = []

        def submit_order(self, req):
            self.events.append(("submit", str(req.client_order_id)))
            self.submits += 1
            raise _ambiguous_failure(failure)

        def get_order_by_client_id(self, client_id):
            self.events.append(("lookup", str(client_id)))
            lookups = sum(1 for kind, _ in self.events if kind == "lookup")
            if lookups < 2:
                raise _OrderMissing()
            return SimpleNamespace(id="ord-99", client_order_id=client_id, status="new")

    trading = _Trading()
    broker = _broker(trading)
    first = _mleg_payload()
    with pytest.raises((SubmitUnconfirmed, _HttpStatus, requests.Timeout)):
        broker._submit_mleg(first)
    assert trading.submits == 1
    first_id = first["client_order_id"]
    # Next poll: a new dict, the way the engine rebuilds the close. The debit
    # moves when the mark moves; the attempt key does not.
    second = _mleg_payload()
    if move_limit:
        second["limit_price"] = "0.55"
    order_id = broker._submit_mleg(second)
    assert order_id == "ord-99"
    assert trading.submits == 1
    assert [kind for kind, _ in trading.events].count("submit") == 1
    # The rebuilt payload is not posted. The lookup is the id from the first attempt,
    # including when the close debit moved.
    assert trading.events[-1] == ("lookup", first_id)
    assert "client_order_id" not in second


def test_restarted_broker_looks_up_the_ambiguous_submit_before_posting(tmp_path: Path):
    class _Trading:
        def __init__(self, *, visible: bool) -> None:
            self.visible = visible
            self.submits = 0
            self.lookups: list[str] = []

        def submit_order(self, req):
            self.submits += 1
            raise _HttpStatus(429)

        def get_order_by_client_id(self, client_id):
            self.lookups.append(str(client_id))
            if not self.visible:
                raise _OrderMissing()
            return SimpleNamespace(id="ord-7", client_order_id=client_id, status="accepted")

    state = tmp_path / "mleg_submit_attempts.json"
    first_trading = _Trading(visible=False)
    first = _broker(first_trading)
    first._submit_state_path = state
    payload = _mleg_payload()
    with pytest.raises(SubmitUnconfirmed):
        first._submit_mleg(payload)
    assert first_trading.submits == 1
    assert state.is_file()

    second_trading = _Trading(visible=True)
    second = _broker(second_trading)
    second._submit_state_path = state
    rebuilt = _mleg_payload()
    rebuilt["limit_price"] = "0.40"
    assert second._submit_mleg(rebuilt) == "ord-7"
    assert second_trading.submits == 0
    assert second_trading.lookups == [payload["client_order_id"]]


def test_stale_heartbeat_log_names_blocking_op(tmp_path: Path, caplog):
    path = tmp_path / "heartbeat.json"
    since = datetime(2026, 10, 2, 16, 18, 58, tzinfo=UTC)
    now = datetime(2026, 10, 2, 16, 20, 38, tzinfo=UTC)
    HeartbeatWriter(path).write(
        Heartbeat(
            ts=since.isoformat(),
            pid=14280,
            status="rth_scan",
            inflight_op="get_stock_bars",
            inflight_symbol="SPY",
            inflight_since=since.isoformat(),
        )
    )
    detail = stale_block_detail(path, now=now)
    assert detail == "blocked_in=get_stock_bars:SPY for 100s"
    with caplog.at_level(logging.WARNING, logger="alpaca_options_credit.supervise"):
        log_restart("stale_heartbeat", 14280, path, now=now)
    assert (
        "supervisor restart (stale_heartbeat) pid=14280 blocked_in=get_stock_bars:SPY for 100s"
        in caplog.text
    )
