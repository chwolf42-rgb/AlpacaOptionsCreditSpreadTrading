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
``variant_id``. ``cfg`` is ``EngineCfg()`` with the default ``k_zones=5``. K is not a formation
axis, and ``EngineCfg`` has no ``pivot_tol`` field, so the tolerance stays on ``variant``.

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
* ``targets`` includes ``1R`` and ``2R`` (``zone`` when a zone target exists). The portfolio
  still applies the Test A target, widen-stop, cap and cost rules to whatever the engine put
  on the signal.
* ``zone`` is a ``Zone`` with finite ``atr_d > 0`` (the stop floor reads only that).
* ``formation`` is a ``Formation``, never None. ``formation.kind`` matches the variant on F.
  ``formation.available_at <= signal.available_at`` (the decision bar). The harness consumes
  ``signal.formation`` through ``Guard.check`` at that decision time. A later ``available_at``
  is lookahead and aborts the run.
* Test B also requires ``formation.zone_id`` set (B2: same zone as the stack touch).
* Formations-alone use ``confluence=0`` and no oscillator/MACD/RVOL flags. Test B confluence
  is the stack's optional-condition count, same as Test A.

``formations_at`` returning ``[]`` is the stub. A missing entry point raises ``StubEngineError``
even when ``--allow-empty`` is set. ``--allow-empty`` only lets a real entry point that found
nothing continue, and it is for fixtures.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime

from research.intraday_sr.types import BarSet, EngineCfg, Signal

FORMATION_SIGNALS = "research.intraday_sr.engine.formations.formation_signals"
TEST_B_SIGNALS = "research.intraday_sr.engine.signals.test_b_signals"
FORMATIONS_IN = "research.intraday_sr.engine.formations.formations_in"

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
    """K maps to k_zones for Test A/B. Formations have no K axis: the EngineCfg default (5)."""
    if "K" in variant and variant["K"] not in (None, ""):
        return EngineCfg(k_zones=int(variant["K"]))
    return EngineCfg()


def _direction(kind: str) -> int:
    if kind in _LONG:
        return 1
    if kind in _SHORT:
        return -1
    raise EngineContractError(f"formation kind {kind!r} is not W, IHS, M, or HS")


def _reject_late_formation(signal: Signal) -> None:
    """Consume signal.formation through Guard at the signal's decision bar."""
    from research.intraday_sr.harness.guard import Guard
    form = signal.formation
    if form is None:
        raise EngineContractError(f"{signal.variant_id} {signal.symbol}: formation is None")
    Guard(signal.available_at).check(form, decision_ts=signal.available_at)


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
    if int(signal.direction) != _direction(form.kind):
        raise EngineContractError(f"{vid}: direction {signal.direction} does not match kind {form.kind}")
    _reject_late_formation(signal)


def signals_for_f(bars: BarSet, start: datetime, end: datetime, variant: Mapping) -> list[Signal]:
    """Map one formation variant to signals via ``formation_signals``. Empty is allowed here;
    the run aborts later if a whole portfolio is empty, unless ``--allow-empty``."""
    fn = formation_signals_fn()
    out = list(fn(bars, start, end, engine_cfg_for_variant(variant), variant))
    for signal in out:
        validate_f_signal(signal, variant)
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
    out = list(fn(bars, start, end, engine_cfg_for_variant(variant), sig))
    for signal in out:
        validate_b_signal(signal, variant)
    return out


def iter_signals_for(bars: BarSet, start: datetime, end: datetime, variant: Mapping) -> Iterator[Signal]:
    """Dispatch used by the real adapter. Test A does not come through here."""
    test = str(variant.get("test") or "")
    if test.startswith("F"):
        return iter(signals_for_f(bars, start, end, variant))
    if test == "B":
        return iter(signals_for_b(bars, start, end, variant))
    raise EngineContractError(f"iter_signals_for is only for F and B, got {test!r}")
