#!/usr/bin/env bash
# Clear out/ (after confirmation), then resample the full dataset at the given resolutions.
# Usage: scripts/build.sh RES_FT [RES_FT ...]   (resolutions are set in mise.toml's build task)
set -euo pipefail

if [ $# -eq 0 ]; then
  echo "Usage: $0 RES_FT [RES_FT ...]" >&2
  exit 2
fi

echo "WARNING: this deletes everything in out/ and runs the full resample ($* ft)."
ls out 2>/dev/null | sed 's/^/  out\//' || true
read -r -p "Press Enter to continue, or n to cancel: " reply
case "$reply" in
[nN]*)
  echo "Cancelled."
  exit 0
  ;;
esac
find out -mindepth 1 -delete 2>/dev/null || true
uv run resample.py "$@"
