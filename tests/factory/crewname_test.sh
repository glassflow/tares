#!/usr/bin/env bash
# Checks a crew's name (TR-541): the default crew keeps today's names, a named crew puts its name in every
# station and message target, two crews on one machine run side by side without a clash, the name comes from
# FACTORY_CREW_NAME then crew.json, a Tares shared by several people asks for it once, and the roster says
# which crew it is and warns when the orchestrator's session title is not the name messages go to. Uses a
# fake claude and throwaway homes only.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib.sh"
T=$(mktemp -d)
mkdir -p "$T/kit-under-test"; cp -R "$here/../../claude-plugin/." "$T/kit-under-test/"; F="$T/kit-under-test/bin/factory"
export HOME="$T/home" FACTORY_HOME="$T/fhome" FACTORY_CREW_HOME="$T/crew-default" FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
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

# The name is read before anything else, so a roster of an empty home already says which crew it is.
check "default crew: roster names it" '[ "$(FACTORY_CREW_HOME=$T/empty "$F" roster 2>&1 | head -1)" = "crew: crew (orchestrator: crew-orchestrator)" ]'
check "FACTORY_CREW_NAME=a: roster names a-orchestrator" '[ "$(FACTORY_CREW_HOME=$T/empty FACTORY_CREW_NAME=a "$F" roster 2>&1 | head -1)" = "crew: a (orchestrator: a-orchestrator)" ]'
# A name that cannot be a station name is refused before it names anything.
for bad in Bad_Name -x UPPER "a b" x-build-y build "$(printf 'a%.0s' $(seq 32))"; do
  FACTORY_CREW_HOME=$T/empty FACTORY_CREW_NAME="$bad" "$F" roster >"$T/bad.out" 2>&1; rc=$?
  check "crew name '$bad' is refused" '[ $rc -ne 0 ] && grep -q "is not valid: it is 1 to 31 characters" "$T/bad.out"'
done
l31=$(printf 'a%.0s' $(seq 31)); first=$(FACTORY_CREW_HOME=$T/empty FACTORY_CREW_NAME=$l31 "$F" roster 2>&1 | head -1)
check "a 31 character name is accepted" '[ "$first" = "crew: $l31 (orchestrator: $l31-orchestrator)" ]'

start_tares "$T/tares"
D=$FACTORY_CREW_HOME; trust "$D"
"$F" crew up --yes >"$T/up-default.out" 2>&1
check "default crew: up exits 0" 'grep -q "$TARESD_URL/crew" "$T/up-default.out"'
for r in orchestrator reviewer; do
  check "default crew: crew-$r is recorded" '[ "$(st $D crew-$r .status)" = active ]'
  check "default crew: crew-$r env FACTORY_CREW" '[ "$(envof $D crew-$r FACTORY_CREW)" = crew ]'
  check "default crew: crew-$r env FACTORY_ORCHESTRATOR" '[ "$(envof $D crew-$r FACTORY_ORCHESTRATOR)" = crew-orchestrator ]'
done
check "default crew: no crew_name is stored, one person" 'jq -e "has(\"crew_name\") | not" $D/crew.json >/dev/null'
check "default crew: orchestrator started first, under today's name" '[ "$(jq -r "select(.args[0] == \"--bg\") | .args[2]" "$FAKE_CLAUDE_STATE/calls.jsonl" | head -1)" = crew-orchestrator ]'
check "default crew: roster names it" '[ "$("$F" roster 2>&1 | head -1)" = "crew: crew (orchestrator: crew-orchestrator)" ]'
repo="$T/shop"; mkdir -p "$repo"; git -C "$repo" init -q; repo=$(git -C "$repo" rev-parse --show-toplevel)
hand_over shop "$repo" >/dev/null
"$F" spawn builder shop m1 "$repo" "work T1" >/dev/null 2>&1
check "default crew: a builder is <project>-build-<slug>" '[ "$(st $D shop-build-m1 .role)" = builder ]'
check "default crew: its parent is crew-orchestrator" '[ "$(envof $D shop-build-m1 FACTORY_PARENT)" = crew-orchestrator ]'
check "default crew: its env carries the crew" '[ "$(envof $D shop-build-m1 FACTORY_CREW)" = crew ] && [ "$(envof $D shop-build-m1 FACTORY_ORCHESTRATOR)" = crew-orchestrator ]'
"$F" spawn helper crew-reviewer probe "look" >/dev/null 2>&1
check "default crew: a helper keeps its suffix rule" '[ "$(st $D crew-reviewer-probe .parent)" = crew-reviewer ]'
check "default crew: the boot prompt sends READY to crew-orchestrator" 'jq -e "select(.args[2] == \"shop-build-m1\") | .args | any(contains(\"send crew-orchestrator READY\"))" "$FAKE_CLAUDE_STATE/calls.jsonl" >/dev/null'
n_default=$(launches)

