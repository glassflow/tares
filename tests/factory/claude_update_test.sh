#!/usr/bin/env bash
# Checks a new Claude Code reaching the crew (TR-669), on a real Tares with a fake claude and a fake ps:
# the watch moves stations one idle station a round, the orchestrator last, in the same conversation;
# a busy, attached or waiting station is left alone; nothing moves while halted, while the setting is
# off, or while the version flips back and forth; an old pre-warmed spare is stopped and the restart
# tried again; `crew update` and `crew restart` do every station and check there is one session each;
# roster, status and doctor show the versions; a frozen crew (TR-676) hears of a newer Claude Code and
# moves nothing; a full plan limit (pace, TR-674) holds a move back until it has reset.
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

# Every resumed launch lands on an old spare (TR-687, D129 A3 overturned): three tries in the same
# conversation, then the station is started again in a new conversation, once per version, so it is not left
# on the old version. Where that lands on an old spare too, the station is flagged as retrying and the watch
# tries it again after a backoff that doubles; the orchestrator is not held back by it. `crew update` tries
# at once, whatever the backoff says.
cc_marks() { jq -c --arg s "$1" '.[$s] | {cc_failed, cc_fails, cc_retry_at, cc_fresh}' "$FACTORY_CREW_HOME/stations.json"; }
resumed() { # station n → how many of its last n launches resumed a conversation
  jq -c "select(.args[0] == \"--bg\" and .args[2] == \"$1\") | .args" "$FAKE_CLAUDE_STATE/calls.jsonl" | tail -n "$2" | grep -c -- '"--resume"'
}
newcc 1.2.1; echo 3 > "$FAKE_CLAUDE_STATE/stale-left"
all_idle; before=$(launches)
watch1; x=$(launched 1)
check "three resumed tries land on old spares, the fourth is a new conversation on 1.2.1" '[ "$(launches)" = $((before + 4)) ] && [ "$(resumed "$x" 4)" = 3 ] && ! last "$x" | grep -q -- "--resume" && [ "$(runs "$x")" = 1.2.1 ]'
check "it is told it was started again, and the event is recorded" 'last "$x" | grep -q "started you again under the same name, in a new conversation" && grep -q "starting it again in a new conversation on 1.2.1" "$T/watch.out" && grep -q "\"station\":\"$x\",\"event\":\"restarted-fresh\"" "$FACTORY_CREW_HOME/events.jsonl"'
check "a station that got there is not marked: no failure, no retry" '[ "$(cc_marks "$x")" = "{\"cc_failed\":null,\"cc_fails\":null,\"cc_retry_at\":null,\"cc_fresh\":null}" ]'
echo all > "$FAKE_CLAUDE_STATE/stale-left"
all_idle; before=$(launches)
watch1; y=$(launched 1)
check "old spares every time: three tries, the new conversation, then it stops and says it will retry" '[ "$y" != "$x" ] && [ "$(launches)" = $((before + 4)) ] && [ "$(resumed "$y" 4)" = 3 ] && grep -q "keeps coming back on an older Claude Code than 1.2.1" "$T/watch.out" && grep -q "the watch tries again in 10 min" "$T/watch.out"'
check "it is marked: the version, one failed move, a retry time ten minutes on, the new conversation done" '[ "$(st "$y" .cc_failed)" = 1.2.1 ] && [ "$(st "$y" .cc_fails)" = 1 ] && [ "$(st "$y" .cc_fresh)" = 1.2.1 ] && d=$(( $(st "$y" .cc_retry_at) - $(date +%s) )) && [ $d -gt 540 ] && [ $d -le 600 ]'
out roster
check "roster flags it as retrying" 'grep "^$y" "$T/cmd.out" | grep -q "1.1.0 (older than 1.2.1, retrying in 10 min)"'
out crew status
check "status counts it as retrying" 'grep -q "^claude code: 2 station(s) run an older Claude Code than 1.2.1.*1 retrying after coming back on an old spare" "$T/cmd.out"'
out doctor
check "doctor says it keeps coming back and when the watch retries" 'grep "$y runs Claude Code 1.1.0" "$T/cmd.out" | grep -q "keeps coming back on an old spare; the watch retries in 10 min"'
check "Tares has it, for the Crew page" '[ "$(api GET /api/crew | jq -r --arg y "$y" ".claude_retry[\$y] | [.target, .fails] | join(\" \")")" = "1.2.1 1" ]'
all_idle; before=$(launches)
watch1; o_=$(launched 1)
check "the station backing off is not tried again, and does not hold the orchestrator back" '[ "$o_" = crew-orchestrator ] && [ "$(launches)" = $((before + 4)) ] && [ "$(st crew-orchestrator .cc_failed)" = 1.2.1 ] && [ "$(st "$y" .cc_fails)" = 1 ]'
all_idle; before=$(launches); watch1
check "everyone is backing off: nothing more is tried" '[ "$(launches)" = "$before" ]'
check "Tares lists both" '[ "$(api GET /api/crew | jq -r ".claude_retry | keys | join(\" \")")" = "$(printf "%s\n" "$y" crew-orchestrator | sort | paste -sd" " -)" ]'
# the backoff is over: the watch tries again in the same conversation, with no second new conversation, and waits twice as long
retry_due() { local tmp; tmp=$(mktemp); jq --arg s "$1" '.[$s].cc_retry_at = 1' "$FACTORY_CREW_HOME/stations.json" > "$tmp" && mv "$tmp" "$FACTORY_CREW_HOME/stations.json"; }
retry_due "$y"; all_idle; before=$(launches)
watch1
check "after the backoff: three more tries, no second new conversation, the wait doubles" '[ "$(launches)" = $((before + 3)) ] && [ "$(resumed "$y" 3)" = 3 ] && [ "$(st "$y" .cc_fails)" = 2 ] && d=$(( $(st "$y" .cc_retry_at) - $(date +%s) )) && [ $d -gt 1140 ] && [ $d -le 1200 ]'
retry_due "$y"; echo 0 > "$FAKE_CLAUDE_STATE/stale-left"; all_idle; before=$(launches)
watch1
check "once a spare is not in the way it moves, and every mark is cleared" '[ "$(launches)" = $((before + 1)) ] && [ "$(runs "$y")" = 1.2.1 ] && [ "$(cc_marks "$y")" = "{\"cc_failed\":null,\"cc_fails\":null,\"cc_retry_at\":null,\"cc_fresh\":null}" ]'
echo all > "$FAKE_CLAUDE_STATE/stale-left"
all_idle; before=$(launches)
"$F" crew update --wait 5 >"$T/update-fail.out" 2>&1; rc=$?
check "crew update ignores the backoff, tries the orchestrator again and says the watch retries" '[ $rc != 0 ] && grep -q "not every station runs Claude Code 1.2.1" "$T/update-fail.out" && grep -q "the watch tries again in" "$T/update-fail.out" && [ "$(launches)" -ge $((before + 3)) ] && [ "$(st crew-orchestrator .cc_fails)" = 2 ]'
echo 0 > "$FAKE_CLAUDE_STATE/stale-left"
all_idle; before=$(launches)
"$F" crew update --wait 5 >"$T/update.out" 2>&1; rc=$?
check "crew update, once the spares are gone: every station on 1.2.1" '[ $rc = 0 ] && for s in crew-orchestrator crew-reviewer crew-releaser; do [ "$(runs $s)" = 1.2.1 ] || exit 1; done && grep -q "every station runs Claude Code 1.2.1, one session each" "$T/update.out"'
check "the failure marks are cleared, here and on Tares" '[ -z "$(st crew-releaser .cc_failed)" ] && [ -z "$(st crew-orchestrator .cc_failed)" ] && [ -z "$(st crew-orchestrator .cc_retry_at)" ] && [ "$(api GET /api/crew | jq -c .claude_retry)" = "{}" ]'

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
# 00 is 0, no deadline: crew update waits for the busy reviewer however long it takes. Read as a deadline
# of now (no zero strip), it gave up at once; 08 read as an octal number is an error, not 8 seconds.
busy crew-reviewer
( sleep 4; idle crew-reviewer ) & wp=$!
bounded 40 "$F" crew update --wait 00 >"$T/wait0.out" 2>&1; rc=$bounded_rc; wait "$wp" 2>/dev/null
check "--wait 00 is 0: it waits for the busy reviewer, then every station moves" '[ $rc = 0 ] && grep -q "waiting for crew-reviewer to be idle" "$T/wait0.out" && ! grep -q "gave up waiting" "$T/wait0.out" && for s in crew-orchestrator crew-reviewer crew-releaser; do [ "$(runs $s)" = 1.5.7 ] || exit 1; done'

