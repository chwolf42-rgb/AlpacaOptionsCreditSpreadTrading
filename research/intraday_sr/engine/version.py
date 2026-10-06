"""Engine spec stamp.

The harness records the commit that actually ran. This module only names
the spec revision.
"""

from __future__ import annotations

ENGINE_SPEC = "v1.3.3"


def engine_stamp() -> dict[str, str]:
    """Return the engine spec label. No commit sha lives here."""
    return {"engine_spec": ENGINE_SPEC}
