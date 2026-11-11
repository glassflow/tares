#!/usr/bin/env bash
# Runs claude_update_test.sh with the Python crew engine on its own (TR-1156, TR-1157): the factory CLI under
# test is `python -m tares.crew` with TARES_FACTORY_ENGINE=python, so a command that is not ported fails the
# suite instead of falling back to bash. The suite's fake `ps` sits on PATH, which psutil cannot see, so it
# hands the engine its files through TARES_FAKE_PS_DIR, and its fake `sort` runs a hook the engine reaches
# through the same folder (claude.fake_ps_step). Any FAIL line, or a non-zero exit, fails this suite.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
out=$(mktemp)
trap 'rm -f "$out"' EXIT

PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}" TARES_FACTORY_ENGINE=python FACTORY_BIN="${PYTHON:-python3} -m tares.crew" \
  bash "$here/claude_update_test.sh" >"$out" 2>&1
rc=$?
cat "$out"

if grep -q '^ *FAIL' "$out"; then
  echo "claude_update_python: a FAIL line above"
  exit 1
fi
if [ $rc != 0 ]; then
  echo "claude_update_python: claude_update_test.sh exited $rc"
  exit 1
fi
echo "claude_update_python: claude_update_test.sh passes on the Python engine"
exit 0