# The station that runs crew update is left to the watch only when it is behind, and the way out is said
# even when another station fails.
before=$(launches); all_idle
bounded 40 env FACTORY_STATION=crew-orchestrator "$F" crew update >"$T/self2.out" 2>&1; rc=$bounded_rc
check "the caller already runs the version: nothing restarted, nothing said about its move" '[ $rc = 0 ] && [ "$(launches)" = "$before" ] && grep -q "crew-orchestrator already runs Claude Code 1.5.7" "$T/self2.out" && ! grep -q "which ran this command" "$T/self2.out"'
# The caller that is not behind is not promised a move by the watch: crew update --force restarts the others and
# says the caller can only be restarted from another session (the watch moves a station only when it is behind).
all_idle
bounded 40 env FACTORY_STATION=crew-orchestrator "$F" crew update --force >"$T/self2f.out" 2>&1; rc=$bounded_rc
check "a caller that is not behind: it is told to restart itself from another session, not that the watch moves it" '[ $rc = 0 ] && grep -q "crew-orchestrator, which ran this command, can only be restarted from another session: run .* restart crew-orchestrator there" "$T/self2f.out" && ! grep -q "moves when" "$T/self2f.out"'
newcc 1.5.7.1; all_idle; busy crew-reviewer
( sleep 3; idle crew-reviewer ) & wp=$!
bounded 40 "$F" crew update --wait 08 >"$T/wait8.out" 2>&1; rc=$bounded_rc; wait "$wp" 2>/dev/null
check "--wait 08 is 8 seconds, not an octal error: the busy reviewer is waited for" '[ $rc = 0 ] && ! grep -q "value too great" "$T/wait8.out" && grep -q "waiting for crew-reviewer to be idle" "$T/wait8.out" && [ "$(runs crew-reviewer)" = 1.5.7.1 ]'
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

