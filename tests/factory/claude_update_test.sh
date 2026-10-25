#!/usr/bin/env bash
# Checks a new Claude Code reaching the crew (TR-669), on a real Tares with a fake claude and a fake ps:
# the watch moves stations one idle station a round, the orchestrator last, in the same conversation;
# a busy, attached or waiting station is left alone; nothing moves while halted, while the setting is
# off, or while the version flips back and forth; an old pre-warmed spare is stopped and the restart
# tried again; `crew update` and `crew restart` do every station and check there is one session each;
# roster, status and doctor show the versions; a frozen crew (TR-676) hears of a newer Claude Code and
# moves nothing.
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
# `sort -V` (the version order) runs a command once when its input names 0.9.9, the version of a spare in
# $FAKE_CLAUDE_STATE/sort-hook's test: something that happens between two steps of prune_spares.
cat > "$T/bin/sort" <<'SH'
#!/usr/bin/env bash
case " $* " in *" -V "*) ;; *) exec /usr/bin/sort "$@" ;; esac
in=$(cat)
if [ -f "$FAKE_CLAUDE_STATE/sort-hook" ] && grep -q '0\.9\.9' <<<"$in"; then hook=$(cat "$FAKE_CLAUDE_STATE/sort-hook"); rm -f "$FAKE_CLAUDE_STATE/sort-hook"; bash -c "$hook"; fi
printf '%s\n' "$in" | /usr/bin/sort "$@"
SH
chmod +x "$T/bin/sort"
trap 'stop_tares; kill $KEEP 2>/dev/null; rm -rf "$T"' EXIT
KEEP=""
fail=0
# a subshell, so an `exit 1` in a check fails the check, not the script
check() { if ( eval "$2" ); then :; else echo "FAIL $1"; fail=1; fi; }
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
kill $claimed $fresh 2>/dev/null; KEEP=""; : > "$FAKE_CLAUDE_STATE/ps-list"

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

# The retries look again at what the first restart left: a halt, or someone opening the station, stops
# them (the restart put the station on the update prompt, so it is not quiet, and only these two matter).
newcc 1.2.5; echo 1.1.0 > "$FAKE_CLAUDE_STATE/spawn-version"; echo all > "$FAKE_CLAUDE_STATE/stale-left"
all_idle; before=$(launches)
echo "$F crew halt retry-test" > "$FAKE_CLAUDE_STATE/on-bg"
watch1; "$F" crew resume >/dev/null 2>&1
check "halted after the first try: no second restart" '[ "$(launches)" = $((before + 1)) ] && grep -q "came back on Claude Code 1.1.0" "$T/watch.out" && [ -z "$(st crew-reviewer .cc_failed)" ]'
all_idle; before=$(launches)
echo "bash -c \"exec -a 'claude attach \$FAKE_ID' sleep 120\" & echo \$! > $T/attach.pid" > "$FAKE_CLAUDE_STATE/on-bg"
watch1; sleep 1; KEEP="$(cat "$T/attach.pid" 2>/dev/null)"
check "opened after the first try: no second restart" '[ "$(launches)" = $((before + 1)) ] && grep -q "came back on Claude Code 1.1.0" "$T/watch.out" && [ -z "$(st crew-reviewer .cc_failed)" ]'
kill $KEEP 2>/dev/null; wait $KEEP 2>/dev/null; KEEP=""
echo 0 > "$FAKE_CLAUDE_STATE/stale-left"; all_idle
"$F" crew update --wait 5 >"$T/retry-settle.out" 2>&1
check "the stations all run 1.2.5 after it" 'for s in crew-orchestrator crew-reviewer crew-releaser; do [ "$(runs $s)" = 1.2.5 ] || exit 1; done'

