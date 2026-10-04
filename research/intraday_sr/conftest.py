"""Harness test bootstrap (Developer 1). Until PR #20 lands, grids.py is absent on research/intraday-sr-harness;
this lets harness/s0grids.py load the TEST-ONLY stand-in for the test run. A real grids.py always takes precedence.
Delete together with tests/_grids_standin.py once #20 is merged."""
import os

os.environ.setdefault("INTRADAY_SR_GRIDS_STANDIN", "1")
