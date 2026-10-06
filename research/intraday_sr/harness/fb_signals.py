"""Harness adapters for --test F and --test B (SPEC v1.3.5, gate GB2).

Developer 2 owns detection and signal construction (F1–F10, B1–B3). This module does not
detect patterns. It calls one engine function per test and rejects the S0 stub instead of
turning it into a silent zero-trade run.

Engine entry points Developer 2 will provide
---------------------------------------------
Formations (``--test F``), one function for every kind. The kind, tolerance, timeframe and
target travel on the grid row, not on a new config type::

    research.intraday_sr.engine.formations.formation_signals(
        bars: BarSet,
        start: datetime,
        end: datetime,
        cfg: EngineCfg,
        variant: Mapping,
    ) -> Iterator[Signal]

``variant`` is one ``grids.FORMATIONS`` row: ``test`` (``F_W`` | ``F_IHS`` | ``F_M`` | ``F_HS``),
``kind`` (``W`` | ``IHS`` | ``M`` | ``HS``), ``entry_tf``, ``target``, ``pivot_tol_atr``,
``variant_id``. ``cfg`` is ``EngineCfg(k_zones=5)`` (formations zone target; passed explicitly,
not left to the dataclass default). K is not a formation axis, and ``EngineCfg`` has no
``pivot_tol`` field, so the tolerance stays on ``variant``.

Test B (``--test B``)::

    research.intraday_sr.engine.signals.test_b_signals(
        bars: BarSet,
        start: datetime,
        end: datetime,
        cfg: EngineCfg,
        sig: SignalCfg,
    ) -> Iterator[Signal]

``sig.test`` is ``"B"``. The other ``SignalCfg`` fields match Test A (``oscillator``,
``rvol_min``, ``entry_tf``, ``target``, ``k_confirm``, ``variant_id``). ``cfg`` is
``EngineCfg(k_zones=variant["K"])``, the same mapping Test A uses. Do not route Test B through
``engine.signals``: that function returns an empty iterator when ``sig.test != "A"``.

P1 (formations, not signals) is separate. See ``harness/p1_prescreen.py`` for
``engine.formations.formations_in``.

Signal fields the harness requires
----------------------------------
Both tests return frozen ``Signal`` objects. The harness does not rebuild triggers or stops.

* ``symbol``, ``tf`` (the variant's ``entry_tf``), ``variant_id``, ``direction`` (+1 for W/IHS,
  −1 for M/HS), ``trigger``, ``stop``, ``expires_at``, ``as_of_ts``, ``available_at``.
* ``targets`` includes ``1R`` and ``2R``. On a ``target == "zone"`` variant the engine emits a
  signal only when an opposite zone in the K=5 cache exists and is at least 1R from the
  trigger (the same rule as ``engine/signals.py`` for Test A). Otherwise it drops the signal.
  Target values are never ``None``. Pass 1 drops a zone-target signal that has no finite
  ``targets["zone"]`` so pass 2 does not abort.
* ``zone.atr_d`` is finite and ``> 0`` (F10 prior-session ATR). Pass 1 checks it.
* Formations-alone set ``zone.score = 0.0``. Same-bar entry ties then break by symbol A to Z.
  Test B keeps the stack zone's score, as Test A does.
* ``formation`` is a ``Formation``, never None. ``formation.kind`` matches the variant on F.
  ``formation.available_at`` is the break bar's close and must be strictly before
  ``signal.available_at`` (F8: the retest is after the break). Equality is rejected.
  ``confirmed_ts <= break_ts <= formation.available_at`` and
  ``signal.as_of_ts <= signal.available_at < signal.expires_at``.
* Test B (B2): ``formation.zone_id == signal.zone.zone_id``,
  ``signal.tf == variant["entry_tf"] == formation.tf``, and
  ``formation.symbol == signal.symbol``.
* ``formation.invalidation`` is the pattern extreme for both F and B (min of the pattern lows
  for longs, max of the highs for shorts). It is not the zone. The compact spill stores it
  as ``f_inval`` and pass 2 cancels on that level.
* Formations-alone use ``confluence=0`` and no oscillator/MACD/RVOL flags. Test B confluence
  is the stack's optional-condition count, same as Test A.

``formations_at`` returning ``[]`` is the stub. A missing entry point raises ``StubEngineError``
even when ``--allow-empty`` is set. ``--allow-empty`` only lets a real entry point that found
nothing continue, and it is for fixtures.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from datetime import datetime

from research.intraday_sr.types import BarSet, EngineCfg, Signal

FORMATION_SIGNALS = "research.intraday_sr.engine.formations.formation_signals"
TEST_B_SIGNALS = "research.intraday_sr.engine.signals.test_b_signals"
FORMATIONS_IN = "research.intraday_sr.engine.formations.formations_in"
FORMATION_K_ZONES = 5          # formations zone target. Explicit; not EngineCfg's default.

_LONG = frozenset({"W", "IHS"})
_SHORT = frozenset({"M", "HS"})


class StubEngineError(RuntimeError):
    """The F/B engine entry point is missing, so the S0 stub must not become zero trades."""


class EngineContractError(RuntimeError):
    """A signal came back, but it does not match the F/B contract above."""


def _load(module: str, name: str, message: str):
    import importlib
    mod = importlib.import_module(module)
    fn = getattr(mod, name, None)
    if not callable(fn):
        raise StubEngineError(message)
    return fn


def formation_signals_fn():
    return _load(
        "research.intraday_sr.engine.formations",
        "formation_signals",
        "engine.formations.formation_signals is not defined. formations_at() is the S0 stub and "
        "returns []. Refusing to run --test F: a stub must not silently produce zero trades. "
        f"Expected {FORMATION_SIGNALS}(bars: BarSet, start: datetime, end: datetime, "
        "cfg: EngineCfg, variant: Mapping) -> Iterator[Signal].",
    )


def test_b_signals_fn():
    return _load(
        "research.intraday_sr.engine.signals",
        "test_b_signals",
        "engine.signals.test_b_signals is not defined. engine.signals.signals() returns an empty "
        "iterator when SignalCfg.test != 'A' (the pre-D2-4 stub). Refusing to run --test B on "
        f"that empty iterator. Expected {TEST_B_SIGNALS}(bars: BarSet, start: datetime, "
        "end: datetime, cfg: EngineCfg, sig: SignalCfg) -> Iterator[Signal], with sig.test == 'B'.",
    )


def engine_cfg_for_variant(variant: Mapping) -> EngineCfg:
    """K maps to k_zones for Test A/B. Formations pass k_zones=5 explicitly (zone target)."""
    if "K" in variant and variant["K"] not in (None, ""):
        return EngineCfg(k_zones=int(variant["K"]))
    return EngineCfg(k_zones=FORMATION_K_ZONES)


def _direction(kind: str) -> int:
    if kind in _LONG:
        return 1
    if kind in _SHORT:
        return -1
    raise EngineContractError(f"formation kind {kind!r} is not W, IHS, M, or HS")


def _finite_zone_target(signal: Signal) -> bool:
    targets = signal.targets
    if "zone" not in targets:
        return False
    value = targets["zone"]
    return value is not None and math.isfinite(float(value))


def _require_atr(signal: Signal, vid: str) -> None:
    value = getattr(signal.zone, "atr_d", None)
    if value is None or not math.isfinite(float(value)) or float(value) <= 0.0:
        raise EngineContractError(f"{vid}: zone.atr_d must be finite and > 0, got {value!r}")


def _require_stamps(signal: Signal, vid: str) -> None:
    """F8: the break bar's close is strictly before the signal's decision bar."""
    form = signal.formation
    if form is None:
        raise EngineContractError(f"{vid}: formation is None")
    if not (form.confirmed_ts <= form.break_ts <= form.available_at):
        raise EngineContractError(
            f"{vid}: need confirmed_ts <= break_ts <= formation.available_at, "
            f"got {form.confirmed_ts}, {form.break_ts}, {form.available_at}"
        )
    if not (form.available_at < signal.available_at):
        raise EngineContractError(
            f"{vid}: formation.available_at {form.available_at} must be strictly before "
            f"signal.available_at {signal.available_at} (F8 retest is after the break bar)"
        )
    if not (signal.as_of_ts <= signal.available_at < signal.expires_at):
        raise EngineContractError(
            f"{vid}: need as_of_ts <= available_at < expires_at, "
            f"got {signal.as_of_ts}, {signal.available_at}, {signal.expires_at}"
        )


def _reject_late_formation(signal: Signal) -> None:
    """Consume signal.formation through Guard at the signal's decision bar."""
    from research.intraday_sr.harness.guard import Guard
    _require_stamps(signal, signal.variant_id)
    Guard(signal.available_at).check(signal.formation, decision_ts=signal.available_at)