# A spare that is claimed after the process listing was read (its claim socket is gone) is not stopped.
newcc 1.2.6; all_idle
sleep 300 & raced=$!; KEEP="$raced"
touch "$T/raced.claim.sock"
echo "$raced claude bg-pty-host --bg-pty-host $T/raced.pty.sock 200 50 -- /x/versions/0.9.9 --bg-spare $T/raced.claim.sock" > "$FAKE_CLAUDE_STATE/ps-list"
echo "rm -f $T/raced.claim.sock" > "$FAKE_CLAUDE_STATE/sort-hook"
"$F" crew update --wait 5 >"$T/raced.out" 2>&1; rc=$?
check "a spare claimed since the listing is left alone" '[ $rc = 0 ] && kill -0 "$raced" 2>/dev/null && [ ! -e "$FAKE_CLAUDE_STATE/sort-hook" ]'
kill $raced 2>/dev/null; wait $raced 2>/dev/null; KEEP=""; : > "$FAKE_CLAUDE_STATE/ps-list"

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

# crew update run from a station (the orchestrator) leaves that station out instead of waiting on itself.
bounded() { # seconds command...: runs it, kills it when it is still going after that long; sets bounded_rc (137 = killed)
  local secs=$1 bp i; shift
  "$@" & bp=$!
  for i in $(seq "$secs"); do kill -0 "$bp" 2>/dev/null || break; sleep 1; done
  if kill -0 "$bp" 2>/dev/null; then pkill -P "$bp" 2>/dev/null; kill "$bp" 2>/dev/null; wait "$bp" 2>/dev/null; bounded_rc=137; else wait "$bp"; bounded_rc=$?; fi
}
newcc 1.5.5; all_idle; busy crew-orchestrator; o=$(st crew-orchestrator .id); before=$(launches)
bounded 40 env FACTORY_STATION=crew-orchestrator "$F" crew update >"$T/self.out" 2>&1; rc=$bounded_rc
check "from the orchestrator: it finishes, the others move, the orchestrator is left" '[ $rc = 0 ] && [ "$(launches)" = $((before + 2)) ] && [ "$(st crew-orchestrator .id)" = "$o" ] && [ "$(runs crew-reviewer)" = 1.5.5 ] && [ "$(runs crew-releaser)" = 1.5.5 ]'
check "it says how the orchestrator moves" 'grep -q "crew-orchestrator, which ran this command, moves when" "$T/self.out"'

# A halt in the middle of crew update stops it; it does not wait for ever.
newcc 1.5.6; all_idle; busy crew-reviewer
( sleep 3; "$F" crew halt "testing" >/dev/null 2>&1 ) & hp=$!
bounded 40 "$F" crew update --wait 0 >"$T/halt.out" 2>&1; rc=$bounded_rc
wait "$hp" 2>/dev/null; "$F" crew resume >/dev/null 2>&1
check "halted in the middle: crew update stops and says so" '[ $rc != 0 ] && [ $rc != 137 ] && grep -q "the crew was halted" "$T/halt.out"'

# --wait is a number of seconds: anything else is refused before a station is touched, and 00 is 0.
newcc 1.5.7; all_idle; before=$(launches)
for w in abc 1e3 -5 "" 99999999999999999999; do
  "$F" crew update --wait "$w" >"$T/wait.out" 2>&1; rc=$?
  check "--wait '$w' is refused, nothing restarted" '[ $rc != 0 ] && grep -q "takes a number of seconds" "$T/wait.out" && [ "$(launches)" = "$before" ]'
done
"$F" crew update --wait >"$T/wait.out" 2>&1; rc=$?
check "--wait with no value is refused with the usage line, nothing restarted" '[ $rc != 0 ] && grep -q "takes a number of seconds" "$T/wait.out" && [ "$(launches)" = "$before" ]'
"$F" crew update --wait 00 >"$T/wait0.out" 2>&1; rc=$?
check "--wait 00 is 0: no deadline, the stations move" '[ $rc = 0 ] && for s in crew-orchestrator crew-reviewer crew-releaser; do [ "$(runs $s)" = 1.5.7 ] || exit 1; done'

