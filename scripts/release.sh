#!/usr/bin/env bash
# Bump the citation version in README.md (committed), tag HEAD, push, and
# upload every out/nyc_landcover_2017_*ft.tif as a GitHub release asset.
# The rasters live only in the release, never in git.
# Usage: scripts/release.sh [tag]
#   default tag: vYYYY.MM.DD, or vYYYY.MM.DD.N (N = 1, 2, …) if that day is taken
set -euo pipefail

shopt -s nullglob
files=(out/nyc_landcover_2017_*ft.tif)
shopt -u nullglob
((${#files[@]})) || { echo "error: no out/nyc_landcover_2017_*ft.tif; run: mise run resample" >&2; exit 1; }
command -v gh >/dev/null || { echo "error: gh (GitHub CLI) not found" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "error: gh is not logged in; run: gh auth login" >&2; exit 1; }
[[ -z $(git status --porcelain) ]] \
  || { echo "error: working tree is dirty; commit or stash first" >&2; git status --short >&2; exit 1; }
git fetch -q --tags origin || { echo "error: could not fetch tags from origin" >&2; exit 1; }
if [[ -n ${1:-} ]]; then
  tag=$1
  git rev-parse -q --verify "refs/tags/$tag" >/dev/null \
    && { echo "error: tag $tag already exists; pass a different tag" >&2; exit 1; }
else
  base=v$(date +%Y.%m.%d)
  tag=$base
  n=0
  while git rev-parse -q --verify "refs/tags/$tag" >/dev/null; do
    tag=$base.$((++n))
  done
fi

echo "Will publish to $tag:"
ls -lh "${files[@]}" | awk '{print "  " $5 "\t" $NF}'

# Rewrite the `version = {…}` line of the README BibTeX citation and the
# `Version … GitHub,` text of the Chicago citation to $tag, then commit.
# Never aborts the release: returns non-zero with a reason on stderr instead.
readme=README.md
committed=
update_readme() {
  local pat='^([[:space:]]*version[[:space:]]*=[[:space:]]*\{)[^}]*(\})'
  local esc
  grep -Eq "$pat" "$readme" || { echo "no 'version = {…}' line found" >&2; return 1; }
  esc=$(printf '%s' "$tag" | sed 's/[&|\\]/\\&/g')
  sed -E -e "s|$pat|\\1$esc\\2|" -e "s|(Version )[^ ]+(\\. GitHub,)|\\1$esc\\2|" "$readme" >"$readme.tmp" && mv "$readme.tmp" "$readme" \
    || { rm -f "$readme.tmp"; echo "rewrite failed" >&2; return 1; }
  if git diff --quiet -- "$readme"; then
    echo "note: $readme version already $tag"
    return 0
  fi
  git commit -q -m "Set README citation version to $tag" -- "$readme" \
    || { git checkout -- "$readme"; echo "git commit failed" >&2; return 1; }
  committed=1
  echo "note: updated $readme version to $tag and committed it"
}
update_readme || echo "note: could not update $readme version; releasing without it" >&2

git tag -a "$tag" -m "Release $tag"
if [[ -n $committed ]]; then
  git push --atomic origin HEAD "refs/tags/$tag"
else
  git push origin "$tag"
fi

# "1 ft, 3 ft, 6 ft" in numeric order (the glob sorts 15 before 3).
res_list=$(printf '%s\n' "${files[@]}" | sed -E 's/.*_([0-9.]+)ft\.tif$/\1/' | sort -n \
  | sed 's/$/ ft/' | paste -sd, - | sed 's/,/, /g')
gh release create "$tag" "${files[@]}" --verify-tag \
  --title "NYC Land Cover 2017 ($tag)" \
  --notes "Majority-resampled GeoTIFFs of the NYC 2017 6-inch land cover raster at $res_list. EPSG:2263, uint8, nodata=0. See README for class values." \
  || { echo "error: upload failed; $tag is pushed. Retry with: gh release upload $tag ${files[*]} --clobber (after gh release create $tag --verify-tag if no release exists)" >&2; exit 1; }
echo "Published $tag"
