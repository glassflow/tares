#!/usr/bin/env bash
# Tares enterprise licence, see ee/LICENSE
# Checks a crew's name on a Tares shared by several people (TR-541): with one person besides local nothing is
# asked; with two the name is asked once, suggesting <first name>-<short hostname>; a typed name is used, a name
# that is not valid is asked again, FACTORY_CREW_NAME skips the question, and a default crew that is already
# running keeps its name when Tares gains a second person. The Tares is started with a test licence
# (ee/tests/licensed.py serve). Uses a fake claude and throwaway homes only.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
. "$repo/tests/factory/lib.sh"
T=$(mktemp -d)
mkdir -p "$T/kit-under-test"; cp -R "$repo/claude-plugin/." "$T/kit-under-test/"; F="$T/kit-under-test/bin/factory"
export HOME="$T/home" FACTORY_HOME="$T/fhome" FACTORY_CREW_HOME="$T/crew-default" FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$repo/tests/factory/fake-claude" FACTORY_PAUSE=0
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
unset FACTORY_CREW_NAME FACTORY_CREW FACTORY_ORCHESTRATOR
unset TARES_URL TARES_AUTH_TOKEN TARES_FACTORY_URL TARES_FACTORY_TOKEN_FILE FACTORY_REPO FACTORY_WORKTREES CLAUDE_JOB_DIR TARES_FACTORY_MCP_URL TARES_FACTORY_CRED_FILE TARES_MCP_HOST TARES_MCP_PORT
# Never write outside this test's own folder: the real crew home and factory home must not be reachable.
case "$FACTORY_CREW_HOME:$FACTORY_HOME" in "$T"/*:"$T"/*) ;; *) echo "FAIL the test would write outside $T"; exit 1 ;; esac
mkdir -p "$HOME"
chmod +x "$CLAUDE_BIN"
trap 'stop_tares; rm -rf "$T"' EXIT
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; fi; }
# st HOME STATION JQ-PATH: a field of a station in one crew's stations.json
st() { jq -r --arg s "$2" ".[\$s]$3 // empty" "$1/stations.json"; }
envof() { jq -r --arg k "$3" '.env[$k] // empty' "$1/stations/$2.settings.json"; }
launches() { grep -c '"--bg"' "$FAKE_CLAUDE_STATE/calls.jsonl" 2>/dev/null || echo 0; }
git config --global user.name "Ashish Bagri"

# The same as start_tares, but the daemon is started the way `tares up` starts it, with a test licence.
start_licensed_tares() { # dir
  local dir=$1 port
  mkdir -p "$dir"
  port=$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
  env TARES_CATALOG="$dir/none.yaml" TARES_OTLP_GRPC_PORT=off HOME="$dir" \
    "$PY" "$repo/ee/tests/licensed.py" serve --port "$port" --data-dir "$dir" >"$dir/taresd.log" 2>&1 &
  TARES_PIDS="$TARES_PIDS $!"
  export TARESD_URL="http://127.0.0.1:$port"
  unset TARES_AUTH_TOKEN
  local _ up=0; for _ in $(seq 150); do curl -sf "$TARESD_URL/health" >/dev/null 2>&1 && { up=1; break; }; sleep 0.2; done
  [ $up = 1 ] || { echo "FAIL Tares did not start on $TARESD_URL"; cat "$dir/taresd.log"; exit 1; }
  start_mcp "$dir" ""
}

start_licensed_tares "$T/tares"
check "the Tares runs under a valid test licence" '[ "$(api GET /api/licence | jq -r "[.valid, .ee_allowed, .source] | join(\",\")")" = true,true,env ]'
D=$FACTORY_CREW_HOME; trust "$D"
"$F" crew up --yes >"$T/up-default.out" 2>&1
check "default crew: up exits 0" 'grep -q "$TARESD_URL/crew" "$T/up-default.out"'
check "default crew: crew-orchestrator is recorded" '[ "$(st $D crew-orchestrator .status)" = active ]'

# A Tares shared by several people asks for the name once, suggesting <first name>-<short hostname>.
api POST /api/people '{"name": "Lena", "email": "lena@example.com"}' >/dev/null
check "one person besides local: nothing is asked" '[ "$(api GET /api/people | jq "[.people[] | select(.id != \"local\")] | length")" = 1 ]'
C0=$T/crew-one; trust "$C0"
FACTORY_CREW_HOME=$C0 "$F" crew up </dev/null >"$T/up-one.out" 2>&1
check "one person: crew up does not ask for a name and keeps crew" '! grep -q "What should this crew be called" "$T/up-one.out" && [ "$(st $C0 crew-orchestrator .status)" = active ] && jq -e "has(\"crew_name\") | not" $C0/crew.json >/dev/null'
api POST /api/people '{"name": "Ravi", "email": "ravi@example.com"}' >/dev/null
C1=$T/crew-two; C2=$T/crew-three; C3=$T/crew-four; trust "$C1" "$C2" "$C3"
FACTORY_CREW_HOME=$C1 "$F" crew up </dev/null >"$T/up-two.out" 2>&1
sugg=$(jq -r .crew_name $C1/crew.json)
check "two people: the name is asked, suggesting <first name>-<short hostname>" 'grep -q "What should this crew be called" "$T/up-two.out" && grep -Eq "^ashish(-[a-z0-9-]+)?$" <<<"$sugg" && grep -q "recommended: $sugg " "$T/up-two.out"'
check "two people: enter takes the suggestion and names the stations with it" '[ "$(st $C1 $sugg-orchestrator .status)" = active ] && [ "$(envof $C1 $sugg-reviewer FACTORY_CREW)" = "$sugg" ]'
printf 'ana-laptop\n' | FACTORY_CREW_HOME=$C2 "$F" crew up >"$T/up-three.out" 2>&1
check "two people: a typed name is used" '[ "$(jq -r .crew_name $C2/crew.json)" = ana-laptop ] && [ "$(st $C2 ana-laptop-orchestrator .status)" = active ]'
printf 'Bad Name\nana-desk\n' | FACTORY_CREW_HOME=$C3 "$F" crew up >"$T/up-four.out" 2>&1
check "two people: a name that is not valid is asked again" 'grep -q "is not one of the choices\|is not a valid crew name" "$T/up-four.out" && [ "$(jq -r .crew_name $C3/crew.json)" = ana-desk ]'
check "two people: FACTORY_CREW_NAME skips the question" 'C4=$T/crew-five; trust "$C4"; FACTORY_CREW_HOME=$C4 FACTORY_CREW_NAME=ravi-pc "$F" crew up </dev/null >"$T/up-five.out" 2>&1; ! grep -q "What should this crew be called" "$T/up-five.out" && [ "$(jq -r .crew_name $C4/crew.json)" = ravi-pc ]'
# A default crew that is already running keeps its name when Tares gains a second person: crew up must not
# start a second orchestrator and reviewer under a new name beside the running ones.
n=$(launches)
FACTORY_CREW_HOME=$D "$F" crew up --yes >"$T/up-d1.out" 2>&1
FACTORY_CREW_HOME=$D "$F" crew up </dev/null >"$T/up-d2.out" 2>&1
check "running default crew: crew up does not ask, rename or start anything" '! grep -q "What should this crew be called" "$T/up-d1.out" "$T/up-d2.out" && jq -e "has(\"crew_name\") | not" $D/crew.json >/dev/null && [ "$(launches)" = "$n" ] && [ "$(jq "[.[] | select(.role == \"orchestrator\")] | length" $D/stations.json)" = 1 ]'
FACTORY_CREW_HOME=$D FACTORY_CREW_NAME=zzz "$F" crew up --yes >"$T/up-d3.out" 2>&1; rc=$?
check "running default crew: another FACTORY_CREW_NAME is refused and nothing is stored" '[ $rc -ne 0 ] && grep -q "already running under the name crew" "$T/up-d3.out" && jq -e "has(\"crew_name\") | not" $D/crew.json >/dev/null && [ "$(launches)" = "$n" ]'


[ $fail = 0 ] && echo "crewname team: all cases pass"
exit $fail