def validate_f_signal(signal: Signal, variant: Mapping) -> None:
    form = signal.formation
    if form is None:
        raise EngineContractError(
            f"{variant.get('variant_id')}: formation is None. That is the Test A / stub shape, "
            "not a formations signal."
        )
    vid = variant["variant_id"]
    if signal.test != variant["test"] or signal.variant_id != vid:
        raise EngineContractError(
            f"{vid}: signal test/variant_id {signal.test}/{signal.variant_id} "
            f"!= {variant['test']}/{vid}"
        )
    if signal.tf != variant["entry_tf"] or form.tf != signal.tf or form.kind != variant["kind"]:
        raise EngineContractError(
            f"{vid}: tf/kind {signal.tf}/{getattr(form, 'kind', None)} "
            f"!= {variant['entry_tf']}/{variant['kind']}"
        )
    if form.symbol != signal.symbol:
        raise EngineContractError(f"{vid}: formation symbol {form.symbol} != {signal.symbol}")
    if int(signal.direction) != _direction(form.kind):
        raise EngineContractError(f"{vid}: direction {signal.direction} does not match kind {form.kind}")
    if float(signal.zone.score) != 0.0:
        raise EngineContractError(
            f"{vid}: formations-alone zone.score must be 0.0 so same-bar ties break by symbol A-Z, "
            f"got {signal.zone.score!r}"
        )
    _require_atr(signal, vid)
    if str(variant.get("target")) == "zone" and not _finite_zone_target(signal):
        raise EngineContractError(f"{vid}: zone-target variant signal has no finite targets['zone']")
    _reject_late_formation(signal)