# Two crews on one machine, one Tares, one fake Claude: each in its own home, each its own names.
A=$T/crew-a; B=$T/crew-b; trust "$A" "$B"
FACTORY_CREW_HOME=$A FACTORY_CREW_NAME=a "$F" crew up --yes >"$T/up-a.out" 2>&1
FACTORY_CREW_HOME=$B FACTORY_CREW_NAME=b "$F" crew up --yes >"$T/up-b.out" 2>&1
for pair in "a $A" "b $B"; do
  set -- $pair; c=$1; H=$2
  for r in orchestrator reviewer; do
    check "crew $c: $c-$r is recorded and active" '[ "$(st $H $c-$r .status)" = active ] && [ "$(st $H $c-$r .role)" = $r ]'
    check "crew $c: $c-$r runs in its own crew home" '[ "$(st $H $c-$r .cwd)" = "$H" ]'
    check "crew $c: $c-$r env FACTORY_STATION" '[ "$(envof $H $c-$r FACTORY_STATION)" = $c-$r ]'
    check "crew $c: $c-$r env FACTORY_CREW" '[ "$(envof $H $c-$r FACTORY_CREW)" = $c ]'
    check "crew $c: $c-$r env FACTORY_ORCHESTRATOR" '[ "$(envof $H $c-$r FACTORY_ORCHESTRATOR)" = $c-orchestrator ]'
  done
  check "crew $c: the name is kept in crew.json" '[ "$(jq -r .crew_name $H/crew.json)" = $c ]'
  check "crew $c: no station of the default crew is recorded" '[ -z "$(st $H crew-orchestrator .status)" ] && [ -z "$(st $H crew-reviewer .status)" ]'
  check "crew $c: roster names the crew" '[ "$(FACTORY_CREW_HOME=$H "$F" roster 2>&1 | head -1)" = "crew: $c (orchestrator: $c-orchestrator)" ]'
  check "crew $c: roster lists its orchestrator working" 'FACTORY_CREW_HOME=$H "$F" roster 2>&1 | grep -q "^$c-orchestrator	orchestrator		working$"'
done
check "both crews and the default one run: no station was started twice" '[ "$(launches)" = $((n_default + 4)) ]'
check "the fake Claude holds six sessions with six different names" '[ "$(jq -r "[.[] | select(.name | test(\"^(crew|a|b)-(orchestrator|reviewer)$\")) | .name] | unique | length" "$FAKE_CLAUDE_STATE/agents.json")" = 6 ]'
# A second crew up without the variable keeps the stored name; a different one is refused, not quietly applied.
n=$(launches)
FACTORY_CREW_HOME=$A "$F" crew up --yes >"$T/up-a2.out" 2>&1
check "crew a: up again starts nothing and keeps the name" '[ "$(launches)" = "$n" ] && [ "$(jq -r .crew_name $A/crew.json)" = a ]'
FACTORY_CREW_HOME=$A FACTORY_CREW_NAME=zzz "$F" crew up --yes >"$T/up-a3.out" 2>&1; rc=$?
check "crew a: another FACTORY_CREW_NAME is refused" '[ $rc -ne 0 ] && grep -q "this crew is named a" "$T/up-a3.out" && [ "$(jq -r .crew_name $A/crew.json)" = a ] && [ "$(launches)" = "$n" ]'
# crew.json alone names the crew (no environment variable), and the environment variable wins over a crew with none stored.
check "crew.json names the crew with no variable" '[ "$(FACTORY_CREW_HOME=$A "$F" settings a-reviewer reviewer | jq -r .env.FACTORY_CREW)" = a ]'

