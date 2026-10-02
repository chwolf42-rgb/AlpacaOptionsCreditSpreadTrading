"""Bounded timeouts and read retries for Alpaca REST calls in the poll loop.

alpaca-py's ``requests`` session does not set a timeout, so a stalled TCP read
blocks the engine until the OS gives up. That silence is what trips
``heartbeat.stale_after_seconds_rth`` (default 90s). Every poll-loop client
installs the wrapper below.

Idempotent GET/HEAD calls retry inside ``read_budget_seconds``, which is kept
under the RTH stale threshold. POST/PUT/PATCH/DELETE are a single attempt.
The in-flight op is published through the heartbeat hook so a stale restart
can name the call that was still running.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional
from urllib.parse import parse_qs, urlparse

import requests

log = logging.getLogger(__name__)

# Connect and read. A single hung read is what the October 2026 restarts show.
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_READ_ATTEMPTS = 3
DEFAULT_READ_BACKOFF_SECONDS = (0.5, 1.0)
# 3 * 15s + 0.5s + 1.0s = 46.5s, under the 90s RTH stale heartbeat.
DEFAULT_READ_BUDGET_SECONDS = 50.0

# Statuses that are safe to retry on a read. Writes never use this set.
_READ_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_BOUNDED = "_options_http_bounded"

_symbol_hint: ContextVar[str] = ContextVar("options_http_symbol", default="")
_hook: Optional[Callable[[], None]] = None
_lock = threading.Lock()
# (op, symbol, since_iso) stack. The top is the call currently blocked.
_stack: list[tuple[str, str, str]] = []


@dataclass(frozen=True)
class HttpPolicy:
    """Connect+read timeout and the read-retry budget for one process."""

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    read_attempts: int = DEFAULT_READ_ATTEMPTS
    read_backoff_seconds: tuple[float, ...] = DEFAULT_READ_BACKOFF_SECONDS
    read_budget_seconds: float = DEFAULT_READ_BUDGET_SECONDS

    def worst_case_seconds(self) -> float:
        """Upper bound on one idempotent read, including timeouts and backoff."""
        total = 0.0
        attempts = max(1, int(self.read_attempts))
        backoffs = self.read_backoff_seconds or ()
        for i in range(attempts):
            total += float(self.timeout_seconds)
            if i + 1 < attempts and backoffs:
                total += float(backoffs[min(i, len(backoffs) - 1)])
        return total


@dataclass(frozen=True)
class Inflight:
    op: str = ""
    symbol: str = ""
    since: str = ""


def policy_from_config(cfg: Optional[dict[str, Any]]) -> HttpPolicy:
    """Named defaults, overridden by ``http.*`` and clamped under the RTH stale limit.

    The supervisor restarts a child whose heartbeat is older than
    ``heartbeat.stale_after_seconds_rth`` (default 90). One read, retries
    included, stays under 60% of that and under ``http.read_budget_seconds``.
    """
    cfg = cfg or {}
    http = cfg.get("http") or {}
    if not isinstance(http, dict):
        http = {}
    stale = float((cfg.get("heartbeat") or {}).get("stale_after_seconds_rth", 90) or 90)
    # Stay well under the watchdog (60% of 90s is 54s). The named budget, 50s,
    # is tighter and still fits 3x15s plus backoff (46.5s).
    ceiling = min(
        float(http.get("read_budget_seconds", DEFAULT_READ_BUDGET_SECONDS) or DEFAULT_READ_BUDGET_SECONDS),
        max(5.0, stale * 0.6),
    )
    timeout = float(http.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS) or DEFAULT_TIMEOUT_SECONDS)
    attempts = int(http.get("read_attempts", DEFAULT_READ_ATTEMPTS) or 1)
    raw_backoff = http.get("read_backoff_seconds", list(DEFAULT_READ_BACKOFF_SECONDS))
    if isinstance(raw_backoff, (int, float)):
        backoff = (float(raw_backoff),)
    else:
        backoff = tuple(float(x) for x in (raw_backoff or ()))
    return _clamp_policy(
        HttpPolicy(
            timeout_seconds=timeout,
            read_attempts=max(1, attempts),
            read_backoff_seconds=backoff,
            read_budget_seconds=ceiling,
        )
    )


def _clamp_policy(policy: HttpPolicy) -> HttpPolicy:
    ceiling = max(1.0, float(policy.read_budget_seconds))
    timeout = min(max(1.0, float(policy.timeout_seconds)), ceiling)
    attempts = max(1, int(policy.read_attempts))
    backoff = tuple(max(0.0, float(x)) for x in policy.read_backoff_seconds)
    while attempts > 1:
        trial = HttpPolicy(timeout, attempts, backoff, ceiling)
        if trial.worst_case_seconds() <= ceiling + 1e-9:
            break
        attempts -= 1
    if HttpPolicy(timeout, attempts, backoff, ceiling).worst_case_seconds() > ceiling + 1e-9:
        timeout = ceiling
        attempts = 1
    return HttpPolicy(timeout, attempts, backoff, ceiling)


def set_inflight_hook(hook: Optional[Callable[[], None]]) -> None:
    """Called when an HTTP call starts and when it returns. Cheap heartbeat writer."""
    global _hook
    _hook = hook


def current_inflight() -> Inflight:
    with _lock:
        if not _stack:
            return Inflight()
        op, symbol, since = _stack[-1]
        return Inflight(op=op, symbol=symbol, since=since)


@contextmanager
def symbol_hint(symbol: str) -> Iterator[None]:
    """Name the order behind a POST body the URL does not carry."""
    token = _symbol_hint.set(str(symbol or ""))
    try:
        yield
    finally:
        _symbol_hint.reset(token)


def push_inflight(op: str, symbol: str) -> int:
    since = datetime.now(timezone.utc).isoformat()
    with _lock:
        _stack.append((str(op or "request"), str(symbol or ""), since))
        token = len(_stack)
    _fire_hook()
    return token


def pop_inflight(token: int) -> None:
    with _lock:
        if _stack and len(_stack) == token:
            _stack.pop()
        elif _stack:
            _stack.pop()
    _fire_hook()


def _fire_hook() -> None:
    hook = _hook
    if hook is None:
        return
    try:
        hook()
    except Exception:
        log.debug("inflight heartbeat hook failed", exc_info=True)


def is_transport_failure(exc: BaseException) -> bool:
    """True when a poll tick died because the HTTP call timed out or disconnected."""
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    cause = exc.__cause__
    if cause is not None and cause is not exc:
        return is_transport_failure(cause)
    return False


def describe_call(method: str, url: str, params: Any = None) -> tuple[str, str]:
    path = urlparse(str(url)).path.lower()
    op = _op_from_path(str(method).upper(), path)
    symbol = _symbol_from_params(params) or _symbol_hint.get()
    return op, symbol


def _op_from_path(method: str, path: str) -> str:
    if "by_client_order_id" in path:
        return "get_order_by_client_id"
    if path.rstrip("/").endswith("/orders") and method == "POST":
        return "submit_order"
    if "/orders/" in path and method == "DELETE":
        return "cancel_order"
    if "/orders/" in path and method == "GET":
        return "get_order"
    if path.rstrip("/").endswith("/orders") and method == "GET":
        return "get_orders"
    if "/positions" in path:
        return "get_positions"
    if path.rstrip("/").endswith("/account"):
        return "get_account"
    if "options/contracts" in path:
        return "get_option_contracts"
    if "stocks" in path and "/bars" in path:
        return "get_stock_bars"
    if "snapshots" in path:
        return "get_option_snapshot"
    if "options" in path and "quotes" in path:
        return "get_option_quotes"
    if "options" in path and "/bars" in path:
        return "get_option_bars"
    tail = path.strip("/").split("/")[-1] or "request"
    return f"{method.lower()}:{tail}"


def _symbol_from_params(params: Any) -> str:
    if params is None:
        return ""
    mapping: Any = params
    if isinstance(params, str):
        parsed = parse_qs(params)
        mapping = {key: values[0] if len(values) == 1 else ",".join(values) for key, values in parsed.items()}
    if not isinstance(mapping, dict):
        return ""
    for key in ("symbols", "symbol", "underlying_symbols"):
        raw = mapping.get(key)
        if not raw:
            continue
        if isinstance(raw, (list, tuple)):
            raw = ",".join(str(item) for item in raw)
        text = str(raw)
        if text.count(",") >= 4:
            return f"{text.count(',') + 1}symbols"
        return text[:80]
    return ""


def _can_spend_another(started: float, policy: HttpPolicy) -> bool:
    elapsed = time.monotonic() - started
    return elapsed + float(policy.timeout_seconds) <= float(policy.read_budget_seconds) + 1e-9


def _sleep_backoff(policy: HttpPolicy, attempt: int, started: float) -> None:
    backoffs = policy.read_backoff_seconds or ()
    if not backoffs:
        return
    delay = float(backoffs[min(attempt, len(backoffs) - 1)])
    remaining = float(policy.read_budget_seconds) - (time.monotonic() - started)
    delay = min(delay, max(0.0, remaining))
    if delay > 0:
        time.sleep(delay)


def install_http_bounds(session: object, policy: HttpPolicy) -> None:
    """Force ``timeout`` on ``session.request``. Retry GET/HEAD only, inside the budget."""
    if getattr(session, _BOUNDED, False):
        return
    original = session.request
    timeout = (float(policy.timeout_seconds), float(policy.timeout_seconds))

    def request(method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not kwargs.get("timeout"):
            kwargs["timeout"] = timeout
        op, symbol = describe_call(str(method), str(url), kwargs.get("params"))
        idempotent = str(method).upper() in {"GET", "HEAD"}
        attempts = policy.read_attempts if idempotent else 1
        started = time.monotonic()
        last_exc: Optional[BaseException] = None
        response: Any = None
        for attempt in range(attempts):
            if attempt and not _can_spend_another(started, policy):
                break
            token = push_inflight(op, symbol)
            try:
                response = original(method, url, *args, **kwargs)
                last_exc = None
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                response = None
            finally:
                pop_inflight(token)
            if last_exc is not None:
                if attempt + 1 >= attempts or not _can_spend_another(started, policy):
                    log.warning(
                        "http %s %s failed after %d attempt(s): %s",
                        str(method).upper(),
                        op,
                        attempt + 1,
                        type(last_exc).__name__,
                    )
                    raise last_exc
                log.info(
                    "http read retry %s attempt %d/%d (%s)",
                    op,
                    attempt + 2,
                    attempts,
                    type(last_exc).__name__,
                )
                _sleep_backoff(policy, attempt, started)
                continue
            status = getattr(response, "status_code", None)
            if (
                idempotent
                and status in _READ_RETRY_STATUSES
                and attempt + 1 < attempts
                and _can_spend_another(started, policy)
            ):
                log.info("http read retry %s attempt %d/%d (HTTP %s)", op, attempt + 2, attempts, status)
                _sleep_backoff(policy, attempt, started)
                continue
            return response
        if last_exc is not None:
            raise last_exc
        return response

    session.request = request  # type: ignore[method-assign]
    setattr(session, _BOUNDED, True)


def bind_rest_client(client: object, policy: HttpPolicy, *, limit_data: bool) -> None:
    """Disable alpaca-py's own retry loop and install the bounded session wrapper.

    The SDK retries POST on HTTP 504 (``DEFAULT_RETRY_EXCEPTION_CODES``). That
    is a blind resubmit. Reads retry here instead, and only GETs.
    """
    if hasattr(client, "_retry"):
        client._retry = 0
    if limit_data:
        from alpaca_options_credit.market_data_limit import install_market_data_client

        install_market_data_client(client)
    session = getattr(client, "_session", None)
    if session is not None:
        install_http_bounds(session, policy)
