#!/usr/bin/env bash
# Runs claude_update_test.sh with the Python crew engine on its own (TR-1156): the factory CLI under test is
# `python -m tares.crew` with TARES_FACTORY_ENGINE unset, so each command runs in Python when it is ported
# and in bash when it is not. The suite's fake `ps` sits on PATH, which psutil cannot see, so it hands the
# engine its files through TARES_FAKE_PS_DIR (claude_update_test.sh sets it).
#
# `crew update` and the Crew page's retry flags (cc_retry_sync) are not ported yet (TR-964), so the all-Python
# form (TARES_FACTORY_ENGINE=python) is not run here. When TR-964 lands, run it instead and drop this wrapper.
# Any FAIL line, or a non-zero exit, fails this suite.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
out=$(mktemp)
trap 'rm -f "$out"' EXIT

PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}" FACTORY_BIN="${PYTHON:-python3} -m tares.crew" \
  bash "$here/claude_update_test.sh" >"$out" 2>&1
rc=$?
cat "$out"

if grep -q '^ *FAIL' "$out"; then
  echo "claude_update_mixed: a FAIL line above"
  exit 1
fi
if [ $rc != 0 ]; then
  echo "claude_update_mixed: claude_update_test.sh exited $rc"
  exit 1
fi
echo "claude_update_mixed: claude_update_test.sh passes on the Python engine in mixed mode"
exit 0
