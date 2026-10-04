"""Issue a holdout token after checking that a FREEZE file exists.

Developer 1's walk-forward runner is the only caller. This function does
not score the holdout.
"""

from __future__ import annotations

from pathlib import Path

from research.intraday_sr.data.holdout import HoldoutLocked, HoldoutToken


def run_holdout(freeze: str | Path) -> HoldoutToken:
    """Return a token when ``freeze`` is an existing FREEZE file."""
    path = Path(freeze)
    if not path.is_file():
        raise HoldoutLocked(f"FREEZE file is missing: {path}")
    return HoldoutToken._from_freeze(path)
