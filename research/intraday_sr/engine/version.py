"""Engine spec stamp.

The harness records the commit that actually ran. This module only names
the spec revision.

spec_doc v1.3.5 (8504fd7c19136141a32746234b63dd084106369d).
engine_spec v1.3.5.
"""

from __future__ import annotations

ENGINE_SPEC = "v1.3.5"


def engine_stamp() -> dict[str, str]:
    """Return the engine spec label. No commit sha lives here."""
    return {"engine_spec": ENGINE_SPEC}
