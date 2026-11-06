#!/usr/bin/env bash
# Stage a shoot up to the point where an agent has proofs + review bundles.
#
#   scripts/prepare.sh <shoot-dir> [work-dir] [--fast] [--limit N]
#
#   --fast     culling tier: use the camera's embedded JPEG previews
#              (~0.08 s/frame, ungraded) instead of developing masters
#   --limit N  only the first N frames (smoke test)
#
# Stops before rendering on purpose: a human or agent must review the proofs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "$ROOT"

PHOTOS=""
WORK="work"
FAST=0
LIMIT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fast) FAST=1; shift ;;
    --limit) LIMIT="--limit $2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) if [[ -z "$PHOTOS" ]]; then PHOTOS="$1"; elif [[ "$WORK" == "work" ]]; then WORK="$1"; else
         echo "unexpected argument: $1" >&2; exit 2; fi; shift ;;
  esac
done

if [[ -z "$PHOTOS" || ! -d "$PHOTOS" ]]; then
  echo "usage: $0 <shoot-dir> [work-dir] [--fast] [--limit N]" >&2
  exit 2
fi

pf() { python3 -m photoflow "$@"; }

echo "== 0. preflight: required tools"
for t in darktable-cli magick exiv2; do command -v "$t" >/dev/null || { echo "missing: $t" >&2; exit 1; }; done
python3 -c "import numpy, PIL" || { echo "missing python deps (numpy, Pillow)" >&2; exit 1; }

STYLE="$(python3 -c "import sys;sys.path.insert(0,'$ROOT');from photoflow import styles;print(styles.get_style('$WORK') or '')" 2>/dev/null || true)"
if [[ -n "$STYLE" ]]; then
  echo "== style: $STYLE (from $WORK/settings.json)"
else
  echo "== style: none (darktable defaults; see 'photoflow styles --help')"
fi

echo "== 1. inventory  -> $WORK/manifest.json"
pf inventory --photos "$PHOTOS" --work "$WORK" --bursts

if [[ $FAST -eq 1 ]]; then
  echo "== 2. proofs (embedded preview, culling tier)"
  pf proofs --work "$WORK" --mode embedded --jobs "${PF_JOBS:-4}" $LIMIT
else
  echo "== 2. masters (darktable, review quality)  -> $WORK/masters"
  pf masters --work "$WORK" --format jpg --jobs "${PF_JOBS:-4}" $LIMIT
  echo "== 3. proofs from masters"
  pf proofs --work "$WORK" --from-masters --long-edge 800 --jobs "${PF_JOBS:-4}" $LIMIT
fi

echo "== 4. measurements"
pf measure --work "$WORK" --jobs "${PF_JOBS:-4}"

echo "== 5. contact sheets  -> $WORK/sheets"
pf sheets --work "$WORK" --per-sheet 25 --tile 360 --cols 6

echo "== 6. review bundles  -> $WORK/review"
pf review --work "$WORK" --per-batch "${PF_BATCH:-10}" --deliverable "${PF_DELIVERABLE:-social_4x5}"

echo
echo "READY FOR REVIEW"
echo "  1. read $WORK/sheets/sheet_*.jpg        (tier 0: triage)"
echo "  2. read $WORK/proofs/*.jpg              (tier 1: per-frame, ~800px)"
echo "  3. answer $WORK/review/request_*.prompt.md as strict JSON"
echo "  4. python3 -m photoflow review --work $WORK --ingest <response>.json"
echo "  5. python3 -m photoflow render --work $WORK --edge 400"
echo "  6. python3 -m photoflow render --work $WORK --full"
