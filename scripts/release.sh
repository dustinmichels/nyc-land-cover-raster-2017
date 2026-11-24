#!/usr/bin/env bash
# Tag HEAD and push the tag; the Release workflow publishes the committed raster.
# Usage: scripts/release.sh [tag]   (default tag: vYYYY.MM.DD)
set -euo pipefail

file=out/nyc_landcover_2017_3ft.tif
tag=${1:-v$(date +%Y.%m.%d)}

[[ -f $file ]] || { echo "error: $file not found; run: mise run resample" >&2; exit 1; }
git ls-files --error-unmatch "$file" >/dev/null 2>&1 \
  || { echo "error: $file is not committed; the workflow publishes the committed copy" >&2; exit 1; }
git diff --quiet HEAD -- "$file" \
  || { echo "error: $file has uncommitted changes" >&2; exit 1; }

git tag -a "$tag" -m "Release $tag"
git push origin "$tag"
echo "Pushed $tag; the Release workflow will publish $file"
