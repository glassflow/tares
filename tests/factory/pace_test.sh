#!/usr/bin/env bash
# Checks the pace (TR-674) on a real Tares with a fake claude, feeding fake /usage readings: with the crew
# setting off (unset or false) the watch, spawn and status do exactly what they did, no note, no state
# file; with it on, at 85% of the 5-hour window or 90% of the week no builder is spawned and each station
# is told once to stretch its tick to 45 minutes, at 95% of the window a builder is also told to finish
# its step, push, report and wait, and back below the thresholds each is told once to go back. With
# idle_wake on the liveness wake waits 45 minutes at once and a builder at the hold gets no work wake.
# A station that stopped while a limit was full is not revived into it; after the reset it is revived in
# its own conversation with a short prompt, and in a new one only when that failed. A reading that is
# missing changes nothing.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib.sh"
T=$(cd "$(mktemp -d)" && pwd -P)
mkdir -p "$T/kit-under-test"; cp -R "$here/../../claude-plugin/." "$T/kit-under-test/"; F="$T/kit-under-test/bin/factory"
export HOME="$T/home" FACTORY_CREW_HOME="$T/crew" FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
unset TARES_URL TARES_AUTH_TOKEN TARES_FACTORY_URL TARES_FACTORY_TOKEN_FILE FACTORY_REPO FACTORY_WORKTREES CLAUDE_JOB_DIR TARES_FACTORY_MCP_URL TARES_FACTORY_CRED_FILE TARES_MCP_HOST TARES_MCP_PORT FACTORY_NOW FACTORY_WAKE_SETTLE
mkdir -p "$HOME"
chmod +x "$CLAUDE_BIN"
trap 'stop_tares; rm -rf "$T"' EXIT
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; [ -z "${PACE_DEBUG:-}" ] || { api GET /api/crew/pace; echo; tail -6 "$FACTORY_CREW_HOME/events.jsonl"; tail -8 "$T/watch.log"; cat "$FACTORY_CREW_HOME/pace.json" 2>/dev/null; cat "$T/spawn.out" 2>/dev/null; jq -c ".[] | {id, name, state}" "$FAKE_CLAUDE_STATE/agents.json" 2>/dev/null | tail -8; jq -c ".[\"crew-reviewer\"]" "$FACTORY_CREW_HOME/stations.json"; echo "times: $(times crew-reviewer 2>/dev/null)"; }; fi; }
st() { jq -r --arg s "$1" ".[\$s]$2 // empty" "$FACTORY_CREW_HOME/stations.json"; }
agent() { local tmp; tmp=$(mktemp); jq --arg id "$(st "$1" .id)" "map(if .id == \$id then $2 else . end)" "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"; }
launches() { grep -c '"--bg"' "$FAKE_CLAUDE_STATE/calls.jsonl" 2>/dev/null; true; }
launches_of() { jq -c --arg s "$1" 'select(.args[0] == "--bg" and .args[2] == $s)' "$FAKE_CLAUDE_STATE/calls.jsonl" 2>/dev/null | grep -c . ; true; }
stops() { grep -c '^{"cwd":"[^"]*","args":\["stop"' "$FAKE_CLAUDE_STATE/calls.jsonl" 2>/dev/null; true; }
last() { jq -c "select(.args[2] == \"$1\") | .args" "$FAKE_CLAUDE_STATE/calls.jsonl" | tail -1; }
lastprompt() { last "$1" | jq -r '.[-1]'; }
resumed() { last "$1" | jq -e --arg id "$2" '(index("--resume")) as $i | $i != null and .[$i + 1] == $id' >/dev/null; }
pacen() { jq -c --arg s "$1" --arg k "$2" 'select(.event == "pace" and .station == $s and .detail == $k)' "$FACTORY_CREW_HOME/events.jsonl" 2>/dev/null | grep -c . ; true; }
allpace() { jq -c 'select(.event == "pace")' "$FACTORY_CREW_HOME/events.jsonl" 2>/dev/null | grep -c . ; true; }
wakes() { jq -c --arg s "$1" --arg w "${2:-}" 'select(.event == "idle_wake" and .station == $s and (.detail | startswith($w)))' "$FACTORY_CREW_HOME/events.jsonl" 2>/dev/null | grep -c . ; true; }
wakefile() { echo "$FACTORY_CREW_HOME/stations/$1.wake"; }
wk() { jq -r "$2 // empty" "$(wakefile "$1")" 2>/dev/null; }
pf() { echo "$FACTORY_CREW_HOME/stations/$1.pace"; }
pfv() { jq -r "$2" "$(pf "$1")" 2>/dev/null; }
pacefiles() { ls "$FACTORY_CREW_HOME"/stations/*.pace "$FACTORY_CREW_HOME/pace.json" 2>/dev/null | grep -c . ; true; }

N0=2000000000   # the clock the watch sees, in epoch seconds; rounds step it
SEC=0
B=links-app-build-m1
STATIONS="crew-orchestrator crew-reviewer crew-releaser $B"
idle_all() { local s; for s in $STATIONS; do agent $s '.state = "done"'; done; }
# One watch round at SEC seconds, then every station idle again (as a restarted one is when its turn ends).
round() { SEC=$1; FACTORY_NOW=$((N0 + SEC)) "$F" crew watch --once >>"$T/watch.log" 2>&1; idle_all; }
rr() { round $((SEC + 60)); }   # the next round, a minute later
sim() { # until-seconds step: rounds every step seconds, noting when each station was woken (the state file's last_wake)
  local t s lw
  for s in $STATIONS; do : > "$T/times-$s"; done
  for t in $(seq $((SEC + $2)) "$2" "$1"); do
    round "$t"
    for s in $STATIONS; do
      lw=$(wk $s .last_wake)
      if [ -n "$lw" ] && [ "$lw" != "$(cat "$T/lw-$s" 2>/dev/null)" ]; then echo "$lw" > "$T/lw-$s"; echo $((lw - N0)) >> "$T/times-$s"; fi
    done
  done
}
times() { tr '\n' ' ' < "$T/times-$1" | sed 's/ $//'; }
iso_in() { "$PY" -c 'import sys, datetime; print((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=int(sys.argv[1]))).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$1"; }
# The reading /usage gave: session% weekly% [seconds until the 5-hour window resets, negative = it has]
plan() {
  api POST /ingest/claude_code "$(jq -cn --arg at "$(iso_in 0)" --arg sr "$(iso_in "${3:-10800}")" --arg wr "$(iso_in 259200)" \
    --argjson s "$1" --argjson w "$2" --argjson n "$RANDOM" \
    '{type: "claude_plan", timestamp: $at, nonce: $n, usage_report: {rate_limits: {limits: [
       {kind: "session", percent: $s, resets_at: $sr, scope: null, is_active: true},
       {kind: "weekly_all", percent: $w, resets_at: $wr, scope: null, is_active: false}]}}}')" >/dev/null
}
plan_none() { # a reading with no limit in it
  api POST /ingest/claude_code "$(jq -cn --arg at "$(iso_in 0)" --argjson n "$RANDOM" '{type: "claude_plan", timestamp: $at, nonce: $n, usage_report: {rate_limits: {limits: []}}}')" >/dev/null
}
set_pace() { api PUT /api/crew/settings "$(jq -cn --argjson v "$1" '{pace: $v}')" >/dev/null; }
set_idle_wake() { api PUT /api/crew/settings "$(jq -cn --argjson v "$1" '{idle_wake: $v}')" >/dev/null; }
pace_state() { api GET /api/crew/pace | jq -r '.state'; }
spawn_m2() { "$F" spawn builder "Links App" m2 "$repo" "work T9" >"$T/spawn.out" 2>&1; }

start_tares "$T/tares"
trust "$FACTORY_CREW_HOME"
api PUT /api/crew/settings '{"autonomy": "L3-review", "releaser": true, "release_profile": "kind", "challenger": false, "builders_max": 2}' >/dev/null
"$F" crew up --yes >"$T/up.out" 2>&1 || { echo "FAIL crew up"; cat "$T/up.out"; exit 1; }
repo="$T/links"; mkdir -p "$repo"; git -C "$repo" init -q; repo=$(git -C "$repo" rev-parse --show-toplevel)
P=$(hand_over "Links App" "$repo")
"$F" spawn builder "Links App" m1 "$repo" "work T1" >/dev/null 2>&1
check "the crew and a builder are up" '[ "$(launches)" = 4 ] && [ -n "$(st $B .id)" ]'
idle_all

# ---- the setting off: nothing changes, whatever the plan says ----
check "the setting starts unset, and the pace reads off" '[ "$(api GET /api/crew/settings | jq -c .pace)" = null ] && [ "$(pace_state)" = off ]'
plan 99 99
l0=$(launches); s0=$(stops)
rr; rr; rr
check "unset, with the plan at 99%: three rounds, no note, no restart" '[ "$(allpace)" = 0 ] && [ "$(launches)" = "$l0" ] && [ "$(stops)" = "$s0" ]'
check "unset: no pace file is written" '[ "$(pacefiles)" = 0 ]'
set_pace false
rr; rr
check "false: the same" '[ "$(allpace)" = 0 ] && [ "$(launches)" = "$l0" ] && [ "$(stops)" = "$s0" ] && [ "$(pacefiles)" = 0 ]'
"$F" status >"$T/status-off.out" 2>&1
check "off: status says nothing about the pace" '! grep -qi "pace" "$T/status-off.out"'
spawn_m2; rc=$?
check "off: a builder can be spawned at 99%" '[ $rc = 0 ] && [ -n "$(st links-app-build-m2 .id)" ]'
"$F" retire links-app-build-m2 >/dev/null 2>&1
"$F" restart crew-reviewer >/dev/null 2>&1
check "off: a restart still says to re-arm the schedule, with no 45 minutes in it" 'lastprompt crew-reviewer | grep -q "re-arm your schedule if you have one" && ! lastprompt crew-reviewer | grep -q "45 minutes"'
idle_all

# ---- on, idle_wake off: the thresholds ----
set_pace true
check "the setting reads back true" '[ "$(api GET /api/crew/settings | jq -c .pace)" = true ]'
plan 84 89
rr
check "84% of the window, 89% of the week: nothing" '[ "$(pace_state)" = ok ] && [ "$(allpace)" = 0 ] && [ "$(pacefiles)" = 0 ]'
"$F" status >"$T/status-ok.out" 2>&1
check "status says the pace is ok, with the percents" 'grep -qx "pace: ok (5-hour 84%, week 89%)" "$T/status-ok.out"'
plan 85 89
l0=$(launches)
rr
check "85% of the window: the pace is slow" '[ "$(pace_state)" = slow ]'
check "each station is told once to stretch its tick" '( for s in $STATIONS; do [ "$(pacen $s stretch)" = 1 ] || exit 1; done ) && [ "$(allpace)" = 4 ]'
check "in the same conversation, with the note and what to do" 'p=$(lastprompt crew-reviewer); last crew-reviewer | jq -e "index(\"--resume\") != null" >/dev/null && grep -q "^Pacing: the 5-hour window is at 85%" <<<"$p" && grep -q "Stretch your tick to 45 minutes" <<<"$p" && grep -q "CronList, then CronDelete" <<<"$p"'
check "a builder at 85% is not told to hold" '! lastprompt $B | grep -q "Finish the step you are on" && [ "$(pfv $B .held)" = false ]'
check "each station has a pace file saying it is stretched" '( for s in $STATIONS; do [ "$(pfv $s .stretched)" = true ] || exit 1; done )'
spawn_m2; rc=$?
check "a builder is not spawned, and the refusal says why and until when" '[ $rc != 0 ] && grep -q "no new builder starts now" "$T/spawn.out" && grep -q "the 5-hour window is at 85%" "$T/spawn.out" && grep -q " until 20" "$T/spawn.out"'
"$F" spawn helper "$B" probe "look at T1" >/dev/null 2>&1; rc=$?
check "a helper still can be" '[ $rc = 0 ]'
rr; rr
check "next rounds: not again" '[ "$(allpace)" = 4 ]'
"$F" status >"$T/status-slow.out" 2>&1
check "status says the crew is pacing, and until when" 'grep -q "^pace: slow, no new builders and the longest ticks, until 20" "$T/status-slow.out" && grep -q "(5-hour 85%, week 89%)" "$T/status-slow.out"'
check "a restart now says to re-arm at 45 minutes" '"$F" restart crew-releaser >/dev/null 2>&1; lastprompt crew-releaser | grep -q "re-arm your schedule if you have one, at 45 minutes"'
idle_all

plan 10 10
l0=$(launches)
rr
check "back to 10%: each station is told once to go back to its normal tick" '( for s in $STATIONS; do [ "$(pacen $s release)" = 1 ] || exit 1; done ) && [ "$(allpace)" = 8 ]'
check "the note says so" 'p=$(lastprompt crew-reviewer); grep -q "stopped pacing the crew" <<<"$p" && grep -q "normal interval" <<<"$p" && last crew-reviewer | jq -e "index(\"--resume\") != null" >/dev/null'
check "and the pace files are gone" '[ "$(pacefiles)" = 0 ]'
rr; rr
check "once: nothing more is sent" '[ "$(allpace)" = 8 ]'
spawn_m2; rc=$?
check "a builder can be spawned again" '[ $rc = 0 ] && [ -n "$(st links-app-build-m2 .id)" ]'
"$F" retire links-app-build-m2 >/dev/null 2>&1
idle_all

plan 10 89
rr
check "89% of the week: nothing" '[ "$(pace_state)" = ok ] && [ "$(allpace)" = 8 ]'
plan 10 90
rr
check "90% of the week: slow, and each station is told again, once" '[ "$(pace_state)" = slow ] && ( for s in $STATIONS; do [ "$(pacen $s stretch)" = 2 ] || exit 1; done ) && [ "$(allpace)" = 12 ]'
spawn_m2; rc=$?
check "and a builder is refused, naming the week" '[ $rc != 0 ] && grep -q "the weekly limit is at 90%" "$T/spawn.out"'
rr
check "once" '[ "$(allpace)" = 12 ]'
idle_all

# ---- 95% of the window: a builder holds ----
plan 94 90
rr
check "94%: no hold" '[ "$(pace_state)" = slow ] && [ "$(pacen $B hold)" = 0 ] && [ "$(allpace)" = 12 ]'
plan 95 90
rr
check "95%: the pace is hold" '[ "$(pace_state)" = hold ]'
check "the builder is told, once, to finish its step, push, report where it is and wait" 'p=$(lastprompt $B); [ "$(pacen $B hold)" = 1 ] && grep -q "Finish the step you are on, push, write in your report where you are" <<<"$p" && grep -q "wait" <<<"$p" && [ "$(pfv $B .held)" = true ] && [ "$(pfv $B .stretched)" = true ]'
check "and nobody else is" '( for s in crew-orchestrator crew-reviewer crew-releaser; do [ "$(pacen $s hold)" = 0 ] && [ "$(pfv $s .held)" = false ] || exit 1; done ) && [ "$(allpace)" = 13 ]'
rr; rr
check "once" '[ "$(allpace)" = 13 ] && [ "$(pacen $B hold)" = 1 ]'
"$F" status >"$T/status-hold.out" 2>&1
check "status says builders wait" 'grep -q "^pace: hold, builders finish their step and wait, until 20" "$T/status-hold.out"'

plan 10 10
rr
check "back to 10%: all four are told to go back, the builder once, and its hold is gone" '( for s in $STATIONS; do [ "$(pacen $s release)" = 2 ] || exit 1; done ) && [ "$(pacefiles)" = 0 ]'
plan 96 10
rr
check "96% from nothing: the builder is told to stretch and to hold in one note" 'p=$(lastprompt $B); grep -q "Stretch your tick to 45 minutes" <<<"$p" && grep -q "Finish the step you are on" <<<"$p" && [ "$(pfv $B .held)" = true ] && [ "$(pacen $B stretch)" = 3 ] && [ "$(pacen $B hold)" = 1 ]'
check "the others are only told to stretch" '( for s in crew-orchestrator crew-reviewer crew-releaser; do [ "$(pacen $s stretch)" = 3 ] && [ "$(pfv $s .held)" = false ] || exit 1; done ) && ! lastprompt crew-reviewer | grep -q "Finish the step"'
plan 90 10
rr
check "back down to 90%: the builder is told it may carry on (once), the others hear nothing" '[ "$(pacen $B unhold)" = 1 ] && lastprompt $B | grep -q "room for builders again" && [ "$(pfv $B .held)" = false ] && [ "$(pfv $B .stretched)" = true ] && [ "$(allpace)" = $(( $(allpace) )) ] && [ "$(pacen crew-reviewer unhold)" = 0 ]'
rr
check "once" '[ "$(pacen $B unhold)" = 1 ]'
plan 10 10
rr
idle_all

# ---- idle_wake on: the liveness wake waits 45 minutes at once, a builder at the hold gets no work wake ----
p0=$(allpace)
plan 85 10
set_idle_wake true
SEC=0; N0=$((N0 + 100000)); round 0
check "idle_wake on under pace: each station that holds a tick is told to drop it (the idle wake's release), and no pace note is sent" '[ "$(wakes crew-reviewer release)" = 1 ] && [ "$(wakes crew-orchestrator release)" = 1 ] && [ "$(wakes crew-releaser release)" = 1 ] && [ "$(allpace)" = "$p0" ] && [ "$(pacefiles)" = 0 ]'
for s in $STATIONS; do wk $s .last_wake > "$T/lw-$s"; done   # the release wakes are not liveness wakes
sim 2400 120
check "85%: no liveness wake in 40 minutes (the reviewer's is at 15, the orchestrator's at 20, the releaser's at 30)" '[ -z "$(times crew-reviewer)" ] && [ -z "$(times crew-orchestrator)" ] && [ -z "$(times crew-releaser)" ]'
sim 3000 120
check "it comes at 45 minutes, for each of them" '[ "$(times crew-reviewer)" = 2760 ] && [ "$(times crew-orchestrator)" = 2760 ] && [ "$(times crew-releaser)" = 2760 ]'
check "and is a liveness wake, still in the same conversation" '[ "$(wakes crew-reviewer liveness)" = 1 ] && last crew-reviewer | jq -e "index(\"--resume\") != null" >/dev/null'
api POST "/api/projects/$P/tickets" '{"title": "Store"}' >/dev/null
api POST "/api/projects/$P/tickets/T1/assign" "{\"holder\": \"$B\"}" >/dev/null
api POST "/api/projects/$P/tickets/T1/stage" '{"stage": "changes", "reason": "see the review"}' >/dev/null
b0=$(wakes $B work)
round 3100
check "85%: a work wake still happens (the builder's ticket needs it)" '[ "$(wakes $B work)" = $((b0 + 1)) ] && lastprompt $B | grep -q "Your tickets changed: T1 (changes asked for)"'
plan 95 10
api POST "/api/projects/$P/tickets" '{"title": "Cache"}' >/dev/null
api POST "/api/projects/$P/tickets/T2/assign" "{\"holder\": \"$B\"}" >/dev/null
api POST "/api/projects/$P/tickets/T2/stage" '{"stage": "changes", "reason": "needs a rebase"}' >/dev/null
b1=$(wakes $B work)
round 3160; round 3220; round 3280
check "95%: a builder with a work item is not woken for it" '[ "$(wakes $B work)" = "$b1" ] && [ "$(pace_state)" = hold ]'
check "(and no pace note under idle_wake)" '[ "$(allpace)" = "$p0" ]'
plan 85 10
round 3340
check "when the hold lifts the item it missed is delivered" '[ "$(wakes $B work)" = $((b1 + 1)) ] && lastprompt $B | grep -q "T2 (changes asked for)" && ! lastprompt $B | grep -q "T1 (changes asked for)"'
set_idle_wake false
plan 10 10
round 3400; round 3460
check "idle_wake off again: the stations get their tick back (the idle wake's), and nothing of the pace" '[ "$(wakes crew-reviewer rearm)" = 1 ] && [ "$(allpace)" = "$p0" ]'
api POST "/api/projects/$P/tickets/T1/stage" '{"stage": "doing"}' >/dev/null
api POST "/api/projects/$P/tickets/T2/stage" '{"stage": "doing"}' >/dev/null

# ---- the limit: a stopped station is not revived into it, and is revived in its own conversation after the reset ----
: > "$T/watch.log"
plan 100 10
sidR=$(st crew-reviewer .sessionId); sidL=$(st crew-releaser .sessionId)
agent crew-reviewer '.state = "stopped" | del(.pid)'; agent crew-releaser '.state = "stopped" | del(.pid)'
rl=$(launches_of crew-reviewer); ll=$(launches_of crew-releaser)
rr; rr
check "100%: the pace is the limit" '[ "$(pace_state)" = limit ] && [ "$(api GET /api/crew/pace | jq -r .at_limit)" = true ]'
check "two stopped stations are not revived" '[ "$(launches_of crew-reviewer)" = "$rl" ] && [ "$(launches_of crew-releaser)" = "$ll" ]'
check "the watch says so once, with the reset time" '[ "$(grep -c "the plan.s limit is full until 20" "$T/watch.log")" = 1 ]'
check "and remembers both, and since when" 'jq -e ".stopped | (index(\"crew-reviewer\") != null) and (index(\"crew-releaser\") != null)" "$FACTORY_CREW_HOME/pace.json" >/dev/null && jq -e ".limit_since != null and .[\"until\"] != null" "$FACTORY_CREW_HOME/pace.json" >/dev/null'
"$F" status >"$T/status-limit.out" 2>&1
check "status says the plan is at its limit" 'grep -q "^pace: at the limit, stopped stations wait for the reset, until 20" "$T/status-limit.out"'
check "the stations that run were still paced (the builder told to hold)" '[ "$(pacen crew-orchestrator stretch)" -ge 1 ] && [ "$(pacen $B hold)" -ge 1 ] || [ "$(pacen $B stretch)" -ge 4 ]'
plan 100 10 -60
rr
check "the window has reset: the pace is ok" '[ "$(pace_state)" = ok ]'
check "both are revived, once each" '[ "$(launches_of crew-reviewer)" = $((rl + 1)) ] && [ "$(launches_of crew-releaser)" = $((ll + 1)) ]'
check "in their own conversation" 'resumed crew-reviewer "$sidR" && resumed crew-releaser "$sidL"'
check "with a short prompt that does not tell them to reload the skills" 'p=$(lastprompt crew-reviewer); grep -q "^The plan.s limit was reset. Carry on from where you stopped" <<<"$p" && ! grep -q "Reload the skills" <<<"$p" && ! lastprompt crew-releaser | grep -q "Reload the skills"'
check "and nothing is left to carry out" '[ ! -f "$FACTORY_CREW_HOME/pace.json" ]'
rr
check "no more revives" '[ "$(launches_of crew-reviewer)" = $((rl + 1))  ]'
idle_all
agent crew-orchestrator '.state = "stopped" | del(.pid)'
rr
check "a station stopped for another reason keeps today's revive prompt" 'lastprompt crew-orchestrator | grep -q "^You were revived by the factory under the same name." && ! lastprompt crew-orchestrator | grep -q "limit was reset"'
idle_all

# ---- a resume that fails: the next round starts it fresh ----
: > "$T/watch.log"
touch "$FACTORY_CREW_HOME/stations/$B.done"   # the builder is out of this: a failing resume would hit its note too
plan 100 10
agent crew-reviewer '.state = "stopped" | del(.pid)'
sidR2=$(st crew-reviewer .sessionId); rl=$(launches_of crew-reviewer)
rr
check "the limit again: the stopped station waits" '[ "$(launches_of crew-reviewer)" = "$rl" ] && [ "$(grep -c "the plan.s limit is full" "$T/watch.log")" = 1 ] && jq -e ".stopped | index(\"crew-reviewer\")" "$FACTORY_CREW_HOME/pace.json" >/dev/null'
plan 100 90 -60   # the window reset, the week is at 90%: slow, not the limit, so no one else is told anything
FAKE_CLAUDE_FAIL_RESUME=1 rr
check "the resume fails: said, its conversation is untouched, and marked tried once" 'grep -q "could not revive crew-reviewer" "$T/watch.log" && [ "$(st crew-reviewer .sessionId)" = "$sidR2" ] && jq -e ".tried | length == 1" "$FACTORY_CREW_HOME/pace.json" >/dev/null'
FAKE_CLAUDE_FAIL_RESUME=1 rr
check "one failure does not make the next fresh: it is tried in its own conversation again, and fails again" '[ "$(st crew-reviewer .sessionId)" = "$sidR2" ] && jq -e ".tried | length == 2" "$FACTORY_CREW_HOME/pace.json" >/dev/null'
rl=$(launches_of crew-reviewer)
rr
check "the next round, after two failures in a row, starts it fresh, not in the old conversation" '! last crew-reviewer | jq -e "index(\"--resume\") != null" >/dev/null && [ "$(launches_of crew-reviewer)" = $((rl + 1)) ] && [ "$(st crew-reviewer .sessionId)" != "$sidR2" ]'
check "and says why, and to reload the skills" 'p=$(lastprompt crew-reviewer); grep -q "could not be resumed" <<<"$p" && grep -q "Reload the skills factory-protocol and station-reviewer" <<<"$p"'
check "then nothing is left to carry out" '[ ! -f "$FACTORY_CREW_HOME/pace.json" ]'
# one failed resume, then the station comes up: it stays in its own conversation
plan 100 10
agent crew-reviewer '.state = "stopped" | del(.pid)'
sidR3=$(st crew-reviewer .sessionId)
rr
plan 100 90 -60
FAKE_CLAUDE_FAIL_RESUME=1 rr
rr
check "a single failed resume does not make the next one fresh" 'resumed crew-reviewer "$sidR3" && p=$(lastprompt crew-reviewer) && grep -q "^The plan.s limit was reset" <<<"$p" && ! grep -q "could not be resumed" <<<"$p" && [ ! -f "$FACTORY_CREW_HOME/pace.json" ]'
# two failed resumes, then the limit fills again: the next recovery starts its count over
plan 100 10
agent crew-reviewer '.state = "stopped" | del(.pid)'
sidR4=$(st crew-reviewer .sessionId)
rr
plan 100 90 -60
FAKE_CLAUDE_FAIL_RESUME=1 rr
FAKE_CLAUDE_FAIL_RESUME=1 rr
plan 100 10
rr
check "the limit filling again forgets the failed resumes" 'jq -e ".tried | length == 0" "$FACTORY_CREW_HOME/pace.json" >/dev/null'
plan 100 90 -60
FAKE_CLAUDE_FAIL_RESUME=1 rr
rr
check "so one failed resume in the new recovery is still tried in its own conversation" 'resumed crew-reviewer "$sidR4" && ! lastprompt crew-reviewer | grep -q "could not be resumed"'
rm -f "$FACTORY_CREW_HOME/stations/$B.done"
idle_all

# ---- a reading that is missing changes nothing ----
rr   # the builder is back in the round: stretched with the rest
e0=$(allpace); l0=$(launches)
plan_none
check "no limit in the reading: the pace is not known" '[ "$(api GET /api/crew/pace | jq -r ".on == true and .known == false")" = true ]'
rr; rr
check "unknown: no note and no restart" '[ "$(allpace)" = "$e0" ] && [ "$(launches)" = "$l0" ]'
"$F" status >"$T/status-unknown.out" 2>&1
check "status says there is no reading yet" 'grep -q "^pace: on, no reading of the plan limits yet" "$T/status-unknown.out"'
spawn_m2; rc=$?
check "unknown: a builder can be spawned" '[ $rc = 0 ]'
"$F" retire links-app-build-m2 >/dev/null 2>&1
agent crew-reviewer '.state = "stopped" | del(.pid)'
rr
check "unknown: a stopped station is revived as today" 'lastprompt crew-reviewer | grep -q "^You were revived by the factory under the same name."'
idle_all

# ---- turning it off: each stretched station is told once to go back ----
plan 85 10
rr
check "(the stations are stretched)" '( for s in $STATIONS; do [ "$(pfv $s .stretched)" = true ] || exit 1; done )'
r0=$(pacen crew-releaser release)
set_pace false
rr
check "off while stretched: each is told once to go back" '( for s in $STATIONS; do [ "$(pacen $s release)" -ge 1 ] || exit 1; done ) && [ "$(pacen crew-releaser release)" = $((r0 + 1)) ] && [ "$(pacefiles)" = 0 ]'
a1=$(allpace)
rr; rr
check "and then nothing" '[ "$(allpace)" = "$a1" ]'
spawn_m2; rc=$?
check "off: a builder can be spawned at 85%" '[ $rc = 0 ]'

"$F" retire links-app-build-m2 >/dev/null 2>&1

# ---- the pace file forgets a station that will not be revived ----
spawn_m2; rc=$?
check "(a second builder is up while the pace is off)" '[ $rc = 0 ]'
set_pace true; plan 85 10
agent links-app-build-m2 '.state = "done"'
rr
check "(it was told to stretch its tick, so it has a pace file)" '[ "$(pfv links-app-build-m2 .stretched)" = true ]'
"$F" retire links-app-build-m2 >/dev/null 2>&1
check "retiring a station removes the pace file it had" '[ ! -f "$(pf links-app-build-m2)" ]'
set_pace false; plan 10 10
rr
spawn_m2; rc=$?
set_pace true; plan 100 10
rr
agent links-app-build-m2 '.state = "stopped" | del(.pid)'
rr
check "a builder that stopped at the limit is remembered" 'jq -e ".stopped | index(\"links-app-build-m2\")" "$FACTORY_CREW_HOME/pace.json" >/dev/null'
"$F" retire links-app-build-m2 >/dev/null 2>&1
check "retiring it removes what the pace told it" '[ ! -f "$(pf links-app-build-m2)" ]'
rr
check "the next round forgets it" '! jq -e ".stopped | index(\"links-app-build-m2\")" "$FACTORY_CREW_HOME/pace.json" >/dev/null'
plan 10 10
rr
check "with the limit gone and no one left, the pace file is gone" '[ ! -f "$FACTORY_CREW_HOME/pace.json" ]'
spawn_m2; rc=$?
plan 100 10
rr
agent links-app-build-m2 '.state = "stopped" | del(.pid)'
rr
check "(a builder stopped at the limit again)" 'jq -e ".stopped | index(\"links-app-build-m2\")" "$FACTORY_CREW_HOME/pace.json" >/dev/null'
set_pace false
rr
check "turning the pace off drops the pace file, so it is revived as today" '[ ! -f "$FACTORY_CREW_HOME/pace.json" ]'
rr
check "(and revived with the usual prompt)" 'lastprompt links-app-build-m2 | grep -q "^You were revived by the factory under the same name."'
"$F" retire links-app-build-m2 >/dev/null 2>&1
idle_all

# ---- a note whose restart fails ----
set_pace true; plan 10 10
rr
a0=$(allpace); c0=$(pacen crew-reviewer stretch)
plan 85 10
FAKE_CLAUDE_FAIL_RESUME=1 rr
check "a note whose restart fails sends no event and marks nothing told" '[ "$(allpace)" = "$a0" ] && [ "$(pacefiles)" = 0 ] && grep -q "could not tell crew-reviewer about the pace" "$T/watch.log"'
rr; rr
check "the stations a failed restart left stopped come back already on the stretched tick, with no second note" '[ "$(pacen crew-reviewer stretch)" = "$c0" ] && [ "$(allpace)" = "$a0" ] && ( for s in $STATIONS; do [ "$(pfv $s .stretched)" = true ] || exit 1; done )'
plan 10 10
rr
# a bare failure deep in the restart (the grants file of the new session cannot be written) stops it just the same
api POST /api/grants '{"words": "the reviewer may label pull requests", "scope": "all"}' >/dev/null
rr; rr   # the watch hands the new grant to each station first
gf="$FACTORY_CREW_HOME/stations/crew-reviewer.grants"
c1=$(pacen crew-reviewer stretch)
plan 85 10
rm -f "$gf"; mkdir "$gf"
rr
rmdir "$gf"
check "a restart that fails midway is not marked told either" '[ "$(pacen crew-reviewer stretch)" = "$c1" ] && [ ! -f "$(pf crew-reviewer)" ]'
rr; rr
check "and the note is sent once the failure is gone" '[ "$(pacen crew-reviewer stretch)" = $((c1 + 1)) ] && [ "$(pfv crew-reviewer .stretched)" = true ]'

[ $fail = 0 ] && echo "pace: all cases pass"
exit $fail
