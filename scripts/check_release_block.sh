#!/usr/bin/env bash
# Refuse a final release while RELEASE_BLOCK exists at the repo root. A release candidate
# (a tag with -rc.N) is fine.
#
#   scripts/check_release_block.sh v1.2.3
#
# The root is the folder above this script's own; RELEASE_BLOCK_ROOT overrides it.
set -euo pipefail

TAG="${1:-}"
if [ -z "$TAG" ]; then
  echo "usage: scripts/check_release_block.sh <tag>   e.g. v1.2.3" >&2
  exit 2
fi
ROOT="${RELEASE_BLOCK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

if [ -f "$ROOT/RELEASE_BLOCK" ] && [[ "$TAG" != *-rc.* ]]; then
  echo "::error::$TAG is a final release and the file RELEASE_BLOCK exists: $(head -n 1 "$ROOT/RELEASE_BLOCK")"
  exit 1
fi
exit 0
