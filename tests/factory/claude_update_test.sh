#!/usr/bin/env bash
# Checks a new Claude Code reaching the crew (TR-669), on a real Tares with a fake claude and a fake ps:
# the watch moves stations one idle station a round, the orchestrator last, in the same conversation;
# a busy, attached or waiting station is left alone; nothing moves while halted, while the setting is
# off, or while the version flips back and forth; an old pre-warmed spare is stopped and the restart
# tried again; `crew update` and `crew restart` do every station and check there is one session each;
# roster, status and doctor show the versions.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib.sh"
T=$(mktemp -d)
mkdir -p "$T/kit-under-test" "$T/bin" "$T/claude"; cp -R "$here/../../claude-plugin/." "$T/kit-under-test/"; F="$T/kit-under-test/bin/factory"
export HOME="$T/home" FACTORY_CREW_HOME="$T/crew" FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
export FACTORY_UPDATE_POLL=1 FACTORY_ASK_POLL=1 FACTORY_STOP_WAIT=2
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
unset TARES_URL TARES_AUTH_TOKEN TARES_FACTORY_URL TARES_FACTORY_TOKEN_FILE FACTORY_REPO FACTORY_WORKTREES CLAUDE_JOB_DIR TARES_FACTORY_MCP_URL TARES_FACTORY_CRED_FILE TARES_MCP_HOST TARES_MCP_PORT
mkdir -p "$HOME"
chmod +x "$CLAUDE_BIN" "$here/fake-ps"
ln -s "$here/fake-ps" "$T/bin/ps"; export PATH="$T/bin:$PATH"   # the process listing is a stub too
trap 'stop_tares; kill $KEEP 2>/dev/null; rm -rf "$T"' EXIT
KEEP=""
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; fi; }
st() { jq -r --arg s "$1" ".[\$s]$2 // empty" "$FACTORY_CREW_HOME/stations.json"; }
launches() { grep -c '"--bg"' "$FAKE_CLAUDE_STATE/calls.jsonl" 2>/dev/null || echo 0; }
launched() { jq -r 'select(.args[0] == "--bg") | .args[2]' "$FAKE_CLAUDE_STATE/calls.jsonl" | tail -n "$1" | paste -sd' ' -; } # the last n names started
last() { jq -c "select(.args[2] == \"$1\") | .args" "$FAKE_CLAUDE_STATE/calls.jsonl" | tail -1; }
agent() { local tmp; tmp=$(mktemp); jq --arg id "$(st "$1" .id)" "map(if .id == \$id then $2 else . end)" "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"; }
idle() { agent "$1" '.state = "working" | .status = "idle"'; }
busy() { agent "$1" '.state = "working" | .status = "busy"'; }
all_idle() { local s; for s in crew-orchestrator crew-reviewer crew-releaser; do idle $s; done; }
newcc() { echo "$1" > "$FAKE_CLAUDE_STATE/version"; }
runs() { # station → the version its process shows in the listing
  ps -o command= -p "$(ps -o ppid= -p "$(jq -r --arg id "$(st "$1" .id)" '.[] | select(.id == $id) | .pid' "$FAKE_CLAUDE_STATE/agents.json")" | tr -d ' ')" | grep -Eo '/versions/[^ /]*' | cut -d/ -f3
}
watch1() { "$F" crew watch --once >"$T/watch.out" 2>&1; }
events() { cat "$FACTORY_CREW_HOME/events.jsonl" 2>/dev/null; }

newcc 1.0.0
start_tares "$T/tares"
trust "$FACTORY_CREW_HOME"
printf 'L3-review\nyes\n\nno\n3\n\n' | "$F" crew up >"$T/up.out" 2>&1
check "crew up starts three stations" '[ "$(launches)" = 3 ]'
all_idle
for s in crew-orchestrator crew-reviewer crew-releaser; do check "$s runs 1.0.0" '[ "$(runs $s)" = 1.0.0 ]'; done

# Nothing newer is installed: the watch moves nothing, roster and doctor show the version and no flag.
before=$(launches); watch1
check "the same version: nothing moves" '[ "$(launches)" = "$before" ]'
out() { "$F" "$@" >"$T/cmd.out" 2>&1; } # a command's output in a file: a pipe to grep -q fails under pipefail
out roster
check "roster shows each station's Claude Code" 'grep "^crew-reviewer" "$T/cmd.out" | grep -q "1.0.0" && ! grep -q "older than" "$T/cmd.out"'
out doctor
check "doctor names it" 'grep -q "crew-reviewer runs Claude Code 1.0.0" "$T/cmd.out"'
out crew status
check "status says nothing is behind" '! grep -q "^claude code:" "$T/cmd.out"'