# A builder of crew a is a-<project>-build-<slug>, started by a-orchestrator; crew b's builder of the same project is b-.
"$F" spawn builder shop m1 "$repo" "work T1" >/dev/null 2>&1
FACTORY_CREW_HOME=$A "$F" spawn builder shop m1 "$repo" "work T1" >/dev/null 2>&1
FACTORY_CREW_HOME=$B "$F" spawn builder shop m1 "$repo" "work T1" >/dev/null 2>&1
check "crew a: the builder is a-shop-build-m1" '[ "$(st $A a-shop-build-m1 .role)" = builder ] && [ -z "$(st $A shop-build-m1 .role)" ]'
check "crew a: its parent is a-orchestrator" '[ "$(st $A a-shop-build-m1 .parent)" = a-orchestrator ] && [ "$(envof $A a-shop-build-m1 FACTORY_PARENT)" = a-orchestrator ]'
check "crew a: its env carries the crew and the orchestrator" '[ "$(envof $A a-shop-build-m1 FACTORY_CREW)" = a ] && [ "$(envof $A a-shop-build-m1 FACTORY_ORCHESTRATOR)" = a-orchestrator ] && [ "$(envof $A a-shop-build-m1 FACTORY_STATION)" = a-shop-build-m1 ]'
check "crew a: its boot prompt sends READY to a-orchestrator" 'jq -e "select(.args[2] == \"a-shop-build-m1\") | .args | any(contains(\"send a-orchestrator READY\"))" "$FAKE_CLAUDE_STATE/calls.jsonl" >/dev/null'
check "crew b: the same project builds as b-shop-build-m1, beside a's" '[ "$(st $B b-shop-build-m1 .role)" = builder ] && [ "$(jq -r "[.[] | select(.name | test(\"^(a|b)-shop-build-m1$\"))] | length" "$FAKE_CLAUDE_STATE/agents.json")" = 2 ]'
check "the default crew keeps its builder name beside them" '[ "$(st $D shop-build-m1 .role)" = builder ] && [ "$(jq -r "[.[] | select(.name == \"shop-build-m1\")] | length" "$FAKE_CLAUDE_STATE/agents.json")" = 1 ]'
check "crew a: roster lists its builder and not b's" 'FACTORY_CREW_HOME=$A "$F" roster 2>&1 | grep -q "^a-shop-build-m1	builder	shop	working$" && ! FACTORY_CREW_HOME=$A "$F" roster 2>&1 | grep -q "b-shop-build-m1"'
FACTORY_CREW_HOME=$A "$F" spawn helper a-reviewer probe "look" >/dev/null 2>&1
check "crew a: a helper of a-reviewer keeps its suffix rule and parent" '[ "$(st $A a-reviewer-probe .parent)" = a-reviewer ] && [ "$(st $A a-reviewer-probe .role)" = helper ]'
check "crew a: a helper of a-reviewer is told to load station-reviewer" 'jq -e "select(.args[2] == \"a-reviewer-probe\") | .args | any(contains(\"station-reviewer\") and contains(\"send a-reviewer DONE\"))" "$FAKE_CLAUDE_STATE/calls.jsonl" >/dev/null'
hand_over a-tools "$repo" >/dev/null
FACTORY_CREW_HOME=$A "$F" spawn builder a-tools m1 "$repo" x >"$T/a-tools.out" 2>&1
check "crew a: a project can not be named like the crew" 'grep -q "can.t start with \"a\"" "$T/a-tools.out" && [ -z "$(st $A a-tools-build-m1 .role)" ]'

# The releaser is only started when the crew asks for one; record a stand-in so the helper path can run.
tmp=$(mktemp); jq --arg h "$A" '.["a-releaser"] = {role: "releaser", status: "active", project: "", repo: "", cwd: $h, model: "", effort: ""}' "$A/stations.json" > "$tmp" && mv "$tmp" "$A/stations.json"
FACTORY_CREW_HOME=$A "$F" spawn releaser-helper probe "look" >/dev/null 2>&1
check "crew a: a releaser helper hangs under a-releaser" '[ "$(st $A a-releaser-probe .parent)" = a-releaser ] && [ "$(st $A a-releaser-probe .role)" = helper ]'
# A builder session the crew did not record is adopted into the crew whose name it carries, never into another's.
tmp=$(mktemp); jq --arg c "$repo" '. + [{id: "0000a002", name: "a-shop-build-m2", cwd: $c, state: "working", sessionId: "s-a002", startedAt: 98, pid: 4242}, {id: "0000b009", name: "b-shop-build-m9", cwd: $c, state: "working", sessionId: "s-b009", startedAt: 99, pid: 4243}]' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"
FACTORY_CREW_HOME=$A "$F" roster >/dev/null 2>&1
check "crew a: an unrecorded a-shop-build-m2 is adopted, under a-orchestrator" '[ "$(st $A a-shop-build-m2 .role)" = builder ] && [ "$(st $A a-shop-build-m2 .parent)" = a-orchestrator ] && [ "$(st $A a-shop-build-m2 .project)" = shop ]'
check "crew a: b's unrecorded builder is not adopted" '[ -z "$(st $A b-shop-build-m9 .role)" ]'
# Any command, not only roster, refuses a crew name that is not valid.
FACTORY_CREW_HOME=$T/empty FACTORY_CREW_NAME=Bad_Name "$F" status >"$T/bad-status.out" 2>&1; rc=$?
check "a command other than roster refuses an invalid crew name" '[ $rc -ne 0 ] && grep -q "is not valid: it is 1 to 31 characters" "$T/bad-status.out"'