# The station that runs crew update is left to the watch only when it is behind, and the way out is said
# even when another station fails.
before=$(launches); all_idle
bounded 40 env FACTORY_STATION=crew-orchestrator "$F" crew update >"$T/self2.out" 2>&1; rc=$bounded_rc
check "the caller already runs the version: nothing restarted, nothing said about its move" '[ $rc = 0 ] && [ "$(launches)" = "$before" ] && grep -q "crew-orchestrator already runs Claude Code 1.5.7" "$T/self2.out" && ! grep -q "which ran this command" "$T/self2.out"'
newcc 1.5.8; echo 1.1.0 > "$FAKE_CLAUDE_STATE/spawn-version"; echo all > "$FAKE_CLAUDE_STATE/stale-left"; all_idle
bounded 40 env FACTORY_STATION=crew-orchestrator "$F" crew update --wait 5 >"$T/self3.out" 2>&1; rc=$bounded_rc
check "another station fails: the caller's way out is still said" '[ $rc != 0 ] && [ $rc != 137 ] && grep -q "crew-orchestrator, which ran this command, moves when" "$T/self3.out" && grep -q "not every station runs Claude Code 1.5.8" "$T/self3.out"'
echo 0 > "$FAKE_CLAUDE_STATE/stale-left"; all_idle; "$F" crew update --wait 5 >/dev/null 2>&1

