#!/usr/bin/env bash
# Runs idle_wake_test.sh on the Python crew engine alone (TR-1158): the factory CLI under test is
# `python -m tares.crew` with TARES_FACTORY_ENGINE=python, so a command not yet ported fails with exit 2
# instead of quietly running in bash. The suite's curl shim cannot see the Python client's requests, so the
# engine notes each URL in the file TARES_URL_LOG names (idle_wake_test.sh sets it).
# Any FAIL line, or a non-zero exit, fails this suite.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
out=$(mktemp)
trap 'rm -f "$out"' EXIT

PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}" FACTORY_BIN="${PYTHON:-python3} -m tares.crew" \
  TARES_FACTORY_ENGINE=python bash "$here/idle_wake_test.sh" >"$out" 2>&1
rc=$?
cat "$out"

if grep -q '^ *FAIL' "$out"; then
  echo "idle_wake_python: a FAIL line above"
  exit 1
fi
if [ $rc != 0 ]; then
  echo "idle_wake_python: idle_wake_test.sh exited $rc"
  exit 1
fi
echo "idle_wake_python: idle_wake_test.sh passes on the Python engine"
exit 0
