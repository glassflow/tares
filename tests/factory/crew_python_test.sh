#!/usr/bin/env bash
# Runs crew_test.sh on the Python crew engine (TR-1155): the same scenarios, with the factory CLI under
# test being `python -m tares.crew` and TARES_FACTORY_ENGINE=python, so a command not yet ported fails
# with exit 2 instead of quietly running in bash.
#
# Three kit-update scenarios wait for the port of the kit-update round (TR-965). Until it merges they are
# the only FAIL lines allowed; any other FAIL, or a non-zero exit with no FAIL line, fails this suite.
# When TR-965 lands, empty KNOWN_FAIL and the wrapper becomes a plain run.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
out=$(mktemp)
trap 'rm -f "$out"' EXIT

KNOWN_FAIL=(
  "FAIL one quiet station moves per round (a cron doesn't count)"
  "FAIL then the next, after half an hour of waiting on a person"
  "FAIL in the same conversation, told why"
)

PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}" FACTORY_BIN="${PYTHON:-python3} -m tares.crew" \
  TARES_FACTORY_ENGINE=python bash "$here/crew_test.sh" >"$out" 2>&1
rc=$?
cat "$out"

unexpected=0
while IFS= read -r line; do
  known=0
  for k in "${KNOWN_FAIL[@]}"; do [ "$line" = "$k" ] && known=1; done
  if [ $known = 0 ]; then echo "crew_python: unexpected $line"; unexpected=1; fi
done < <(grep '^FAIL ' "$out")

if [ $unexpected = 1 ]; then exit 1; fi
if [ $rc != 0 ] && ! grep -q '^FAIL ' "$out"; then
  echo "crew_python: crew_test.sh exited $rc without a FAIL line"
  exit 1
fi
echo "crew_python: only the known kit-update scenarios (TR-965) may fail; the rest pass"
exit 0