def validate_b_signal(signal: Signal, variant: Mapping) -> None:
    form = signal.formation
    vid = variant["variant_id"]
    if form is None:
        raise EngineContractError(
            f"{vid}: Test B signal has formation=None. engine.signals() for test != 'A' is the stub."
        )
    if signal.test != "B" or signal.variant_id != vid:
        raise EngineContractError(f"{vid}: signal test/variant_id {signal.test}/{signal.variant_id} != B/{vid}")
    if not form.zone_id:
        raise EngineContractError(f"{vid}: B2 requires formation.zone_id to be set")
    if str(form.zone_id) != str(signal.zone.zone_id):
        raise EngineContractError(
            f"{vid}: formation.zone_id {form.zone_id} != signal.zone.zone_id {signal.zone.zone_id}"
        )
    if signal.tf != variant["entry_tf"] or form.tf != signal.tf:
        raise EngineContractError(
            f"{vid}: need signal.tf == entry_tf == formation.tf, "
            f"got {signal.tf}, {variant['entry_tf']}, {form.tf}"
        )
    if form.symbol != signal.symbol:
        raise EngineContractError(f"{vid}: formation symbol {form.symbol} != {signal.symbol}")
    if int(signal.direction) != _direction(form.kind):
        raise EngineContractError(f"{vid}: direction {signal.direction} does not match kind {form.kind}")
    _require_atr(signal, vid)
    if str(variant.get("target")) == "zone" and not _finite_zone_target(signal):
        raise EngineContractError(f"{vid}: zone-target variant signal has no finite targets['zone']")
    _reject_late_formation(signal)


def signals_for_f(bars: BarSet, start: datetime, end: datetime, variant: Mapping) -> list[Signal]:
    """Map one formation variant to signals via ``formation_signals``. Empty is allowed here;
    the run aborts later if a whole portfolio is empty, unless ``--allow-empty``."""
    fn = formation_signals_fn()
    out = []
    for signal in fn(bars, start, end, engine_cfg_for_variant(variant), variant):
        if str(variant.get("target")) == "zone" and not _finite_zone_target(signal):
            continue
        validate_f_signal(signal, variant)
        out.append(signal)
    return out


def signals_for_b(bars: BarSet, start: datetime, end: datetime, variant: Mapping) -> list[Signal]:
    """Map one Test B variant to signals via ``test_b_signals``."""
    from research.intraday_sr.types import SignalCfg
    fn = test_b_signals_fn()
    sig = SignalCfg(
        oscillator=variant["oscillator"],
        rvol_min=float(variant["rvol_min"]),
        entry_tf=variant["entry_tf"],
        target=variant["target"],
        k_confirm=int(variant["k_confirm"]),
        variant_id=variant["variant_id"],
        test="B",
    )
    out = []
    for signal in fn(bars, start, end, engine_cfg_for_variant(variant), sig):
        if str(variant.get("target")) == "zone" and not _finite_zone_target(signal):
            continue
        validate_b_signal(signal, variant)
        out.append(signal)
    return out


def iter_signals_for(bars: BarSet, start: datetime, end: datetime, variant: Mapping) -> Iterator[Signal]:
    """Dispatch used by the real adapter. Test A does not come through here."""
    test = str(variant.get("test") or "")
    if test.startswith("F"):
        return iter(signals_for_f(bars, start, end, variant))
    if test == "B":
        return iter(signals_for_b(bars, start, end, variant))
    raise EngineContractError(f"iter_signals_for is only for F and B, got {test!r}")