# 1.1.0 is installed (the symlink swapped).
newcc 1.1.0
out roster
check "roster flags an older station" 'grep "^crew-reviewer" "$T/cmd.out" | grep -q "1.0.0 (older than 1.1.0)"'
out crew status
check "status counts them" 'grep -q "^claude code: 3 station(s) run an older Claude Code than 1.1.0" "$T/cmd.out"'
out doctor
check "doctor warns, and says how to update" 'grep "crew-reviewer runs Claude Code 1.0.0, older than the 1.1.0 installed" "$T/cmd.out" | grep -q "crew update"'

# The setting switches the watch off; Tares unreadable counts as off too.
api PUT /api/crew/settings '{"claude_update": false}' >/dev/null
watch1
check "the setting off: nothing moves" '[ "$(launches)" = "$before" ]'
out doctor
check "doctor tells the watch is off" 'grep -qi "claude_update" "$T/cmd.out"'
api PUT /api/crew/settings '{"claude_update": true}' >/dev/null

# A halted crew starts nothing.
"$F" crew halt "testing" >/dev/null 2>&1; watch1
check "halted: nothing moves" '[ "$(launches)" = "$before" ]'
"$F" crew resume >/dev/null 2>&1

# A version that flips back and forth is not a release: a watch that runs rounds sees no two alike.
echo 1.2.0 > "$FAKE_CLAUDE_STATE/version-flip"
FACTORY_NO_WATCH= "$F" crew watch --interval 1 >"$T/flip.out" 2>&1 & wp=$!; KEEP="$wp"
sleep 6; kill "$wp" 2>/dev/null; wait "$wp" 2>/dev/null; KEEP=""
rm -f "$FAKE_CLAUDE_STATE/version-flip" "$FAKE_CLAUDE_STATE/version-calls"
check "a version that flips within a round moves nothing" '[ "$(launches)" = "$before" ]'

# Busy, a subagent running, attached: each waits. The orchestrator waits for the others.
o=$(st crew-orchestrator .id); r=$(st crew-reviewer .id); l=$(st crew-releaser .id); rs=$(st crew-reviewer .sessionId)
job() { local f="$HOME/.claude/jobs/$(st "$1" .id)/state.json"; jq "$2" "$f" > "$T/js" && mv "$T/js" "$f"; }
all_idle; busy crew-reviewer
job crew-releaser '.inFlight = {tasks: 2, queued: 0, kinds: ["local_agent"]}'
watch1
check "a busy station and one with a subagent running wait" '[ "$(st crew-reviewer .id)" = "$r" ] && [ "$(st crew-releaser .id)" = "$l" ]'
check "the orchestrator waits while others are behind" '[ "$(st crew-orchestrator .id)" = "$o" ]'
# A busy station first in line does not hold up an idle one behind it.
job crew-releaser 'del(.inFlight)'; ls_=$(st crew-releaser .sessionId)
watch1
check "busy reviewer waits, the idle releaser behind it moves, in the same conversation, told why" '[ "$(st crew-reviewer .id)" = "$r" ] && [ "$(st crew-releaser .id)" != "$l" ] && [ "$(runs crew-releaser)" = 1.1.0 ] && last crew-releaser | jq -e --arg s "$ls_" "index(\"--resume\") as \$i | \$i != null and .[\$i+1] == \$s" >/dev/null && last crew-releaser | grep -q "Claude Code was updated to 1.1.0"'
check "the move is recorded" 'grep -q "\"station\":\"crew-releaser\",\"event\":\"claude-updated\"" "$FACTORY_CREW_HOME/events.jsonl"'
check "the orchestrator still waits" '[ "$(st crew-orchestrator .id)" = "$o" ]'
# Another release lands; the same again for an attached station.
newcc 1.1.5; all_idle; l=$(st crew-releaser .id); ls_=$(st crew-releaser .sessionId)
bash -c "exec -a 'claude attach $r' sleep 120" & apid=$!; KEEP="$apid"; sleep 1
watch1
check "an attached station waits (the other one moves)" '[ "$(st crew-reviewer .id)" = "$r" ] && [ "$(st crew-releaser .id)" != "$l" ]'
check "the moved station runs 1.1.5, in the same conversation, told why" '[ "$(runs crew-releaser)" = 1.1.5 ] && last crew-releaser | jq -e --arg s "$ls_" "index(\"--resume\") as \$i | \$i != null and .[\$i+1] == \$s" >/dev/null && last crew-releaser | grep -q "Claude Code was updated to 1.1.5"'
all_idle
watch1
check "still attached: the reviewer waits, the orchestrator too" '[ "$(st crew-reviewer .id)" = "$r" ] && [ "$(st crew-orchestrator .id)" = "$o" ]'
kill "$apid" 2>/dev/null; wait "$apid" 2>/dev/null; KEEP=""
watch1
check "no longer attached: the reviewer moves, not the orchestrator" '[ "$(st crew-reviewer .id)" != "$r" ] && [ "$(runs crew-reviewer)" = 1.1.5 ] && [ "$(st crew-orchestrator .id)" = "$o" ]'
all_idle
watch1
check "the orchestrator moves last" '[ "$(st crew-orchestrator .id)" != "$o" ] && [ "$(runs crew-orchestrator)" = 1.1.5 ] && [ "$(launched 3)" = "crew-releaser crew-reviewer crew-orchestrator" ]'
all_idle
before=$(launches); watch1
check "all on 1.1.5: nothing more moves" '[ "$(launches)" = "$before" ]'
out roster
check "roster has no flag any more" '! grep -q "older than" "$T/cmd.out"'