# Pace (TR-674): a Claude Code move is a restart, a model turn, so it never goes into a plan limit that is full
# and has not reset (the watch moves nothing, `crew update` says why and restarts nothing), and moves again
# once the limit has reset. Below the limit the restart prompt says the schedule is re-armed at 45 minutes.
iso_in() { "$PY" -c 'import sys, datetime; print((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=int(sys.argv[1]))).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$1"; }
planbody() { # session% weekly% nonce
  jq -cn --arg at "$(iso_in 0)" --arg sr "$(iso_in 10800)" --arg wr "$(iso_in 259200)" --argjson s "$1" --argjson w "$2" --argjson n "$3" \
    '{type: "claude_plan", timestamp: $at, nonce: $n, usage_report: {rate_limits: {limits: [
       {kind: "session", percent: $s, resets_at: $sr, scope: null, is_active: true},
       {kind: "weekly_all", percent: $w, resets_at: $wr, scope: null, is_active: false}]}}}'
}
plan() { api POST /ingest/claude_code "$(planbody "$1" "$2" "$RANDOM")" >/dev/null; }
rounds() { local _; for _ in $(seq "$1"); do all_idle; watch1; done; }
api PUT /api/crew/settings '{"pace": true, "idle_wake": false}' >/dev/null
plan 100 10; rounds 3   # the notes pace sends to a crew at its limit are restarts too; let them go first
v0=$(runs crew-reviewer); newcc 1.11.0; before=$(launches)
rounds 4
check "limit full: the watch moves no station onto the newer Claude Code" '[ "$(launches)" = "$before" ] && [ "$(runs crew-reviewer)" = "$v0" ]'
"$F" crew update --wait 5 >"$T/pace-update.out" 2>&1; rc=$?
check "limit full: crew update says so and restarts nothing" '[ $rc != 0 ] && grep -q "the plan.s limit is full until 20" "$T/pace-update.out" && [ "$(launches)" = "$before" ]'
plan 10 10; rounds 4
check "the limit has reset: the watch moves a station again" '[ "$(launches)" -gt "$before" ] && [ "$(runs "$(launched 1)")" = 1.11.0 ]'
"$F" crew update --wait 5 >/dev/null 2>&1; all_idle
# the limit fills in the middle of a round (the watch read the plan before it did): the move waits
newcc 1.11.1; before=$(launches)
echo "curl -sS -X POST -H 'Content-Type: application/json' --data '$(planbody 100 10 424242)' \"$TARESD_URL/ingest/claude_code\"" > "$FAKE_CLAUDE_STATE/on-agents"
echo 1 > "$FAKE_CLAUDE_STATE/on-agents-at"; rm -f "$FAKE_CLAUDE_STATE/on-agents-reads"
watch1; rm -f "$FAKE_CLAUDE_STATE/on-agents"
check "the limit fills in the middle of the round: nothing is restarted onto the newer Claude Code" '[ "$(api GET /api/crew/pace | jq -r .at_limit)" = true ] && ! grep -q "restarting .* on 1.11.1" "$T/watch.out" && [ "$(launches)" = "$before" ]'
plan 90 10; rounds 3   # slow, not the limit: the stations are told to tick every 45 minutes
newcc 1.11.2
"$F" crew update --wait 5 >"$T/pace-slow.out" 2>&1; rc=$?
check "below the limit, crew update moves them" '[ $rc = 0 ] && for s in crew-orchestrator crew-reviewer crew-releaser; do [ "$(runs $s)" = 1.11.2 ] || exit 1; done'
check "and the restart says to re-arm at 45 minutes" 'for s in crew-orchestrator crew-reviewer crew-releaser; do last $s | grep -q "re-arm your schedule if you have one, at 45 minutes while the factory paces the crew" || exit 1; done'
# the plan's limit fills while crew update waits for a busy station: the move behind it is not made, and the
# message says when the limit resets (the limit was open when crew update started, so it reads the plan again)
newcc 1.11.3; all_idle; busy crew-reviewer; plan 10 10
r=$(st crew-reviewer .id); o=$(st crew-orchestrator .id)
( sleep 3; plan 100 10; idle crew-reviewer ) & pp=$!
bounded 40 "$F" crew update --wait 30 >"$T/pace-fills.out" 2>&1; rc=$bounded_rc; wait "$pp" 2>/dev/null
check "the limit fills while crew update waits: it stops, says until when, and restarts nothing more" '[ $rc != 0 ] && [ $rc != 137 ] && grep -q "the plan.s limit is full until 20" "$T/pace-fills.out" && grep -q "nothing more was restarted" "$T/pace-fills.out" && [ "$(st crew-reviewer .id)" = "$r" ] && [ "$(st crew-orchestrator .id)" = "$o" ]'
plan 10 10; all_idle; "$F" crew update --wait 5 >/dev/null 2>&1
api PUT /api/crew/settings '{"pace": false}' >/dev/null; rounds 3; rm -f "$FACTORY_CREW_HOME"/stations/*.pace "$FACTORY_CREW_HOME/pace.json"

# A version nobody can read, or one that is older than a station's, never moves anything.
echo "" > "$FAKE_CLAUDE_STATE/version"; all_idle; before=$(launches)
"$F" crew update >"$T/nover.out" 2>&1; rc=$?
check "no readable version: crew update stops plainly" '[ $rc != 0 ] && grep -q "could not read the Claude Code version" "$T/nover.out" && [ "$(launches)" = "$before" ]'
newcc 1.0.0; watch1
check "a station on a newer version than installed is left" '[ "$(launches)" = "$before" ]'

# A helper follows its station onto a new Claude Code (TR-687, D129 A2 overturned): after the other stations,
# before the orchestrator, in the same conversation. A helper that is busy does not hold the orchestrator back.
newcc 1.11.2; "$F" spawn helper crew-reviewer probe "look at something" >"$T/helper.out" 2>&1; hn=crew-reviewer-probe
check "a helper is running, on the installed version" '[ -n "$(st $hn .id)" ] && [ "$(runs $hn)" = 1.11.2 ]'
newcc 1.12.0; s0=$(st $hn .sessionId); before=$(launches)
for _ in 1 2 3 4; do all_idle; idle $hn; watch1; done
check "the helper moves with the watch: four restarts, the helper before the orchestrator, which is last" '[ "$(launches)" = $((before + 4))  ] && set -- $(launched 4) && [ "$4" = crew-orchestrator ] && launched 4 | grep -q "$hn" && [ "$(runs $hn)" = 1.12.0 ] && [ "$(runs crew-orchestrator)" = 1.12.0 ]'
check "the helper keeps its conversation and is told to load its parent role skill" 'last $hn | jq -e --arg s "$s0" "index(\"--resume\") as \$i | \$i != null and .[\$i+1] == \$s" >/dev/null && last $hn | grep -q "station-reviewer"'
newcc 1.12.1; before=$(launches)
for _ in 1 2 3; do all_idle; busy $hn; watch1; done
check "a busy helper is left alone, and does not hold the orchestrator back" '[ "$(runs crew-orchestrator)" = 1.12.1 ] && [ "$(runs crew-reviewer)" = 1.12.1 ] && [ "$(runs $hn)" = 1.12.0 ]'
all_idle; idle $hn; watch1
check "once the helper is idle it moves too" '[ "$(runs $hn)" = 1.12.1 ]'
newcc 1.12.2
check "the roster flags an old helper" '"$F" roster 2>&1 | grep "$hn" | grep -q "older than 1.12.2"'

# A helper's task lives only in its spawn prompt, so it is never started in a new conversation (review of
# TR-687, F1): where every try lands on an old spare it is marked and retried after a backoff, resumed each time.
newcc 1.12.2; echo 1.12.0 > "$FAKE_CLAUDE_STATE/spawn-version"; echo all > "$FAKE_CLAUDE_STATE/stale-left"
for _ in 1 2; do all_idle; idle $hn; watch1; done   # the reviewer and the releaser go first and back off
all_idle; idle $hn; before=$(launches); watch1
check "a helper behind on old spares: three resumed tries, no new conversation" '[ "$(launches)" = $((before + 3)) ] && [ "$(resumed "$hn" 3)" = 3 ] && ! last $hn | grep -q "started you again"'
check "it is marked for a retry, with no new conversation recorded" '[ "$(st $hn .cc_failed)" = 1.12.2 ] && [ -z "$(st $hn .cc_fresh)" ] && grep -q "a helper keeps its conversation" "$T/watch.out"'
# crew update does not promise a helper a new conversation either (F9)
retry_due $hn; all_idle; idle $hn
"$F" crew update --wait 5 >"$T/update-helper.out" 2>&1 || true
check "crew update on a helper that keeps landing on old spares: no new conversation promised" 'grep "$hn came back" "$T/update-helper.out" | grep -q "a helper keeps its conversation" && ! grep "$hn came back" "$T/update-helper.out" | grep -q "even in a new conversation"'
# A helper whose restart fails is not revived by the watch, and the message does not say it is (F5)
retry_due $hn; all_idle; idle $hn
FAKE_CLAUDE_FAIL_RESUME=1 watch1
check "a helper that could not be restarted: the message says the watch does not revive helpers" 'grep -q "could not restart $hn on Claude Code 1.12.2; the watch does not revive helpers, so ask crew-reviewer to spawn it again" "$T/watch.out" && ! grep "$hn" "$T/watch.out" | grep -q "revives it as before"'
"$F" retire $hn >/dev/null 2>&1
echo 0 > "$FAKE_CLAUDE_STATE/stale-left"; for _ in 1 2 3 4; do all_idle; watch1; done

# The new conversation is recorded only once its restart has returned (F3): one that dies leaves it to a later round
newcc 1.12.3; echo all > "$FAKE_CLAUDE_STATE/stale-left"; all_idle
FAKE_CLAUDE_FAIL_FRESH=1 watch1; x=$(launched 1)
check "the new conversation could not be started: it is not recorded as done, nor marked as failed" '[ -n "$x" ] && [ "$(st $x .cc_fresh)" != 1.12.3 ] && [ "$(st $x .cc_failed)" != 1.12.3 ]'
echo 0 > "$FAKE_CLAUDE_STATE/stale-left"; for _ in 1 2 3 4; do all_idle; watch1; done
check "the stations all run 1.12.3 after it" 'for s in crew-orchestrator crew-reviewer crew-releaser; do [ "$(runs $s)" = 1.12.3 ] || exit 1; done'

# Tares hears only of stations still retrying the version now installed (F4): one that moved some other way, or
# a mark for an older version, drops off the list at the next round
newcc 1.12.4; echo all > "$FAKE_CLAUDE_STATE/stale-left"; all_idle; watch1; x=$(launched 1)
check "a station that exhausted its tries is listed on Tares" '[ "$(st $x .cc_failed)" = 1.12.4 ] && [ "$(api GET /api/crew | jq -r --arg x "$x" ".claude_retry[\$x].target")" = 1.12.4 ]'
echo 0 > "$FAKE_CLAUDE_STATE/stale-left"; "$F" restart "$x" >/dev/null 2>&1; all_idle
check "it moved by a manual restart and the mark is still there" '[ "$(runs $x)" = 1.12.4 ] && [ "$(st $x .cc_failed)" = 1.12.4 ]'
watch1
check "the next round drops it from Tares" '[ "$(api GET /api/crew | jq -r --arg x "$x" ".claude_retry | has(\$x)")" = false ]'
for _ in 1 2 3 4; do all_idle; watch1; done

# The note for the caller promises a move by the watch only when the watch makes one (F7)
newcc 1.12.5; all_idle; api PUT /api/crew/settings '{"claude_update": false}' >/dev/null
bounded 40 env FACTORY_STATION=crew-orchestrator "$F" crew update --wait 5 >"$T/self-off.out" 2>&1; rc=$bounded_rc
check "claude_update off: the caller is told to restart itself from another session, not that the watch moves it" '[ $rc = 0 ] && grep -q "crew-orchestrator, which ran this command, can only be restarted from another session" "$T/self-off.out" && ! grep -q "moves when" "$T/self-off.out"'
api PUT /api/crew/settings '{"claude_update": true}' >/dev/null
newcc 1.12.6; all_idle; "$F" crew freeze >/dev/null 2>&1
bounded 40 env FACTORY_STATION=crew-orchestrator "$F" crew update --wait 5 >"$T/self-frozen.out" 2>&1; rc=$bounded_rc
check "a frozen crew: the same" '[ $rc = 0 ] && grep -q "crew-orchestrator, which ran this command, can only be restarted from another session" "$T/self-frozen.out" && ! grep -q "moves when" "$T/self-frozen.out"'
"$F" crew unfreeze >/dev/null 2>&1

# A mark for an older version is dropped from Tares, not carried over to the new one (F8): the station is still
# behind (busy here, so not moved this round), but it has not failed for the version now installed
newcc 1.12.7; echo all > "$FAKE_CLAUDE_STATE/stale-left"; all_idle; busy crew-reviewer
jq '.["crew-reviewer"].cc_failed = "1.12.6" | .["crew-reviewer"].cc_fails = 2 | .["crew-reviewer"].cc_retry_at = 4102444800' "$FACTORY_CREW_HOME/stations.json" > "$T/st.json" && mv "$T/st.json" "$FACTORY_CREW_HOME/stations.json"
api PUT '/api/crew/claude-retry?crew=crew' '{"stations":{"crew-reviewer":{"target":"1.12.6","fails":2,"next_at":4102444800}}}' >/dev/null
check "a mark for an older version is listed on Tares before the round" '[ "$(api GET /api/crew | jq -r ".claude_retry[\"crew-reviewer\"].target")" = 1.12.6 ]'
watch1; watch1
check "a mark for an older version: the next settled round drops it from Tares" '[ "$(api GET /api/crew | jq -r ".claude_retry | has(\"crew-reviewer\")")" = false ]'

# crew update on a helper whose restart fails: it says the watch does not revive helpers, not that it revives it
echo 0 > "$FAKE_CLAUDE_STATE/stale-left"; all_idle
"$F" spawn helper crew-reviewer probe2 "look at something else" >"$T/helper2.out" 2>&1; h2=crew-reviewer-probe2
newcc 1.12.8; all_idle; idle $h2
FAKE_CLAUDE_FAIL_RESUME=1 "$F" crew update --wait 5 >"$T/update-helper-down.out" 2>&1 || true
check "crew update, a helper that could not be restarted: the watch is not said to revive it" 'grep -q "could not restart $h2 on Claude Code 1.12.8; the watch does not revive helpers, so ask crew-reviewer to spawn it again" "$T/update-helper-down.out" && ! grep "$h2" "$T/update-helper-down.out" | grep -q "revives it"'

[ $fail = 0 ] && echo "claude update tests passed" || { echo "claude update tests FAILED"; exit 1; }
