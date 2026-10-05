"""Thin import shim for Developer 2's `research.intraday_sr.grids` (S0, PR #20): the SINGLE source of every frozen
constant (SPEC v1.3.1 C3 / CP0 R5). The harness reads PRIMARY_GUARDRAIL, COMPARISON_GUARDRAILS, FIXED and
GRID_SHA256 only through this module and redefines none of them.

grids.py absent -> GridsUnavailable (loud). The ONLY exception is the harness test suite on this branch before #20
lands: `research/intraday_sr/conftest.py` sets INTRADAY_SR_GRIDS_STANDIN=1, which loads the test-only stand-in
`research/intraday_sr/tests/_grids_standin.py` (mirrors the CP0-ruled #20 constants). A real grids.py always wins.
When #20 lands, delete the stand-in branch below and import grids directly.
"""
from __future__ import annotations

import importlib
import os
from functools import lru_cache
from types import ModuleType

STANDIN_ENV = "INTRADAY_SR_GRIDS_STANDIN"
REQUIRED = ("PRIMARY_GUARDRAIL", "COMPARISON_GUARDRAILS", "FIXED", "GRID_SHA256", "TEST_A", "TEST_B", "FORMATIONS",
            "OPTIONS", "OPTIONS_0DTE", "N_TRIALS")


class GridsUnavailable(ImportError):
    pass


@lru_cache(maxsize=1)
def grids() -> ModuleType:
    try:
        mod = importlib.import_module("research.intraday_sr.grids")
        source = "grids.py"
    except ImportError as e:
        if os.environ.get(STANDIN_ENV) != "1":
            raise GridsUnavailable(
                "research/intraday_sr/grids.py (S0, PR #20) is required: it is the single source of PRIMARY_GUARDRAIL, "
                f"COMPARISON_GUARDRAILS, FIXED and GRID_SHA256 (SPEC v1.3.1 C3). Import failed: {e}") from e
        mod = importlib.import_module("research.intraday_sr.tests._grids_standin")
        source = "TEST STAND-IN (tests/_grids_standin.py; #20 not merged)"
    missing = [k for k in REQUIRED if not hasattr(mod, k)]
    if missing:
        raise GridsUnavailable(f"grids.py lacks {missing} (CP0 ruling (d)/R5: PRIMARY_GUARDRAIL = "
                               "{'daily_losses': 2, 'weekly_losses': 5}, COMPARISON_GUARDRAILS, FIXED, GRID_SHA256)")
    mod.__harness_source__ = source
    return mod


def source() -> str:
    return grids().__harness_source__


def grid_sha256() -> str:
    """The full canonical grids.GRID_SHA256 (R5/R6), logged in the manifest and every ledger row."""
    return str(grids().GRID_SHA256)


def fixed(key: str):
    f = grids().FIXED
    if key not in f:
        raise GridsUnavailable(f"grids.FIXED has no {key!r} (R5: grids.py is the single source of frozen constants)")
    return f[key]


def _losses(g: dict, key: str):
    if not isinstance(g, dict):
        raise GridsUnavailable(f"guardrail must be a dict with daily_losses/weekly_losses, got {g!r}")
    bad = set(g) - {"name", "daily_losses", "weekly_losses"}
    if bad:
        raise GridsUnavailable(f"unknown guardrail keys {sorted(bad)} (CP0: daily_losses / weekly_losses)")
    v = g.get(key)
    return None if v is None else int(v)


def primary_guardrail() -> tuple[int, int]:
    g = grids().PRIMARY_GUARDRAIL
    d, w = _losses(g, "daily_losses"), _losses(g, "weekly_losses")
    if d is None or w is None:
        raise GridsUnavailable(f"PRIMARY_GUARDRAIL needs daily_losses and weekly_losses, got {g!r}")
    return d, w


def comparison_guardrails() -> tuple[tuple[str, int | None, int | None], ...]:
    out = []
    for g in grids().COMPARISON_GUARDRAILS:
        if "name" not in g:
            raise GridsUnavailable(f"COMPARISON_GUARDRAILS entry without a name: {g!r}")
        out.append((str(g["name"]), _losses(g, "daily_losses"), _losses(g, "weekly_losses")))
    return tuple(out)


# O1 scaffold rows. Not part of REQUIRED and not part of GRID_SHA256: Developer 2's grids bump
# owns the hash. The test stand-in carries them until that bump; a grids.py without them fails here.
_OVERLAY_ATTRS = ("OPTIONS_BASELINE_O1", "OPTIONS_A2_O1", "N_PROGRAM_O1")


def overlay_scaffold() -> tuple[tuple, tuple, int]:
    """Return (baseline rows, A2 rows, program N) for the dual-expiry overlay.

    SPEC v1.3.2 O1: 3 structures × 2 books = 6 baseline, 9 stop×TP × 2 books = 18 A2,
    program N = 456. These constants are read from the grids module when it has them.
    They must not be folded into the hashed grid document by the harness.
    """
    g = grids()
    missing = [name for name in _OVERLAY_ATTRS if not hasattr(g, name)]
    if missing:
        raise GridsUnavailable(
            "O1 overlay constants " + ", ".join(missing) + " are not on this grids module. "
            "The test stand-in (tests/_grids_standin.py) carries OPTIONS_BASELINE_O1, "
            "OPTIONS_A2_O1 and N_PROGRAM_O1 until Developer 2 bumps grids.py and GRID_SHA256. "
            "Do not add them to the hashed document in the harness.")
    base, a2 = tuple(g.OPTIONS_BASELINE_O1), tuple(g.OPTIONS_A2_O1)
    n = int(g.N_PROGRAM_O1)
    if len(base) != 6 or len(a2) != 18:
        raise GridsUnavailable(f"O1 rows must be 6 baseline + 18 A2, got {len(base)} + {len(a2)}")
    if n != 456:
        raise GridsUnavailable(f"N_PROGRAM_O1 must be 456 (192+192+48+24), got {n}")
    return base, a2, n