# An old pre-warmed spare comes back from Claude Code's pool: it is seen in the process listing, and the
# restart is tried again; a spare that nobody claimed yet is stopped first.
newcc 1.2.0; echo 1.1.0 > "$FAKE_CLAUDE_STATE/spawn-version"; echo 1 > "$FAKE_CLAUDE_STATE/stale-left"
sleep 300 & spare=$!; KEEP="$spare"; sleep 300 & claimed=$!; KEEP="$spare $claimed"; sleep 300 & fresh=$!; KEEP="$spare $claimed $fresh"
touch "$T/unclaimed.claim.sock"
{
  echo "$spare claude bg-pty-host --bg-pty-host $T/unclaimed.pty.sock 200 50 -- /x/versions/1.1.0 --bg-spare $T/unclaimed.claim.sock"
  echo "$claimed claude bg-pty-host --bg-pty-host $T/gone.pty.sock 200 50 -- /x/versions/1.1.0 --bg-spare $T/gone.claim.sock"
  echo "$fresh claude bg-pty-host --bg-pty-host $T/new.pty.sock 200 50 -- /x/versions/1.2.0 --bg-spare $T/unclaimed.claim.sock"
} > "$FAKE_CLAUDE_STATE/ps-list"
all_idle; before=$(launches)
watch1
check "a stale spare is noticed and the restart tried again" '[ "$(launches)" = $((before + 2)) ] && [ "$(runs crew-reviewer)" = 1.2.0 ] && grep -q "came back on Claude Code 1.1.0" "$T/watch.out" && grep -q "\"event\":\"stale-spare\"" "$FACTORY_CREW_HOME/events.jsonl"'
check "an unclaimed older spare was stopped" '! kill -0 "$spare" 2>/dev/null'
check "a claimed one (a session runs in it) and a new one were left" 'kill -0 "$claimed" 2>/dev/null && kill -0 "$fresh" 2>/dev/null'
check "the move succeeded in the end" 'grep -q "crew-reviewer runs Claude Code 1.2.0" "$T/watch.out"'
kill $claimed $fresh 2>/dev/null; KEEP=""; rm -f "$FAKE_CLAUDE_STATE/ps-list"

# Every launch lands on an old spare: three tries, then the watch leaves that station until the next
# version, and the orchestrator is not held back for ever by it. `crew update` tries again.
echo all > "$FAKE_CLAUDE_STATE/stale-left"
all_idle; before=$(launches)
watch1
check "three tries, then it gives up" '[ "$(launches)" = $((before + 3)) ] && [ "$(st crew-releaser .cc_failed)" = 1.2.0 ] && grep -q "keeps coming back on an older Claude Code than 1.2.0" "$T/watch.out"'
all_idle; before=$(launches); watch1
check "the failed station is not tried again; the orchestrator is" '[ "$(launches)" = $((before + 3)) ] && [ "$(launched 3)" = "crew-orchestrator crew-orchestrator crew-orchestrator" ] && [ "$(st crew-orchestrator .cc_failed)" = 1.2.0 ]'
all_idle; before=$(launches); watch1
check "nothing more this version" '[ "$(launches)" = "$before" ]'
all_idle; before=$(launches)
"$F" crew update --wait 5 >"$T/update-fail.out" 2>&1; rc=$?
check "crew update tries the failed ones again and says so" '[ $rc != 0 ] && grep -q "not every station runs Claude Code 1.2.0" "$T/update-fail.out" && [ "$(launches)" -ge $((before + 6)) ]'
echo 0 > "$FAKE_CLAUDE_STATE/stale-left"
all_idle; before=$(launches)
"$F" crew update --wait 5 >"$T/update.out" 2>&1; rc=$?
check "crew update, once the spares are gone: every station on 1.2.0" '[ $rc = 0 ] && for s in crew-orchestrator crew-reviewer crew-releaser; do [ "$(runs $s)" = 1.2.0 ] || exit 1; done && grep -q "every station runs Claude Code 1.2.0, one session each" "$T/update.out"'
check "the failure mark is cleared" '[ -z "$(st crew-releaser .cc_failed)" ] && [ -z "$(st crew-orchestrator .cc_failed)" ]'