# What a restart says about the schedule follows idle_wake (the same words as every other restart): off, re-arm
# it; on, arm none, since the factory holds the tick. A Claude Code move is one of those restarts.
echo 0 > "$FAKE_CLAUDE_STATE/stale-left"
newcc 1.5.9; all_idle; api PUT /api/crew/settings '{"idle_wake": false}' >/dev/null
"$F" crew update --wait 5 >"$T/rearm-off.out" 2>&1
check "idle_wake off: the Claude Code restart says to re-arm the schedule" 'for s in crew-orchestrator crew-reviewer crew-releaser; do last $s | grep -q "re-arm your schedule if you have one" || exit 1; done'
newcc 1.6.0; all_idle; api PUT /api/crew/settings '{"idle_wake": true}' >/dev/null
"$F" crew update --wait 5 >"$T/rearm-on.out" 2>&1
check "idle_wake on: the Claude Code restart says to arm no tick" 'for s in crew-orchestrator crew-reviewer crew-releaser; do last $s | grep -q "arm no tick of your own" && ! last $s | grep -q "re-arm your schedule" || exit 1; done'
# off again, and one more restart so no station stays released (the watch would wake it to re-arm)
api PUT /api/crew/settings '{"idle_wake": false}' >/dev/null; all_idle
"$F" crew restart --wait 5 >/dev/null 2>&1; rm -f "$FACTORY_CREW_HOME"/stations/*.wake

# Numbers compare as numbers: 1.10.0 is newer than the 1.5.x the stations run (a plain sort puts it before them).
newcc 1.10.0; all_idle; before=$(launches)
watch1
check "1.10.0 is newer than 1.5.x: the watch moves a station onto it" '[ "$(launches)" = $((before + 1)) ] && [ "$(runs "$(launched 1)")" = 1.10.0 ]'

# A frozen crew (`crew freeze`, TR-676) moves no station onto a newer Claude Code either, and says so once
# for each version; `crew update` is the person asking, so it still works; unfreezing lets the watch move.
newcc 1.10.1; all_idle; "$F" crew freeze >"$T/freeze.out" 2>&1; before=$(launches)
watch1
check "frozen: the watch moves no station onto a newer Claude Code" '[ "$(launches)" = "$before" ]'
check "frozen: it says there is a newer Claude Code" 'grep -q "Claude Code 1.10.1 is newer than the one [0-9]* station(s) run; not moving stations while the kit is frozen" "$T/watch.out"'
watch1
check "frozen: the same version is said once" '[ "$(events | grep -c "\"event\":\"claude-newer\"")" = 1 ] && ! grep -q "is newer than" "$T/watch.out" && [ "$(launches)" = "$before" ]'
out crew status
check "frozen: status says the freeze stops the watch moving them" 'grep -q "^claude code: [0-9]* station(s) run an older Claude Code than 1.10.1; the freeze stops the watch moving them" "$T/cmd.out"'
"$F" crew update --wait 5 >"$T/frozen-update.out" 2>&1; rc=$?
check "frozen: crew update still moves every station" '[ $rc = 0 ] && for s in crew-orchestrator crew-reviewer crew-releaser; do [ "$(runs $s)" = 1.10.1 ] || exit 1; done'
newcc 1.10.2; all_idle; watch1
check "frozen: the next version is said once more" '[ "$(events | grep -c "\"event\":\"claude-newer\"")" = 2 ] && grep -q "Claude Code 1.10.2 is newer" "$T/watch.out"'
"$F" crew unfreeze >"$T/unfreeze.out" 2>&1; before=$(launches); all_idle
watch1
check "unfrozen: the watch moves a station again" '[ "$(launches)" = $((before + 1)) ]'
for _ in 1 2 3 4; do all_idle; watch1; done   # the stations still on the frozen kit follow the checkout's, one a round

# The note of the version already said (claude-newer) goes when nobody is behind any more, and with a freeze or unfreeze.
NF="$FACTORY_CREW_HOME/claude-newer"
newcc 1.10.4; all_idle; "$F" crew freeze >/dev/null 2>&1; watch1
check "frozen: the version said is noted" '[ "$(cat "$NF" 2>/dev/null)" = 1.10.4 ]'
"$F" crew update --wait 5 >/dev/null 2>&1; watch1
check "frozen, nobody behind: the note is cleared" '[ ! -e "$NF" ] && ! grep -q "is newer than" "$T/watch.out"'
echo 9.9.9 > "$NF"; "$F" crew unfreeze >/dev/null 2>&1
check "unfreeze clears the note" '[ ! -e "$NF" ]'
echo 9.9.9 > "$NF"; "$F" crew freeze >/dev/null 2>&1
check "freeze clears the note" '[ ! -e "$NF" ]'
"$F" crew unfreeze >/dev/null 2>&1; for _ in 1 2 3 4; do all_idle; watch1; done

# A freeze that lands in the middle of a round (`crew freeze` while the watch has picked a station) wins:
# nothing is restarted. The fake claude freezes the crew once the watch has read the session list.
newcc 1.10.5; all_idle; before=$(launches)
echo "$F crew freeze" > "$FAKE_CLAUDE_STATE/on-agents"; echo 1 > "$FAKE_CLAUDE_STATE/on-agents-at"; rm -f "$FAKE_CLAUDE_STATE/on-agents-reads"
watch1; rm -f "$FAKE_CLAUDE_STATE/on-agents"
out crew status
check "frozen in the middle of the round: nothing moves onto the newer Claude Code" '[ "$(launches)" = "$before" ] && grep -q "^FROZEN" "$T/cmd.out"'
"$F" crew unfreeze >/dev/null 2>&1; for _ in 1 2 3 4; do all_idle; watch1; done

# A version nobody can read, or one that is older than a station's, never moves anything.
echo "" > "$FAKE_CLAUDE_STATE/version"; all_idle; before=$(launches)
"$F" crew update >"$T/nover.out" 2>&1; rc=$?
check "no readable version: crew update stops plainly" '[ $rc != 0 ] && grep -q "could not read the Claude Code version" "$T/nover.out" && [ "$(launches)" = "$before" ]'
newcc 1.0.0; watch1
check "a station on a newer version than installed is left" '[ "$(launches)" = "$before" ]'

[ $fail = 0 ] && echo "claude update tests passed" || { echo "claude update tests FAILED"; exit 1; }
