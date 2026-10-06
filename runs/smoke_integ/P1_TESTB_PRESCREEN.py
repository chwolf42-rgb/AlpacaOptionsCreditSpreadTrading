#!/usr/bin/env python3
"""Thin P1 entry. The rule lives in research.intraday_sr.harness.p1_prescreen.

Do not point this at a real A1b cache from a cloud agent. Tests pass a synthetic parquet
and a pickle of Formation objects via --formations.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.intraday_sr.harness.p1_prescreen import main

if __name__ == "__main__":
    main()
