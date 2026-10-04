"""Engine entry points.

S0 returns causal 5m pivots, a padded zone, and a k_confirm=0 signal so
the lookahead gate is not an empty comparison. D2-2 and D2-3 replace the
bodies with the full spec stack.
"""

from research.intraday_sr.engine.formations import formations_at
from research.intraday_sr.engine.levels import levels_at
from research.intraday_sr.engine.signals import signals
from research.intraday_sr.engine.zones import zones_at

__all__ = ["formations_at", "levels_at", "signals", "zones_at"]
