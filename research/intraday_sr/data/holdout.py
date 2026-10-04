"""Holdout lock. Bars on or after 2026-04-01 need a token from ``run_holdout``."""

from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path

HOLDOUT_START = date(2026, 4, 1)


class HoldoutLocked(RuntimeError):
    """A loader was asked for holdout bars without a token from ``run_holdout``."""


class HoldoutToken:
    """Proof that ``walkforward.run_holdout`` saw a FREEZE file.

    Other modules cannot build one. ``run_holdout`` calls ``_from_freeze``.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        raise HoldoutLocked("only walkforward.run_holdout() can build a holdout token")

    @classmethod
    def _from_freeze(cls, freeze: Path) -> "HoldoutToken":
        caller = inspect.stack()[1]
        if caller.function != "run_holdout":
            raise HoldoutLocked("only walkforward.run_holdout() can build a holdout token")
        path = Path(freeze)
        if not path.is_file():
            raise HoldoutLocked(f"FREEZE file is missing: {path}")
        token = object.__new__(cls)
        token.freeze_path = path.resolve()
        return token


def require_dev_range(start: date, end: date, token: HoldoutToken | None) -> None:
    """Raise when the requested range touches the holdout and no token was passed."""
    if end >= HOLDOUT_START or start >= HOLDOUT_START:
        if not isinstance(token, HoldoutToken):
            raise HoldoutLocked(
                f"bars on or after {HOLDOUT_START.isoformat()} require a holdout token"
            )