# crew update on demand: waits for a busy station, the orchestrator last; a station already current is left.
newcc 1.3.0; all_idle; busy crew-reviewer; before=$(launches)
( sleep 3; ) ; ( sleep 4; idle crew-reviewer ) & ip=$!
"$F" crew update --wait 30 >"$T/update2.out" 2>&1; rc=$?
wait "$ip" 2>/dev/null
check "crew update waits for a busy station and then does it" '[ $rc = 0 ] && grep -q "waiting for crew-reviewer to be idle" "$T/update2.out" && [ "$(runs crew-reviewer)" = 1.3.0 ]'
check "orchestrator last" '[ "$(launched 3 | awk "{print \$3}")" = crew-orchestrator ]'
before=$(launches); all_idle
"$F" crew update >"$T/update3.out" 2>&1
check "already current: nothing restarted" '[ "$(launches)" = "$before" ] && grep -q "already runs Claude Code 1.3.0" "$T/update3.out"'
"$F" crew restart >"$T/restart.out" 2>&1; rc=$?
check "crew restart restarts every station anyway, the orchestrator last" '[ $rc = 0 ] && [ "$(launches)" = $((before + 3)) ] && [ "$(launched 1)" = crew-orchestrator ]'
all_idle

# Someone has a station open: crew update does not interrupt, and gives up at --wait.
newcc 1.4.0; r=$(st crew-reviewer .id)
bash -c "exec -a 'claude attach $r' sleep 120" & apid=$!; KEEP="$apid"; sleep 1
"$F" crew update --wait 3 >"$T/update4.out" 2>&1; rc=$?
check "an attached station is not restarted; crew update says who it waited for" '[ $rc != 0 ] && [ "$(st crew-reviewer .id)" = "$r" ] && grep -q "gave up waiting for crew-reviewer" "$T/update4.out" && grep -q "someone has it open" "$T/update4.out"'
check "the others were done" '[ "$(runs crew-releaser)" = 1.4.0 ] && [ "$(runs crew-orchestrator)" = 1.4.0 ]'
kill "$apid" 2>/dev/null; wait "$apid" 2>/dev/null; KEEP=""

# Two sessions under one station name: reported, and crew update fails (RIUS-1364).
all_idle
tmp=$(mktemp); jq '. + [{id: "dup00001", name: "crew-reviewer", state: "working", status: "idle", sessionId: "other", pid: 49999, cwd: "/"}]' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"
"$F" crew update >"$T/dup.out" 2>&1; rc=$?
check "two sessions under one name: reported, the update fails" '[ $rc != 0 ] && grep -q "TWO SESSIONS named crew-reviewer" "$T/dup.out"'
check "the watch reports it after a move too" 'newcc 1.5.0; all_idle; watch1; grep -q "TWO SESSIONS named crew-reviewer" "$T/watch.out" || grep -q "two-sessions" "$FACTORY_CREW_HOME/events.jsonl"'
jq 'map(select(.id != "dup00001"))' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"
all_idle; "$F" crew update >"$T/dup2.out" 2>&1; rc=$?
check "one session each: it passes" '[ $rc = 0 ] && grep -q "one session each" "$T/dup2.out"'

# A version nobody can read, or one that is older than a station's, never moves anything.
echo "" > "$FAKE_CLAUDE_STATE/version"; all_idle; before=$(launches)
"$F" crew update >"$T/nover.out" 2>&1; rc=$?
check "no readable version: crew update stops plainly" '[ $rc != 0 ] && grep -q "could not read the Claude Code version" "$T/nover.out" && [ "$(launches)" = "$before" ]'
newcc 1.0.0; watch1
check "a station on a newer version than installed is left" '[ "$(launches)" = "$before" ]'

[ $fail = 0 ] && echo "claude update tests passed" || { echo "claude update tests FAILED"; exit 1; }
