#!/usr/bin/env bash
# GB5 launch for Test B (--test B), only after P1 does not screen it out (GB4).
# Do not run from a cloud agent.
# spec_doc v1.3.5 (8504fd7c19136141a32746234b63dd084106369d)
# engine_spec comes from engine/version.py engine_stamp() and must be v1.3.5.
# Tag: interim_B1. Pass-2 workers: 2. Signals are kept.
#
# Required env:
#   P1_JSON    p1.json with label "not screened out by P1" and screened_out false
# Replace EXPECT_HEAD with the GB3 GO harness sha before launch.
# Optional env: WORKERS (default 2), MIN_AVAIL_MB (default 7000), OUT.
set -euo pipefail

REPO=/workspace/research4/wt_integ
PY=/workspace/research4/intraday_sr/.venv/bin/python
OUT_ROOT=${OUT:-/workspace/research4/runs}
TAG=interim_B1
EXPECT_GRID_SHA=2ef95123c015d70a1751f559f21fa1249d0b08aea272319e232f2facb120aa22
EXPECT_HEAD=${EXPECT_HEAD:-ddbc8a8e93d3e2e2a7103e547bd477661b91a91b}
ENGINE_COMMIT=${FROZEN_ENGINE_COMMIT:-30c20eb3b9c1f5f4ddedc1820ce3d719a6b29595}
MIN_AVAIL_MB=${MIN_AVAIL_MB:-7000}
LIVE=/workspace/AlpacaOptionsCreditSpreadTrading

: "${P1_JSON:?set P1_JSON to the P1 report}"

cd "$REPO"
export PYTHONPATH="$REPO"
top="$(git rev-parse --show-toplevel)"
if [[ "$top" == "$LIVE" ]]; then
  echo "refusing to run in the live checkout $LIVE" >&2
  exit 1
fi
git diff --quiet && git diff --cached --quiet || { echo "worktree not clean; refusing"; exit 1; }
[ "$(git rev-parse HEAD)" = "$EXPECT_HEAD" ] || { echo "HEAD $(git rev-parse HEAD) != $EXPECT_HEAD"; exit 1; }
git merge-base --is-ancestor "$ENGINE_COMMIT" HEAD || { echo "engine $ENGINE_COMMIT is not an ancestor of HEAD"; exit 1; }
git diff --quiet "$ENGINE_COMMIT" HEAD -- research/intraday_sr/engine research/intraday_sr/grids.py \
  || { echo "engine/ or grids.py differs from $ENGINE_COMMIT; refusing"; exit 1; }
GS=$("$PY" -c "from research.intraday_sr import grids; print(grids.GRID_SHA256)")
[ "$GS" = "$EXPECT_GRID_SHA" ] || { echo "GRID_SHA256 $GS != $EXPECT_GRID_SHA"; exit 1; }
NP=$("$PY" -c "from research.intraday_sr.harness.config import N_PROGRAM; print(N_PROGRAM)")
[ "$NP" = "456" ] || { echo "harness N_PROGRAM $NP != 456"; exit 1; }
"$PY" - "$P1_JSON" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
ok = p.get("label") == "not screened out by P1" and p.get("screened_out") is False
if not ok:
    raise SystemExit(f"P1 does not clear Test B: label={p.get('label')!r} screened_out={p.get('screened_out')!r}")
print("p1", p.get("label"))
PY
[ -e "$OUT_ROOT/$TAG/manifest.json" ] && { echo "$OUT_ROOT/$TAG already has a finished run; pick a new tag"; exit 1; }
[ -e "$OUT_ROOT/$TAG/run.log" ] && { echo "$OUT_ROOT/$TAG/run.log exists (a run started here); refusing"; exit 1; }
if pgrep -f 'research.intraday_sr.harness.run' >/dev/null; then
  echo "another harness run is alive; refusing" >&2
  exit 1
fi
FREE_GB=$(df -BG --output=avail "$OUT_ROOT" | tail -1 | tr -dc 0-9)
[ "$FREE_GB" -ge 15 ] || { echo "only ${FREE_GB} GB free under $OUT_ROOT; need >= 15 GB"; exit 1; }
AVAIL_MB=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
[ "$AVAIL_MB" -ge "$MIN_AVAIL_MB" ] || { echo "MemAvailable ${AVAIL_MB} MB < ${MIN_AVAIL_MB} MB; refusing"; exit 1; }
mkdir -p "$OUT_ROOT/$TAG"

nohup "$PY" -m research.intraday_sr.harness.run \
  --test B \
  --symbols available \
  --tag "$TAG" \
  --out "$OUT_ROOT" \
  --workers "${WORKERS:-2}" \
  --pass2-workers 2 \
  --keep-signals \
  --p1-json "$P1_JSON" \
  > "$OUT_ROOT/$TAG/run.log" 2>&1 &
echo "started pid $! -> $OUT_ROOT/$TAG/run.log (p1 sha recorded in the manifest)"
