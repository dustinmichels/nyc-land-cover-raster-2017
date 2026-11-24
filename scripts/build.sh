#!/usr/bin/env bash
# Clear out/ (after confirmation), then resample the full dataset at 2, 3, 4, 8, and 16 ft.
# Usage: scripts/build.sh
set -euo pipefail

echo "WARNING: this deletes everything in out/ and runs the full resample (2, 3, 8, 16 ft)."
ls out 2>/dev/null | sed 's/^/  out\//' || true
read -r -p "Press Enter to continue, or n to cancel: " reply
case "$reply" in
[nN]*)
  echo "Cancelled."
  exit 0
  ;;
esac
find out -mindepth 1 -delete 2>/dev/null || true
uv run resample.py 2 3 8 16
