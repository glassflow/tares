#!/usr/bin/env bash
# Checks factory-heavy: two commands never overlap, and the timeout kills.
set -uo pipefail
H="$(cd "$(dirname "$0")/../.." && pwd)/claude-plugin/bin/factory-heavy"
export TMPDIR; TMPDIR=$(mktemp -d)
log="$TMPDIR/log"
"$H" 30 bash -c "echo start-a >> $log; sleep 2; echo end-a >> $log" &
for _ in $(seq 100); do [ -s "$TMPDIR/tares-factory-heavy.lock/pid" ] && break; sleep 0.1; done # a holds the lock
"$H" 30 bash -c "echo start-b >> $log; echo end-b >> $log"
wait
order=$(tr '\n' ' ' < "$log")
fail=0
[ "$order" = "start-a end-a start-b end-b " ] || { echo "FAIL overlap: $order"; fail=1; }
"$H" 1 sleep 5 2>/dev/null; [ $? -eq 142 ] || { echo "FAIL timeout did not kill"; fail=1; }
[ ! -d "$TMPDIR/tares-factory-heavy.lock" ] || { echo "FAIL lock left behind"; fail=1; }
[ $fail = 0 ] && echo "factory-heavy: serialises and times out"
exit $fail
