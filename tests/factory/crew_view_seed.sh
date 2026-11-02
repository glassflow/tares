#!/usr/bin/env bash
# A scratch Tares with a known crew view, to check the Crew page by eye (Mark ended, builders under
# finished projects). Not a CI test: it keeps the daemon running until Ctrl-C.
#   (cd ui && npm run build); bash tests/factory/crew_view_seed.sh
# Projects: seed-open (handed over, open) and seed-done (handed over, finished). Stations:
#   seed-open-build-m1  builder on seed-open, working 1 min ago
#   seed-done-build-m1  builder on seed-done, ended 2 h ago
#   seed-quiet-build    builder on seed-done, working 60 min ago (shows Quiet)
#   seed-stray-build    builder with no project, working 1 min ago
#   crew-orchestrator   orchestrator, working 1 min ago
set -uo pipefail   # no -e: stop_tares waits on daemons killed with a non-zero status
source "$(dirname "$0")/lib.sh"
T=$(mktemp -d)
trap stop_tares EXIT
trap 'exit 0' INT TERM
start_tares "$T/tares"

mkdir -p "$T/repo-open" "$T/repo-done"
OPEN=$(hand_over seed-open "$T/repo-open")
DONE=$(hand_over seed-done "$T/repo-done")
api POST "/api/projects/$DONE/finish" '{"words": "seed"}' >/dev/null

# the source the station lines land in (a 409 when it exists is fine)
api POST /api/sources '{"name":"claude_code","connector":"claude_code","poll":"10s","config":{"push":true}}' >/dev/null || true

ts() { # minutes ago -> the transcript's timestamp format
  "$PY" -c 'import sys, datetime as d; t = d.datetime.now(d.timezone.utc) - d.timedelta(minutes=float(sys.argv[1])); print(t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")' "$1"
}

station() { # session station role project state minutes-ago
  local sid=$1 name=$2 role=$3 project=$4 state=$5 ago=$6 at
  at=$(ts "$ago")
  jq -cn --arg s "$sid" --arg n "$name" --arg r "$role" --arg p "$project" --arg at "$(ts $((ago + 1)))" \
    '{type: "session_station", sessionId: $s, cwd: "/w/crew", timestamp: $at, station: $n, role: $r,
      parent: (if $r == "orchestrator" then "" else "crew-orchestrator" end), factory_station: $n}
     + (if $p == "" then {} else {tares_project: $p} end)'
  jq -cn --arg s "$sid" --arg n "$name" --arg at "$at" \
    '{type: "assistant", sessionId: $s, cwd: "/w/crew", timestamp: $at, factory_station: $n,
      message: {role: "assistant", content: ("seeded line from " + $n)}}'
  jq -cn --arg s "$sid" --arg st "$state" --arg at "$at" \
    '{type: "session_state", sessionId: $s, cwd: "/w/crew", timestamp: $at, state: $st, reason: ""}'
}

{
  station s-orch crew-orchestrator orchestrator "" working 1
  station s-open seed-open-build-m1 builder "$OPEN" working 1
  station s-done seed-done-build-m1 builder "$DONE" ended 120
  station s-quiet seed-quiet-build builder "$DONE" working 60
  station s-stray seed-stray-build builder "" working 1
} | curl -sS -X POST -H 'Content-Type: application/x-ndjson' --data-binary @- "$TARESD_URL/ingest/claude_code" >/dev/null

echo "TARESD_URL=$TARESD_URL"
echo "Console: $TARESD_URL/crew"
echo "Ctrl-C stops it"
while :; do sleep 3600 & wait $!; done