# The hooks follow the crew: a helper of a-reviewer is a reviewer, a helper of a builder is a builder.
G="$here/../../claude-plugin/hooks/guard.sh"
hook() { # parent command crew: deny or allow, for a helper under that parent
  local out; out=$(jq -n --arg c "$2" '{tool_name: "Bash", tool_input: {command: $c}}' | FACTORY_ROLE=helper FACTORY_CREW=$3 FACTORY_PARENT=$1 bash "$G")
  if grep -q '"deny"' <<<"$out"; then echo deny; else echo allow; fi
}
check "guard: a helper of a-releaser acts as a releaser (named crew)" '[ "$(hook a-releaser "gh pr merge 3" a)" = deny ] && [ "$(hook a-releaser "kubectl --context kind-factory rollout undo deploy/app" a)" = allow ]'
check "guard: a helper of a-shop-build-m1 acts as a builder" '[ "$(hook a-shop-build-m1 "factory restart a-reviewer" a)" = deny ]'
check "guard: the default crew still works with no FACTORY_CREW" '[ "$(hook crew-releaser "gh pr merge 3" "")" = deny ]'
check "guard: a helper whose name has -build- under crew-releaser is still a releaser's" '[ "$(hook crew-releaser-build-x "gh pr merge 3" "")" = deny ] && [ "$(hook a-releaser-build-x "gh pr merge 3" a)" = deny ]'

start() { # parent crew: the skill a helper's session is told to load
  echo '{"source": "startup"}' | FACTORY_ROLE=helper FACTORY_STATION=h FACTORY_CREW=$2 FACTORY_PARENT=$1 bash "$here/../../claude-plugin/hooks/session-start.sh" | jq -r '.hookSpecificOutput.additionalContext' | grep -o 'station-[a-z]*'
}
check "session start: a helper of a-reviewer loads station-reviewer (named crew)" '[ "$(start a-reviewer a)" = station-reviewer ] && [ "$(start a-shop-build-m1 a)" = station-builder ] && [ "$(start crew-releaser "")" = station-releaser ]'
check "session start: a helper whose name has -build- under crew-reviewer loads station-reviewer" '[ "$(start crew-reviewer-build-x "")" = station-reviewer ] && [ "$(start a-reviewer-build-x a)" = station-reviewer ]'

# The roster warns when the orchestrator's session carries another title than the name messages go to.
id=$(jq -r '.id' <<<"$(jq -c --arg n a-orchestrator '.[] | select(.name == $n)' "$FAKE_CLAUDE_STATE/agents.json" | head -1)")
tmp=$(mktemp); jq --arg id "$id" 'map(if .id == $id then .name = "orchestrator-of-a" else . end)' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"
FACTORY_CREW_HOME=$A "$F" roster >"$T/roster-title.out" 2>&1
check "roster warns of a different orchestrator session title" 'grep -qF "Orchestrator session title is orchestrator-of-a, expected a-orchestrator: messages to it will not arrive" "$T/roster-title.out"'
FACTORY_CREW_HOME=$B "$F" roster >"$T/roster-b.out" 2>&1
check "roster stays quiet when the title is the name" '! grep -q "Orchestrator session title" "$T/roster-b.out"'

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

# The plugin sends the crew's name with every MCP call, next to the station headers.
check "the plugin sends X-Tares-Crew from FACTORY_CREW" '[ "$(jq -r ".mcpServers.tares.headers[\"X-Tares-Crew\"]" "$here/../../claude-plugin/.mcp.json")" = "\${FACTORY_CREW:-}" ]'

[ $fail = 0 ] && echo "crewname: all cases pass"
exit $fail
