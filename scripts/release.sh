#!/usr/bin/env bash
# Cut a release: bump the package version, commit + tag. The compose files track :latest, so there
# is no pinned tag to bump here. Pushing the tag triggers CI (.github/workflows/docker-publish.yml)
# to build and publish ghcr.io/glassflow/tares:<version> (+ :<major>.<minor> and :latest).
#
#   scripts/release.sh 0.0.2
#   git push && git push origin v0.0.2     # then publish (review first)
set -euo pipefail

VERSION="${1:-}"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$ ]]; then
  echo "usage: scripts/release.sh <version>   e.g. 0.0.2, or 0.0.2-rc.1 for a release candidate" >&2
  exit 1
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# a final release is held while RELEASE_BLOCK exists; the same check runs in CI on the tag
bash "$ROOT/scripts/check_release_block.sh" "v$VERSION"

# collect changelog.d/*.md (and anything under [Unreleased]) into a new CHANGELOG.md section; a bad
# fragment stops here, before the version bump, any commit or tag. CHANGELOG_SUMMARY is the summary paragraph.
python3 "$ROOT/scripts/changelog_collect.py" "$VERSION" "$(date +%F)" "${CHANGELOG_SUMMARY:-}" --root "$ROOT"

# package version
sed -i.bak -E "s/^version = \".*\"/version = \"$VERSION\"/" "$ROOT/pyproject.toml"
rm -f "$ROOT/pyproject.toml.bak"

if [ -d "$ROOT/changelog.d" ]; then git -C "$ROOT" add -A -- changelog.d; fi
git -C "$ROOT" add -- CHANGELOG.md pyproject.toml
git -C "$ROOT" commit -m "Release v$VERSION"
git -C "$ROOT" tag "v$VERSION"

echo
echo "Prepared v$VERSION (committed + tagged). Review, then publish:"
echo "  git push && git push origin v$VERSION"
