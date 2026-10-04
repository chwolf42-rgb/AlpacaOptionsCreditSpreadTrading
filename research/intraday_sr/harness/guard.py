"""Lookahead guard (SPEC section 3). Always on, including production runs.

Every engine object (Signal, Zone, Formation, Level) is consumed through `Guard`. The simulator advances
`Guard.now` to the decision timestamp (the close, `available_at`, of the bar being processed). Reading any field
of an object whose `available_at` is later than `now` raises `LookaheadError`. Only `available_at` itself (and
`symbol`) may be read early, so the simulator can schedule activation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

_ALWAYS = frozenset({"available_at", "symbol"})


class LookaheadError(RuntimeError):
    """An engine object was consumed before it was available."""


def _available_at(obj: Any) -> datetime:
    try:
        ts = object.__getattribute__(obj, "available_at")
    except AttributeError as exc:
        raise LookaheadError(f"{type(obj).__name__} has no available_at; cannot be consumed") from exc
    if ts is None or getattr(ts, "tzinfo", None) is None:
        raise LookaheadError(f"{type(obj).__name__}.available_at must be tz-aware, got {ts!r}")
    return ts


class Guard:
    def __init__(self, now: datetime | None = None):
        self.now = now
        self.checks = 0

    def advance(self, now: datetime) -> None:
        if self.now is not None and now < self.now:
            raise LookaheadError(f"guard clock moved backwards: {now} < {self.now}")
        self.now = now

    def check(self, obj: Any, decision_ts: datetime | None = None) -> Any:
        ts = decision_ts if decision_ts is not None else self.now
        if ts is None:
            raise LookaheadError("guard has no decision time")
        self.checks += 1
        avail = _available_at(obj)
        if avail > ts:
            raise LookaheadError(
                f"{type(obj).__name__} available_at={avail.isoformat()} consumed at decision_ts={ts.isoformat()}"
            )
        return obj

    def wrap(self, obj: Any) -> "Guarded":
        return Guarded(obj, self)

    def wrap_all(self, objs: Iterable[Any]) -> list["Guarded"]:
        return [Guarded(o, self) for o in objs]


class Guarded:
    """Read-through proxy: every attribute read (except available_at/symbol) is checked against guard.now.
    Nested engine objects that carry available_at (signal.zone, signal.formation) are wrapped too."""

    __slots__ = ("_obj", "_guard")

    def __init__(self, obj: Any, guard: Guard):
        object.__setattr__(self, "_obj", obj)
        object.__setattr__(self, "_guard", guard)

    def __getattr__(self, name: str) -> Any:
        obj = object.__getattribute__(self, "_obj")
        guard = object.__getattribute__(self, "_guard")
        if name not in _ALWAYS:
            guard.check(obj)
        val = getattr(obj, name)
        if val is not None and hasattr(val, "available_at") and not isinstance(val, Guarded):
            return Guarded(val, guard)
        return val

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("guarded engine objects are read-only")

    @property
    def unwrap(self) -> Any:
        """The raw object, after a guard check (for storing on Trade records)."""
        obj = object.__getattribute__(self, "_obj")
        object.__getattribute__(self, "_guard").check(obj)
        return obj
