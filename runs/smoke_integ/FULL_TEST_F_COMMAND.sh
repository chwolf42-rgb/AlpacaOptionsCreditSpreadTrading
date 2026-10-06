#!/usr/bin/env bash
# GB5 launch for FORMATIONS (--test F). Do not run from a cloud agent.
# spec_doc v1.3.5 (8504fd7c19136141a32746234b63dd084106369d)
# engine_spec v1.3.5
# Tag: interim_F1. Pass-2 workers: 2. Signals are kept.
#
# Required env:
#   EXPECT_HEAD            git HEAD that must be checked out
#   FROZEN_ENGINE_COMMIT   engine/ and grids.py must match this commit (no diff)
#   MIN_AVAIL_MB           MemAvailable floor, in MB
# Optional:
#   WORKERS                pass-1 workers (default 2)
#   OUT                    harness --out (default /workspace/research4/runs)
set -euo pipefail

: "${EXPECT_HEAD:?set EXPECT_HEAD}"
: "${FROZEN_ENGINE_COMMIT:?set FROZEN_ENGINE_COMMIT}"
: "${MIN_AVAIL_MB:?set MIN_AVAIL_MB}"

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

SPEC_DOC="v1.3.5"
SPEC_DOC_COMMIT="8504fd7c19136141a32746234b63dd084106369d"
ENGINE_SPEC="v1.3.5"
GRID_SHA256="2ef95123c015d70a1751f559f21fa1249d0b08aea272319e232f2facb120aa22"
N_PROGRAM="456"

echo "spec_doc=${SPEC_DOC} spec_doc_commit=${SPEC_DOC_COMMIT} engine_spec=${ENGINE_SPEC}"

head="$(git rev-parse HEAD)"
if [[ "$head" != "$EXPECT_HEAD" ]]; then
  echo "HEAD ${head} != EXPECT_HEAD ${EXPECT_HEAD}" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "working tree is not clean" >&2
  exit 1
fi
git diff --exit-code "$FROZEN_ENGINE_COMMIT" -- research/intraday_sr/engine research/intraday_sr/grids.py

python - <<PY
from research.intraday_sr.grids import GRID_SHA256
from research.intraday_sr.harness.config import N_PROGRAM
assert GRID_SHA256 == "${GRID_SHA256}", GRID_SHA256
assert N_PROGRAM == ${N_PROGRAM}, N_PROGRAM
PY

avail_kb="$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')"
need_kb=$((15 * 1024 * 1024))
if (( avail_kb < need_kb )); then
  echo "disk free ${avail_kb} KB < 15 GB" >&2
  exit 1
fi
avail_mb="$(awk '/MemAvailable:/ {print int($2/1024)}' /proc/meminfo)"
if (( avail_mb < MIN_AVAIL_MB )); then
  echo "MemAvailable ${avail_mb} MB < MIN_AVAIL_MB ${MIN_AVAIL_MB}" >&2
  exit 1
fi

exec python -m research.intraday_sr.harness.run \
  --test F \
  --tag interim_F1 \
  --symbols available \
  --keep-signals \
  --workers "${WORKERS:-2}" \
  --pass2-workers 2 \
  --out "${OUT:-/workspace/research4/runs}"
