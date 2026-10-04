"""Double bottoms, double tops, head-and-shoulders, and their events.

Patterns are assembled only from pivots that are already confirmed.
``known_at`` of a formation is the ``known_at`` of its last point.
"""

from __future__ import annotations

import pandas as pd

from research.intraday_sr.levels.config import FormationConfig
from research.intraday_sr.levels.types import (
    Formation,
    FormationEvent,
    LevelCandidate,
    Zone,
)


def detect_formations(
    pivots: list[LevelCandidate],
    *,
    timeframe: str,
    atr: float,
    zones: list[Zone],
    config: FormationConfig,
    symbol: str,
) -> list[Formation]:
    """Formations whose last pivot is the most recently confirmed one.

    The caller passes the confirmed-pivot prefix. This function returns
    only patterns that complete on the last pivot, so replaying a prefix
    cannot see a pattern that needs a later pivot.
    """
    raise NotImplementedError


def update_formation_events(
    formations: list[Formation],
    bar: pd.Series,
    *,
    atr: float,
    config: FormationConfig,
    already: list[FormationEvent],
) -> list[FormationEvent]:
    """Events newly knowable on ``bar``.

    ``bar.name`` is the bar close. A bullish neckline break is a close
    above the neckline. A bearish break is a close below it. A retest is
    a later bar, within ``retest_bars``, that tags the neckline and closes
    back on the break side. Invalidation is a close through the pattern
    extreme before the break, or a close back through the neckline after
    the break.
    """
    raise NotImplementedError
