#!/usr/bin/env bash
# Checks one crew for all projects, without Tares: crew up from a home folder, builders in their
# project's repo, revive under the same name, halt and resume, down. Uses a fake claude.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
T=$(mktemp -d)
cp -R "$here/../plugin" "$T/kit-under-test"; F="$T/kit-under-test/bin/factory"
export HOME="$T/home" FACTORY_CREW_HOME="$T/crew" FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
unset TARES_URL TARES_AUTH_TOKEN TARES_PLUGIN_DIR
mkdir -p "$HOME"
chmod +x "$CLAUDE_BIN"
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; fi; }
st() { jq -r --arg s "$1" ".[\$s]$2 // empty" "$FACTORY_CREW_HOME/stations.json"; }
envof() { jq -r --arg k "$2" '.env[$k] // empty' "$FACTORY_CREW_HOME/stations/$1.settings.json"; }
allow_has() { jq -e --arg s "$2" 'any(.autoMode.allow[]; contains($s))' "$FACTORY_CREW_HOME/stations/$1.settings.json" >/dev/null; }
launches() { grep -c '"--bg"' "$FAKE_CLAUDE_STATE/calls.jsonl" 2>/dev/null || echo 0; }

# First run asks the settings one at a time; answers: L3-review, defaults, no challenger, 3 builders, one grant.
printf 'L3-review\n\n\nno\n3\nthe releaser may restart the staging database\n\n' | "$F" crew up >"$T/up.out" 2>&1
rc=$?
check "crew up exits 0" '[ $rc -eq 0 ]'
check "three crew stations started" '[ "$(launches)" = 3 ]'
for r in orchestrator reviewer releaser; do
  check "crew-$r recorded" '[ "$(st crew-$r .status)" = active ]'
  check "crew-$r runs in the crew home" '[ "$(st crew-$r .cwd)" = "$FACTORY_CREW_HOME" ]'
  check "crew-$r env FACTORY_STATION" '[ "$(envof crew-$r FACTORY_STATION)" = crew-$r ]'
  check "crew-$r env FACTORY_ROLE" '[ "$(envof crew-$r FACTORY_ROLE)" = $r ]'
  check "crew-$r env FACTORY_PARENT empty" 'jq -e ".env.FACTORY_PARENT == \"\"" "$FACTORY_CREW_HOME/stations/crew-$r.settings.json" >/dev/null'
  check "crew-$r has no TARES_PROJECT" '[ -z "$(envof crew-$r TARES_PROJECT)" ]'
  check "crew-$r FACTORY_TARES off" '[ "$(envof crew-$r FACTORY_TARES)" = off ]'
done
check "no comms by default" '[ -z "$(st crew-comms .id)" ]'
check "Rius on by default" '[ "$(jq -r .rius.enabled "$FACTORY_CREW_HOME/crew.json")" = true ]'
check "orchestrator started first" '[ "$(jq -r "select(.args[0] == \"--bg\") | .args[2]" "$FAKE_CLAUDE_STATE/calls.jsonl" | head -1)" = crew-orchestrator ]'
check "settings saved in crew.json" '[ "$(jq -c ".settings | [.autonomy, .release_profile, .challenger, .builders_max]" "$FACTORY_CREW_HOME/crew.json")" = "[\"L3-review\",\"kind\",false,3]" ]'
today=$(date -u +%Y-%m-%d)
check "grant saved as Tares writes it" 'grep -qx -- "- \"the releaser may restart the staging database\" ($today)" "$FACTORY_CREW_HOME/GRANTS.md"'
check "grant quoted in the allow rules" 'allow_has crew-releaser "granted this on $today for all projects, in their own words: \"the releaser may restart the staging database\". Acting"'
check "template example is not a grant" '! jq -e ".autoMode.allow | any(contains(\"when a migration needs it\"))" "$FACTORY_CREW_HOME/stations/crew-releaser.settings.json" >/dev/null'
check "orchestrator may spawn" 'jq -e ".permissions.allow | index(\"Bash(factory spawn *)\")" "$FACTORY_CREW_HOME/stations/crew-orchestrator.settings.json" >/dev/null'
check "stations launch with a 200k autocompact window" 'jq -e "select(.args[2] == \"crew-orchestrator\") | .args | index(\"--autocompact\") as \$i | .[\$i+1] == \"200000\"" "$FAKE_CLAUDE_STATE/calls.jsonl" >/dev/null'
check "offline desk files in the crew home" '[ -f "$FACTORY_CREW_HOME/DESK.md" ] && [ -f "$FACTORY_CREW_HOME/DECISIONS.md" ]'

# The desk: status shows the next question with its options, crew desk shows every question in full.
check "empty desk: crew desk says nothing is open" 'grep -q "Nothing open on the desk" <<<"$("$F" crew desk 2>&1)"'
check "empty desk: status says nothing is open" 'grep -q "^desk: nothing open" <<<"$("$F" crew status 2>&1)"'
{ head -3 "$FACTORY_CREW_HOME/DESK.md"; cat <<'DESK'
- options: a header line, not a question
## D4: keep the old endpoint? (added 2026-10-08T18:02Z, blocking)
- project: p1. Two callers still use it.
- recommendation: A, since two callers remain.
- options:
  - A) keep it until v2 ships
  - B) drop it now
- blocks: T7

## D5: rename the bucket? (added 2026-10-08T18:05Z, not blocking)
- recommendation: B
- options: A) rename now; B) leave it
DESK
} > "$T/desk.md" && mv "$T/desk.md" "$FACTORY_CREW_HOME/DESK.md"
"$F" crew status >"$T/status-desk.out" 2>&1
"$F" crew desk >"$T/desk.out" 2>&1
check "status counts the open questions" 'grep -q "^desk: 2 open" "$T/status-desk.out"'
check "status shows the next question" 'grep -q "^  D4: keep the old endpoint?" "$T/status-desk.out"'
check "status shows its recommendation" 'grep -q "^    recommendation: A, since two callers remain" "$T/status-desk.out"'
check "status shows each option, also on lines of their own" 'grep -q "A) keep it until v2 ships" "$T/status-desk.out" && grep -q "B) drop it now" "$T/status-desk.out"'
check "status leaves out the context and the later questions" '! grep -q -e "Two callers" -e "blocks: T7" -e "D5" "$T/status-desk.out"'
check "status skips the file header" '! grep -q "a header line" "$T/status-desk.out"'
check "crew desk shows every question in full" 'grep -q "^## D4" "$T/desk.out" && grep -q "Two callers" "$T/desk.out" && grep -q "^- options: A) rename now; B) leave it" "$T/desk.out"'
check "crew desk leaves out the file header" '! grep -q -e "^# Desk" -e "a header line" "$T/desk.out"'
check "crew desk says where to answer" 'grep -q "factory crew attach orchestrator" "$T/desk.out"'
check "factory desk is short for crew desk" '[ "$("$F" desk 2>&1)" = "$(cat "$T/desk.out")" ]'

# The same, written another way: bold and capitalised labels, a * bullet, options as flat bullets,
# Windows line endings, and an indented line under a later bullet.
{ head -3 "$FACTORY_CREW_HOME/DESK.md"; cat <<'DESK'
## D6: move the cron? (added 2026-10-08T19:00Z, not blocking)
- Two jobs collide at 03:00.
- **Recommendation:** B, the quieter hour.
* Options:
- A) keep 03:00
- B) move to 04:00
- blocks: T9
  more about T9
DESK
} | sed 's/$/\r/' > "$T/desk.md" && mv "$T/desk.md" "$FACTORY_CREW_HOME/DESK.md"
"$F" crew status >"$T/status-desk2.out" 2>&1
check "variant: status shows the recommendation without the bold" 'grep -q "^    Recommendation: B, the quieter hour" "$T/status-desk2.out"'
check "variant: status shows the options label and the flat options" 'grep -q "^    Options:" "$T/status-desk2.out" && grep -q "A) keep 03:00" "$T/status-desk2.out" && grep -q "B) move to 04:00" "$T/status-desk2.out"'
check "variant: status stops at the next bullet" '! grep -q -e "blocks: T9" -e "more about T9" -e "collide" "$T/status-desk2.out"'
check "variant: no carriage returns" '! grep -q $'"'"'\r'"'"' "$T/status-desk2.out"'

# A second run asks nothing and starts nothing new.
"$F" crew up </dev/null >"$T/up2.out" 2>&1
check "second crew up asks nothing" '! grep -q "recommended:" "$T/up2.out"'
check "second crew up starts nothing" '[ "$(launches)" = 3 ]'

# Builders start inside their project's repo, with the project in their env.
repo="$T/links"; mkdir -p "$repo"; git -C "$repo" init -q
repo=$(git -C "$repo" rev-parse --show-toplevel)
"$F" spawn builder "Links App" m1 "$repo" "work T1 and T2" >/dev/null 2>&1
b=links-app-build-m1
check "builder recorded" '[ "$(st $b .role)" = builder ]'
check "builder runs in its repo" '[ "$(jq -r "select(.args[2] == \"$b\") | .cwd" "$FAKE_CLAUDE_STATE/calls.jsonl")" = "$repo" ]'
check "builder TARES_PROJECT" '[ "$(envof $b TARES_PROJECT)" = "Links App" ]'
check "builder FACTORY_PARENT" '[ "$(envof $b FACTORY_PARENT)" = crew-orchestrator ]'
check "builder FACTORY_REPO" '[ "$(envof $b FACTORY_REPO)" = "$repo" ]'
check "builder worktrees in its repo" '[ "$(envof $b FACTORY_WORKTREES)" = "$repo/.factory/worktrees" ]'
check "project recorded" '[ "$(jq -r ".[\"Links App\"].repo" "$FACTORY_CREW_HOME/projects.json")" = "$repo" ]'
check "L3: builder waits for the relayed yes" 'jq -e ".autoMode.allow | any(test(\"relayed .*yes\"))" "$FACTORY_CREW_HOME/stations/$b.settings.json" >/dev/null'
check "L3: the no-CI clause holds at L3 too" 'jq -e ".autoMode.allow | any(test(\"relayed .*yes\") and contains(\"no CI checks configured\"))" "$FACTORY_CREW_HOME/stations/$b.settings.json" >/dev/null'
check "L3: no pre-authorized merge" '! jq -e ".autoMode.allow | any(test(\"pre-authorized at level\"))" "$FACTORY_CREW_HOME/stations/$b.settings.json" >/dev/null'
"$F" spawn builder "Links App" m1 "$repo" "again" >/dev/null 2>&1
check "no duplicate builder" '[ "$(launches)" = 4 ]'
repo2="$T/shop"; mkdir -p "$repo2"; git -C "$repo2" init -q
"$F" spawn builder shop m1 "$repo2" "work T1" >/dev/null 2>&1
"$F" spawn builder shop m2 "$repo2" "work T2" >/dev/null 2>&1
repo3="$T/blog"; mkdir -p "$repo3"; git -C "$repo3" init -q
"$F" spawn builder blog m1 "$repo3" "work T1" >"$T/max.out" 2>&1
check "builders_max is honoured" 'grep -q "builders_max is 3" "$T/max.out"'
"$F" spawn builder "Crew Tools" m1 "$repo3" "x" >"$T/crewname.out" 2>&1
check "a project can't be named like the crew" 'grep -q "can.t start with" "$T/crewname.out" && [ -z "$(st crew-tools-build-m1 .role)" ]'
check "roster lists project" '"$F" roster | grep -q "^links-app-build-m1	builder	Links App	working$"'

# A helper keeps the parent's project and works under its role.
"$F" spawn helper "$b" rebase "rebase T2" >/dev/null 2>&1
check "helper name" '[ "$(st $b-rebase .role)" = helper ]'
check "helper parent" '[ "$(envof $b-rebase FACTORY_PARENT)" = $b ]'
check "helper project" '[ "$(envof $b-rebase TARES_PROJECT)" = "Links App" ]'
"$F" retire "$b-rebase" >/dev/null 2>&1
before=$(launches)
"$F" revive "$b-rebase" >"$T/retired.out" 2>&1
check "a retired station is not revived" '[ "$(launches)" = "$before" ] && grep -q "was retired" "$T/retired.out" && [ ! -e "$FACTORY_CREW_HOME/stations/$b-rebase.lock" ]'

# watch revives a stopped station under the same name, resuming its conversation.
old=$(st crew-reviewer .id); sess=$(st crew-reviewer .sessionId)
tmp=$(mktemp); jq --arg id "$old" 'map(if .id == $id then .state = "stopped" else . end)' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"
"$F" crew watch --once >/dev/null 2>&1
new=$(st crew-reviewer .id)
check "reviewer revived with a new id" '[ -n "$new" ] && [ "$new" != "$old" ]'
check "revive resumed the conversation" 'jq -e --arg s "$sess" "select(.args[2] == \"crew-reviewer\") | .args | index(\"--resume\") as \$i | \$i != null and .[\$i+1] == \$s" "$FAKE_CLAUDE_STATE/calls.jsonl" >/dev/null'
check "earlier session kept in history" '[ "$(jq -r ".[\"crew-reviewer\"].earlier[0].id" "$FACTORY_CREW_HOME/stations.json")" = "$old" ]'
check "revived env unchanged" '[ "$(envof crew-reviewer FACTORY_STATION)" = crew-reviewer ]'
check "other stations untouched" '[ "$(grep -c "\"--resume\"" "$FAKE_CLAUDE_STATE/calls.jsonl")" = 1 ]'

# A session whose last turn ended and whose process is gone (Claude Code retired it for idling) is
# revived; one that ended its turn but still runs is not.
id=$(st crew-reviewer .id); tmp=$(mktemp)
jq --arg id "$id" 'map(if .id == $id then .state = "done" | .pid = 4242 else . end)' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"
"$F" crew watch --once >/dev/null 2>&1
check "a done session still running is left alone" '[ "$(st crew-reviewer .id)" = "$id" ]'
tmp=$(mktemp); jq --arg id "$id" 'map(if .id == $id then del(.pid) else . end)' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"
"$F" crew watch --once >/dev/null 2>&1
check "a done session with no process is revived" '[ "$(st crew-reviewer .id)" != "$id" ] && [ "$(jq -r ".[\"crew-reviewer\"].earlier | length" "$FACTORY_CREW_HOME/stations.json")" = 2 ]'

# With no process left anywhere in the list, every finished station is still revived: whether a session
# runs is decided per session, never from what the rest of the list holds.
expect=$(jq '[to_entries[] | select(.value.status == "active" and .value.role != "helper")] | length' "$FACTORY_CREW_HOME/stations.json")
tmp=$(mktemp); jq 'map(.state = "done" | del(.pid))' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"
before=$(launches)
"$F" crew watch --once >/dev/null 2>&1
check "with no process left anywhere, every finished station is revived" '[ $(( $(launches) - before )) = "$expect" ]'

# A session list that can't be read is never taken for an empty one.
stopped() { local tmp; tmp=$(mktemp); jq --arg id "$(st "$1" .id)" 'map(if .id == $id then .state = "stopped" | del(.pid) else . end)' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"; }
stops() { grep -c '"args":\["stop"' "$FAKE_CLAUDE_STATE/calls.jsonl" || true; }
stopped crew-reviewer; before=$(launches); s0=$(stops)
FAKE_CLAUDE_AGENTS_FAIL=1 "$F" crew watch --once >"$T/unread.out" 2>&1
rc=$?
check "unreadable session list: the watch starts nothing and carries on" '[ $rc = 0 ] && [ "$(launches)" = "$before" ] && grep -q "could not read" "$T/unread.out"'
FAKE_CLAUDE_AGENTS_FAIL=1 "$F" revive crew-reviewer >"$T/unread.out" 2>&1
check "unreadable session list: revive refuses to guess" '[ "$(launches)" = "$before" ] && grep -q "could not read" "$T/unread.out"'
FAKE_CLAUDE_AGENTS_FAIL=1 "$F" restart crew-orchestrator >/dev/null 2>&1
check "unreadable session list: restart stops nothing" '[ "$(stops)" = "$s0" ] && [ "$(launches)" = "$before" ]'
FAKE_CLAUDE_AGENTS_FAIL=1 "$F" crew status >"$T/unread.out" 2>&1
check "unreadable session list: status says so instead of showing every station gone" 'grep -q "could not read" "$T/unread.out" && ! grep -q " gone " "$T/unread.out"'
"$F" crew watch --once >/dev/null 2>&1
check "readable again: the next round revives it" '[ $(( $(launches) - before )) = 1 ]'

# One command at a time starts or stops a station. A restart and the watch's revive once both found the
# same station down and both started it: two live sessions under one name.
lock="$FACTORY_CREW_HOME/stations/crew-reviewer.lock"
sleep 60 & holder=$!
mkdir "$lock"; echo "$holder" > "$lock/pid"
stopped crew-reviewer; before=$(launches)
"$F" revive crew-reviewer >"$T/lock.out" 2>&1
check "revive leaves a station another command is starting alone" '[ "$(launches)" = "$before" ] && grep -q "another factory command (pid $holder)" "$T/lock.out"'
"$F" crew watch --once >/dev/null 2>&1
check "so does the watch" '[ "$(launches)" = "$before" ]'
kill "$holder"; wait "$holder" 2>/dev/null
"$F" crew watch --once >/dev/null 2>&1
check "a lock left by a command that is gone is taken over, and released" '[ $(( $(launches) - before )) = 1 ] && [ ! -e "$lock" ]'
sh -c 'exit 0' & dead=$!; wait "$dead"
mkdir "$lock" "$lock.takeover"; echo "$dead" > "$lock/pid"
stopped crew-reviewer; before=$(launches)
"$F" revive crew-reviewer >/dev/null 2>&1
check "so is one whose takeover was cut off by a kill" '[ $(( $(launches) - before )) = 1 ] && [ ! -e "$lock" ] && [ ! -e "$lock.takeover" ]'
s0=$(stops); before=$(launches)
FAKE_CLAUDE_BG_DELAY=3 FACTORY_STOP_WAIT=2 "$F" restart crew-reviewer >"$T/race-restart.out" 2>&1 &
racer=$!
for _ in $(seq 100); do [ "$(stops)" != "$s0" ] && break; sleep 0.1; done
FAKE_CLAUDE_BG_DELAY=3 "$F" crew watch --once >"$T/race-watch.out" 2>&1
wait "$racer"
check "a restart racing the watch: one session under the name" '[ "$(jq "[.[] | select(.name == \"crew-reviewer\" and has(\"pid\"))] | length" "$FAKE_CLAUDE_STATE/agents.json")" = 1 ] && [ $(( $(launches) - before )) = 1 ]'
check "restart and watch: the restart started it, the watch was turned away" 'grep -q "started crew-reviewer" "$T/race-restart.out" && grep -q "another factory command" "$T/race-watch.out"'
# restart holds the lock from before it stops the station, not only once it has stopped.
s0=$(stops)
FAKE_CLAUDE_STOP_DELAY=2 FACTORY_STOP_WAIT=4 "$F" restart crew-reviewer >/dev/null 2>&1 &
racer=$!
for _ in $(seq 100); do [ "$(stops)" != "$s0" ] && break; sleep 0.1; done
check "restart holds the lock while it stops the station" '[ -d "$lock" ] && kill -0 "$(cat "$lock/pid")" 2>/dev/null'
wait "$racer"
# A dead station reads as gone, so the orchestrator's roster check revives it even with no watch running.
agent_gone() { local tmp; tmp=$(mktemp); jq --arg id "$(st "$1" .id)" 'map(if .id == $id then .state = "done" | del(.pid) else . end)' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"; }
agent_gone crew-reviewer
check "roster shows a finished station with no process as gone" '"$F" roster | grep -q "^crew-reviewer	reviewer		gone$"'
"$F" revive crew-reviewer >/dev/null 2>&1
dup=$(st crew-reviewer .id)
tmp=$(mktemp); jq --arg id "$dup" '. + [(.[] | select(.id == $id) | .id = "0000dup1")]' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"
check "status shows two live sessions under one name" 'grep -q "TWO SESSIONS named crew-reviewer: .*0000dup1" <<<"$("$F" crew status 2>/dev/null)"'
check "and keeps the one it started" '[ "$(st crew-reviewer .id)" = "$dup" ]'
tmp=$(mktemp); jq 'map(if .id == "0000dup1" then del(.pid, .status) | .state = "working" else . end)' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"
check "a gone row under the same name is not a second session" '! grep -q "TWO SESSIONS" <<<"$("$F" crew status 2>/dev/null)"'
tmp=$(mktemp); jq 'map(select(.id != "0000dup1"))' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"

# A station with a schedule armed stays listed as "working" after its process is stopped: it is gone, so
# a restart goes through and the watch revives it. One waiting on a person with no process is left be.
before=$(launches)
FAKE_CLAUDE_STOP_KEEPS_STATE=1 FACTORY_STOP_WAIT=2 "$F" restart crew-orchestrator >"$T/cron-restart.out" 2>&1
check "restart of a station with a schedule armed goes through" '[ $(( $(launches) - before )) = 1 ] && ! grep -q "did not stop" "$T/cron-restart.out"'
tmp=$(mktemp); jq --arg id "$(st crew-orchestrator .id)" 'map(if .id == $id then .state = "working" | del(.pid, .status) else . end)' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"
check "listed as working with no process: roster says gone" '"$F" roster | grep -q "^crew-orchestrator	orchestrator		gone$"'
before=$(launches)
"$F" crew watch --once >/dev/null 2>&1
check "listed as working with no process: revived" '[ $(( $(launches) - before )) = 1 ]'
tmp=$(mktemp); jq --arg id "$(st crew-orchestrator .id)" 'map(if .id == $id then .state = "blocked" | del(.pid, .status) else . end)' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"
before=$(launches)
"$F" crew watch --once >/dev/null 2>&1
check "waiting on a person with no process: left alone" '[ "$(launches)" = "$before" ]'
tmp=$(mktemp); jq --arg id "$(st crew-orchestrator .id)" 'map(if .id == $id then .state = "working" | .pid = 4747 else . end)' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"

# A failed revive is reported and the watcher carries on with the next station.
earlier() { jq -r --arg s "$1" '.[$s].earlier // [] | length' "$FACTORY_CREW_HOME/stations.json"; }
e0=$(( $(earlier crew-orchestrator) + $(earlier crew-releaser) ))
for s in crew-orchestrator crew-releaser; do
  id=$(st $s .id); tmp=$(mktemp); jq --arg id "$id" 'map(if .id == $id then .state = "stopped" else . end)' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"
done
FAKE_CLAUDE_FAIL_RESUME=1 "$F" crew watch --once >"$T/watch.out" 2>&1
rc=$?
check "watcher survives a failed revive" '[ $rc = 0 ] && [ "$(grep -c "could not revive" "$T/watch.out")" = 2 ]'
"$F" crew watch --once >/dev/null 2>&1
check "next round revives them" '[ $(( $(earlier crew-orchestrator) + $(earlier crew-releaser) )) = $((e0 + 2)) ]'

# A finished builder (it leaves stations/<name>.done) and helpers are not revived.
"$F" spawn helper "$b" probe "look at T3" >/dev/null 2>&1
for s in "$b" shop-build-m1 "$b-probe"; do
  id=$(st "$s" .id); tmp=$(mktemp); jq --arg id "$id" 'map(if .id == $id then .state = "stopped" else . end)' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"
done
touch "$FACTORY_CREW_HOME/stations/$b.done"
before=$(launches); eb=$(earlier "$b"); es=$(earlier shop-build-m1)
"$F" crew watch --once >/dev/null 2>&1
check "only the unfinished builder is revived" '[ $(( $(launches) - before )) = 1 ] && [ "$(earlier shop-build-m1)" = $((es + 1)) ] && [ "$(earlier "$b")" = "$eb" ]'
"$F" retire "$b-probe" >/dev/null 2>&1

# restart waits for the stop before resuming. The kit installed from any marketplace is not loaded twice.
mkdir -p "$HOME/.claude"; echo '{"enabledPlugins": {"tares-factory@glassflow": true}}' > "$HOME/.claude/settings.json"
er=$(earlier crew-reviewer)
FACTORY_STOP_WAIT=2 "$F" restart reviewer >/dev/null 2>&1
check "restart resumes the reviewer" '[ "$(earlier crew-reviewer)" = $((er + 1)) ]'
check "installed kit: no --plugin-dir" '[ "$(jq -r "select(.args[2] == \"crew-reviewer\") | .args | index(\"--plugin-dir\") != null" "$FAKE_CLAUDE_STATE/calls.jsonl" | tail -1)" = false ]'
rm -f "$HOME/.claude/settings.json"

# The snippet takes any project name.
check "snippet escapes the project name" '"$F" snippet "a|b & c" | grep -q "crew on a|b & c$"'

# halt blocks risky actions in every station until resume.
"$F" crew halt "testing" >/dev/null 2>&1
check "halt file" '[ -f "$FACTORY_CREW_HOME/HALT" ]'
"$F" spawn builder blog m1 "$repo3" "x" >"$T/halt.out" 2>&1
check "spawn refused while halted" 'grep -q "halted" "$T/halt.out"'
out=$(jq -n '{tool_name: "Bash", tool_input: {command: "gh pr merge 3 --squash"}}' | FACTORY_ROLE=builder FACTORY_HOME="$FACTORY_CREW_HOME" bash "$here/../plugin/hooks/guard.sh")
check "guard denies merge while halted" 'grep -q "halted" <<<"$out"'
"$F" crew resume >/dev/null 2>&1
out=$(jq -n '{tool_name: "Bash", tool_input: {command: "gh pr merge 3 --squash"}}' | FACTORY_ROLE=builder FACTORY_HOME="$FACTORY_CREW_HOME" bash "$here/../plugin/hooks/guard.sh")
check "guard allows merge after resume" '[ -z "$out" ]'

ctx=$(echo '{"source": "resume"}' | env $(jq -r '.env | to_entries[] | "\(.key)=\(.value)"' "$FACTORY_CREW_HOME/stations/$b.settings.json" | tr ' ' '_') bash "$here/../plugin/hooks/session-start.sh")
check "session start names the station and project" 'grep -q "links-app-build-m1" <<<"$ctx" && grep -q "Links_App" <<<"$ctx" && grep -q "resumed" <<<"$ctx"'

# A station Claude Code stopped for thrashing on autocompact is started fresh, not resumed; one whose
# last reply is ordinary is left alone. A window under 150k is raised to 150k.
transcript() { mkdir -p "$HOME/.claude/projects/-crew"; echo "$2" >> "$HOME/.claude/projects/-crew/$(st "$1" .sessionId).jsonl"; }
transcript crew-orchestrator '{"type":"assistant","message":{"model":"claude-opus-5-5","content":[{"type":"text","text":"tick done"}]}}'
transcript crew-orchestrator '{"type":"system","subtype":"compact_boundary"}'
transcript crew-orchestrator '{"type":"assistant","message":{"model":"<synthetic>","content":[{"type":"text","text":"Autocompact is thrashing: the context refilled to the limit within 3 turns of the previous compact, 3 times in a row."}]}}'
transcript crew-reviewer '{"type":"assistant","message":{"model":"<synthetic>","content":[{"type":"text","text":"Autocompact is thrashing: old"}]}}'
transcript crew-reviewer '{"type":"assistant","message":{"model":"claude-opus-5-5","content":[{"type":"text","text":"review posted"}]}}'
agent() { local tmp; tmp=$(mktemp); jq --arg id "$(st "$1" .id)" "map(if .id == \$id then $2 else . end)" "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"; }
# Its schedule is armed, so the list's state says "working"; its status says the turn is over.
agent crew-orchestrator '.state = "working" | .status = "idle"'
check "status shows the stuck station" '"$F" crew status 2>/dev/null | grep "^crew-orchestrator " | grep -q "STUCK on autocompact"'
agent crew-orchestrator '.status = "busy"'; rm -f "$FAKE_CLAUDE_STATE/agents-reads"
check "a list that fails mid-way never reads as stuck" '! FAKE_CLAUDE_AGENTS_FAIL_AFTER=2 "$F" crew status 2>/dev/null | grep "^crew-orchestrator " | grep -q STUCK'
agent crew-orchestrator '.state = "stopped" | .status = "busy"'
check "a stopped row that kept its status reads as gone" '[ "$("$F" crew status 2>/dev/null | awk "\$1 == \"crew-orchestrator\" {print \$4}")" = gone ]'
agent crew-orchestrator '.state = "working" | .status = "idle"'
check "status shows where its turn is, not the agent list's working" '[ "$("$F" crew status 2>/dev/null | awk "\$1 == \"crew-orchestrator\" {print \$4}")" = done ]'
check "status leaves a recovered station alone" '! "$F" crew status 2>/dev/null | grep "^crew-reviewer " | grep -q STUCK'
jq '.limits.autocompact_tokens = 100000' "$FACTORY_CREW_HOME/crew.json" > "$T/c.json" && mv "$T/c.json" "$FACTORY_CREW_HOME/crew.json"
old=$(st crew-orchestrator .id); rev=$(st crew-reviewer .id); before=$(launches)
FACTORY_STOP_WAIT=2 "$F" crew watch --once >"$T/thrash.out" 2>&1
last() { jq -c "select(.args[2] == \"$1\") | .args" "$FAKE_CLAUDE_STATE/calls.jsonl" | tail -1; }
check "thrashing station restarted" '[ "$(st crew-orchestrator .id)" != "$old" ] && grep -q "crew-orchestrator is stuck" "$T/thrash.out"'
check "restarted in a new conversation" '! last crew-orchestrator | jq -e "index(\"--resume\")" >/dev/null && last crew-orchestrator | grep -q "in a new conversation"'
check "only the stuck station restarted" '[ $(( $(launches) - before )) = 1 ] && [ "$(st crew-reviewer .id)" = "$rev" ]'
check "thrash recorded" 'grep -q "\"event\":\"thrashing\",.*crew-orchestrator\|crew-orchestrator.*\"event\":\"thrashing\"" "$FACTORY_CREW_HOME/events.jsonl" && grep -q restarted-fresh "$FACTORY_CREW_HOME/events.jsonl"'
check "window under 150k raised" 'last crew-orchestrator | jq -e "index(\"--autocompact\") as \$i | .[\$i+1] == \"150000\"" >/dev/null && grep -q "stations use 150000" "$T/thrash.out"'
"$F" crew watch --once >"$T/thrash2.out" 2>&1
check "the new conversation is not restarted again" '! grep -q "is stuck" "$T/thrash2.out"'

# Woken by a new message after the notice, or still working on one: left alone.
thrash='{"type":"assistant","message":{"model":"<synthetic>","content":[{"type":"text","text":"Autocompact is thrashing: x"}]}}'
transcript crew-releaser "$thrash"
transcript crew-releaser '{"type":"user","message":{"role":"user","content":"carry on with T4"}}'
agent crew-releaser '.state = "done"'
rel=$(st crew-releaser .id)
"$F" crew watch --once >/dev/null 2>&1
check "a station woken after the notice is left alone" '[ "$(st crew-releaser .id)" = "$rel" ]'
transcript crew-releaser "$thrash"; agent crew-releaser '.state = "working"'
"$F" crew watch --once >/dev/null 2>&1
check "a station working on a new message is left alone" '[ "$(st crew-releaser .id)" = "$rel" ]'
agent crew-releaser '.state = "done" | .status = "busy"'
"$F" crew watch --once >/dev/null 2>&1
check "busy by the list's status: left alone, whatever its state says" '[ "$(st crew-releaser .id)" = "$rel" ]'
agent crew-releaser 'del(.status)'

# After /clear the live session has a new transcript: the old one's notice no longer counts.
agent crew-releaser '.state = "done"'
jq '.sessionId = "session-cleared"' "$HOME/.claude/jobs/$rel/state.json" > "$T/js" && mv "$T/js" "$HOME/.claude/jobs/$rel/state.json"
"$F" crew watch --once >/dev/null 2>&1
check "a cleared session is not restarted" '[ "$(st crew-releaser .id)" = "$rel" ]'

# A stuck station that stopped before the watch saw it comes back in a new conversation, not resumed.
jq --arg s "$(st crew-releaser .sessionId)" '.sessionId = $s' "$HOME/.claude/jobs/$rel/state.json" > "$T/js" && mv "$T/js" "$HOME/.claude/jobs/$rel/state.json"
agent crew-releaser '.state = "stopped" | del(.pid)'
jq '.state = "working"' "$HOME/.claude/jobs/$rel/state.json" > "$T/js" && mv "$T/js" "$HOME/.claude/jobs/$rel/state.json" # frozen when it died
"$F" crew watch --once >/dev/null 2>&1
check "a stopped stuck station comes back fresh" '[ "$(st crew-releaser .id)" != "$rel" ] && ! last crew-releaser | jq -e "index(\"--resume\")" >/dev/null'

# A stuck helper is left to its parent: its task was only in its first prompt.
"$F" spawn helper crew-reviewer probe "look at PR 9" >/dev/null 2>&1
transcript crew-reviewer-probe "$thrash"; agent crew-reviewer-probe '.state = "done"'
hp=$(st crew-reviewer-probe .id)
"$F" crew watch --once >/dev/null 2>&1
check "a stuck helper is not restarted" '[ "$(st crew-reviewer-probe .id)" = "$hp" ]'
"$F" retire crew-reviewer-probe >/dev/null 2>&1

# An updated kit: each quiet station moves onto it, one per round, in the same conversation. A busy one,
# one with a subagent running, and one waiting on a person for under half an hour wait; a session cron
# doesn't count as work. Nothing moves while the crew is halted, or while a station is down.
check "stations record the kit they run on" '[ -n "$(st crew-reviewer .kit)" ]'
older() { local tmp; tmp=$(mktemp); jq --arg s "$1" '.[$s].kit = "older"' "$FACTORY_CREW_HOME/stations.json" > "$tmp" && mv "$tmp" "$FACTORY_CREW_HOME/stations.json"; }
job() { local f="$HOME/.claude/jobs/$(st "$1" .id)/state.json"; jq "$2" "$f" > "$T/js" && mv "$T/js" "$f"; }
moved() { [ "$(st "$1" .id)" != "$2" ]; }
for s in crew-orchestrator crew-reviewer crew-releaser; do older $s; done
check "status counts stations on an older kit" 'grep -q "^kit: 3 station(s) run an older version" <<<"$("$F" crew status 2>/dev/null)"'
# With a schedule armed the list's state says "working" even between turns; its status has the turn. On a
# permission prompt the list says "waiting" while the job file still says "working".
agent crew-orchestrator '.state = "working" | .status = "busy"'
agent crew-reviewer '.state = "working" | .status = "idle"'; job crew-reviewer '.inFlight = {tasks: 2, queued: 0, kinds: ["local_agent", "session_cron"]}'
agent crew-releaser '.state = "working" | .status = "waiting"'; job crew-releaser '.state = "working" | .updatedAt = (now | todate)'
o=$(st crew-orchestrator .id); r=$(st crew-reviewer .id); l=$(st crew-releaser .id)
"$F" crew watch --once >/dev/null 2>&1
check "a working station waits" '! moved crew-orchestrator "$o"'
check "a station with a subagent running waits" '! moved crew-reviewer "$r"'
check "a station waiting on a person waits" '! moved crew-releaser "$l"'
job crew-reviewer '.inFlight = {tasks: 1, queued: 0, kinds: ["session_cron"], wake: {at: 1, keepalive: true}}'
"$F" crew watch --once >/dev/null 2>&1
check "a station waiting for its own wake-up waits" '! moved crew-reviewer "$r"'
job crew-reviewer '.inFlight = {tasks: 1, queued: 0, kinds: ["session_cron"]}'
job crew-releaser '.updatedAt = (now - 3600 | todate)'
rs=$(st crew-reviewer .sessionId); ls_=$(st crew-releaser .sessionId)
"$F" crew watch --once >/dev/null 2>&1
check "one quiet station moves per round (a cron doesn't count)" '[ $(( $(moved crew-reviewer "$r" && echo 1 || echo 0) + $(moved crew-releaser "$l" && echo 1 || echo 0) )) = 1 ]'
"$F" crew watch --once >/dev/null 2>&1
check "then the next, after half an hour of waiting on a person" 'moved crew-reviewer "$r" && moved crew-releaser "$l"'
check "in the same conversation, told why" 'last crew-reviewer | jq -e --arg s "$rs" "index(\"--resume\") as \$i | \$i != null and .[\$i+1] == \$s" >/dev/null && last crew-releaser | grep -q "kit was updated"'
check "the working station still waits" '! moved crew-orchestrator "$o"'
"$F" crew halt "testing" >/dev/null 2>&1
agent crew-orchestrator '.status = "idle"'
"$F" crew watch --once >/dev/null 2>&1
check "nothing moves while halted" '! moved crew-orchestrator "$o"'
"$F" crew resume >/dev/null 2>&1
tmp=$(mktemp); jq '.["crew-orchestrator"].sessionId = ""' "$FACTORY_CREW_HOME/stations.json" > "$tmp" && mv "$tmp" "$FACTORY_CREW_HOME/stations.json"
"$F" crew watch --once >/dev/null 2>&1
check "a station with no conversation to resume isn't moved" '! moved crew-orchestrator "$o"'

# A kit that can't start a station takes down that one, not the crew.
for s in crew-reviewer crew-releaser; do older $s; agent $s '.state = "done"'; done
for _ in 1 2 3; do FAKE_CLAUDE_FAIL_RESUME=1 "$F" crew watch --once >/dev/null 2>&1; done
check "a failing kit takes down one station at most" '[ "$(jq "[.[] | select((.name == \"crew-reviewer\" or .name == \"crew-releaser\") and .state != \"stopped\")] | length" "$FAKE_CLAUDE_STATE/agents.json")" -ge 1 ]'
"$F" crew watch --once >/dev/null 2>&1
check "it comes back once the kit works" '"$F" roster 2>/dev/null | grep -q "^crew-reviewer" && "$F" roster 2>/dev/null | grep -q "^crew-releaser"'

"$F" crew down >/dev/null 2>&1
check "down retires all" '[ "$(jq "[.[] | select(.status == \"active\")] | length" "$FACTORY_CREW_HOME/stations.json")" = 0 ]'

# A crew.json written by hand gets the template's defaults under it.
H="$T/hand"; mkdir -p "$H"; echo '{"stations": {"reviewer": {"model": "sonnet"}}}' > "$H/crew.json"
FACTORY_CREW_HOME="$H" "$F" crew up --yes >/dev/null 2>&1
check "hand-made crew.json keeps its choice" '[ "$(jq -r .stations.reviewer.model "$H/crew.json")/$(jq -r .stations.reviewer.effort "$H/crew.json")" = sonnet/high ]'
check "hand-made crew.json gains defaults" '[ "$(jq -r .stations.orchestrator.model "$H/crew.json")" = opus ] && [ "$(jq -r .limits.autocompact_tokens "$H/crew.json")" = 200000 ]'
check "handovers/done exists" '[ -d "$H/handovers/done" ]'

# A running watch whose kit is updated starts again on the new version, once the update has settled.
K="$T/kit"; cp -R "$here/../plugin" "$K"
FACTORY_CREW_HOME="$H" FACTORY_WATCH_INTERVAL=1 "$K/bin/factory" crew watch >"$T/kw.out" 2>&1 &
kw=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do grep -q "watching the crew" "$T/kw.out" && break; sleep 0.3; done
sleep 1
echo "# updated" >> "$K/skills/factory-protocol/SKILL.md"
for _ in $(seq 40); do [ "$(grep -c "watching the crew" "$T/kw.out")" -ge 2 ] && break; sleep 0.5; done
kill "$kw" 2>/dev/null; wait "$kw" 2>/dev/null
check "the watch restarts itself on an updated kit" 'grep -q "the kit was updated" "$T/kw.out" && [ "$(grep -c "watching the crew" "$T/kw.out")" -ge 2 ]'

# An L5 crew: a project handed over stricter keeps production for the person, for every builder of it.
L="$T/l5"; mkdir -p "$L" "$T/core"; git -C "$T/core" init -q
printf 'L5-dark\n\n\nno\n4\n\n' | FACTORY_CREW_HOME="$L" "$F" crew up >/dev/null 2>&1
FACTORY_CREW_HOME="$L" "$F" spawn builder --autonomy L4-ship shop m1 "$T/core" "T1" >"$T/l5.out" 2>&1
check "L5: releaser started earlier is flagged for a restart" 'grep -q "factory restart crew-releaser" "$T/l5.out"'
FACTORY_CREW_HOME="$L" "$F" settings crew-releaser >"$T/rel.json" 2>/dev/null
check "L5: production soft-denied for the stricter project" 'jq -e ".autoMode.soft_deny | any(contains(\"for project shop: \"))" "$T/rel.json" >/dev/null'
check "L5: production still pre-authorized for the rest" 'jq -e ".autoMode.allow | any(contains(\"pre-authorized at level L5-dark\"))" "$T/rel.json" >/dev/null'
FACTORY_CREW_HOME="$L" "$F" retire shop-build-m1 >/dev/null 2>&1
FACTORY_CREW_HOME="$L" "$F" spawn builder shop m2 "$T/core" "T2" >/dev/null 2>&1
check "the project's level sticks to its next builder" 'jq -e ".autoMode.allow | any(test(\"pre-authorized at level L4-ship\"))" "$L/stations/shop-build-m2.settings.json" >/dev/null'
jq '.settings.autonomy = "L3-review"' "$L/crew.json" > "$T/c.json" && mv "$T/c.json" "$L/crew.json"
FACTORY_CREW_HOME="$L" "$F" settings shop-build-m2 >"$T/b.json" 2>/dev/null
check "a crew lowered later takes the project down with it" 'jq -e ".autoMode.allow | any(test(\"relayed .*yes\"))" "$T/b.json" >/dev/null'

# One project, several repos: each repo gets its own builder, scoped to that repo; the crew stations,
# doctor and adoption see every repo; a repo where a tag is a real release is not tagged.
M="$T/multi"; mkdir -p "$M"
for r in core charts; do mkdir -p "$T/argus/$r"; git -C "$T/argus/$r" init -q; done
core=$(git -C "$T/argus/core" rev-parse --show-toplevel); charts=$(git -C "$T/argus/charts" rev-parse --show-toplevel)
mkdir -p "$charts/.factory"; echo '{"release": {"tags": false}}' > "$charts/.factory/factory.json"
FACTORY_CREW_HOME="$M" "$F" crew up --yes >/dev/null 2>&1
FACTORY_CREW_HOME="$M" "$F" spawn builder argus m1-core "$core" "T1" >/dev/null 2>&1
FACTORY_CREW_HOME="$M" "$F" spawn builder argus m1-charts "$charts" "T2 after T1" >/dev/null 2>&1
check "multi: project keeps both repos in order" '[ "$(jq -c ".argus | [.repo, .repos]" "$M/projects.json")" = "$(jq -cn --arg a "$core" --arg b "$charts" "[\$a, [\$a, \$b]]")" ]'
check "multi: each builder runs in its repo" '[ "$(jq -r .env.FACTORY_REPO "$M/stations/argus-build-m1-charts.settings.json")" = "$charts" ]'
check "multi: a builder may push and merge only in its own repo" 'jq -e --arg c "$core" --arg h "$charts" "[.autoMode.allow[] | select(test(\"^(Pushing|Merging)\"))] | length == 2 and all(contains(\$h) and (contains(\$c) | not))" "$M/stations/argus-build-m1-charts.settings.json" >/dev/null'
tmp=$(mktemp); jq --arg l "$repo" '.other = {repo: $l, repos: [$l], slug: "other"}' "$M/projects.json" > "$tmp" && mv "$tmp" "$M/projects.json"
FACTORY_CREW_HOME="$M" "$F" settings argus-build-m1-charts >"$T/b2.json" 2>/dev/null
check "multi: a builder's state files stay within its project" 'jq -e --arg l "$repo" --arg c "$core" ".autoMode.allow[1] | contains(\"repositories of project argus: \") and contains(\$c) and (contains(\$l) | not)" "$T/b2.json" >/dev/null'
FACTORY_CREW_HOME="$M" "$F" settings crew-reviewer >"$T/rev.json" 2>/dev/null
check "multi: the reviewer's grant names every repo" 'jq -e --arg c "$core" --arg h "$charts" ".autoMode.allow | any(contains(\$c) and contains(\$h) and contains(\"factory/review\"))" "$T/rev.json" >/dev/null'
FACTORY_CREW_HOME="$M" "$F" settings crew-releaser >"$T/rel2.json" 2>/dev/null
check "multi: no tags where release.tags is false" 'jq -e --arg h "$charts" ".autoMode.soft_deny | any(contains(\"tags or GitHub releases in \" + \$h))" "$T/rel2.json" >/dev/null'
check "multi: tags still allowed elsewhere" 'jq -e --arg h "$charts" ".autoMode.allow | any(contains(\"(except in \" + \$h))" "$T/rel2.json" >/dev/null'
tmp=$(mktemp); jq --arg c "$charts" '. + [{id: "0000beef", name: "argus-build-m2-charts", cwd: $c, state: "working", sessionId: "s-beef", startedAt: 99, pid: 4343}]' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"
FACTORY_CREW_HOME="$M" "$F" roster >/dev/null 2>&1
check "multi: an unrecorded builder in the second repo is adopted with its project" '[ "$(jq -r ".[\"argus-build-m2-charts\"] | [.project, .repo] | join(\" \")" "$M/stations.json")" = "argus $charts" ]'
mkdir -p "$T/bin"; printf '%s\n' '#!/usr/bin/env bash' 'case "$1 $2" in' '  "auth status") exit 0 ;;' '  "repo view") case "$PWD" in *charts) echo "o/charts READ" ;; *) echo "o/core WRITE" ;; esac ;;' 'esac' > "$T/bin/gh"; chmod +x "$T/bin/gh"
jq -n --arg c "$core" '{projects: {($c): {hasTrustDialogAccepted: true}}}' > "$HOME/.claude.json"
PATH="$T/bin:$PATH" FACTORY_CREW_HOME="$M" "$F" doctor >"$T/doctor.out" 2>&1
check "multi doctor: trust per repo" 'grep -q "ok .*project argus: $core trusted" "$T/doctor.out" && grep -q "FIX .*project argus: Claude Code has not trusted $charts" "$T/doctor.out"'
check "multi doctor: GitHub write access per repo" 'grep -q "ok .*$core is o/core on GitHub (WRITE)" "$T/doctor.out" && grep -q "FIX .*gh has READ on o/charts" "$T/doctor.out"'
check "multi doctor: tag setting shown" 'grep -q "$charts has release.tags=false" "$T/doctor.out"'

# A station of a per-project factory from the older kit gets a plain refusal, even with a crew up,
# and `factory up` in its repo doesn't start a second team.
old="$T/oldproj"; mkdir -p "$old/.factory"; git -C "$old" init -q
echo '{"project": "oldproj", "level": "L5-dark"}' > "$old/.factory/factory.json"; echo '{}' > "$old/.factory/stations.json"
FACTORY_HOME="$old/.factory" FACTORY_ROLE=orchestrator FACTORY_CREW_HOME="$M" "$F" roster >"$T/old.out" 2>&1
check "older per-project station: plain refusal" 'grep -q "per-project factory started by an older" "$T/old.out" && ! grep -q "^crew-" "$T/old.out"'
(cd "$old" && FACTORY_CREW_HOME="$T/never" "$F" up --yes >"$T/oldup.out" 2>&1)
check "factory up in an older factory's repo starts nothing" 'grep -q "older tares-factory kit" "$T/oldup.out" && [ ! -d "$T/never" ]'

# Rius: the service name fits the 64 characters Rius takes. With Rius on in crew.json, doctor checks the
# plugin is enabled and that its own path rules trace the crew home and every project repo.
long=$(printf 'p%.0s' $(seq 70))
"$F" settings "$long" builder >"$T/long.json" 2>/dev/null
check "the Rius service name is cut to 64 characters" '[ "$(jq -r ".env.RIUS_SERVICE_NAME | length" "$T/long.json")" = 64 ]'
mkdir -p "$HOME/.claude/rius"; echo '{"enabledPlugins": {"rius@rius-coding-agents": false, "slack@x": true}}' > "$HOME/.claude/settings.json"
check "Rius on but the plugin off: doctor notes it, without failing on it" 'grep -q "warn  the rius plugin is not installed or not enabled, so stations run untraced" <<<"$("$F" doctor 2>/dev/null)"'
check "and crew up says nothing about Rius" '! grep -q "Rius doesn.t trace" <<<"$("$F" crew up --yes 2>&1)"'
echo '{"enabledPlugins": {"rius@rius-coding-agents": true}}' > "$HOME/.claude/settings.json"
crew_real=$(cd "$FACTORY_CREW_HOME" && pwd -P)
jq -n --arg c "$crew_real" '{enabled_paths: [$c]}' > "$HOME/.claude/rius/config.json"
out=$("$F" doctor 2>/dev/null)
check "crew up names the folders Rius doesn't trace yet" 'grep -q "Rius doesn.t trace.*$repo.*yet, so those stations send nothing: in a Claude session in each, run /rius:enable-content-here" <<<"$("$F" crew up --yes 2>&1)"'
check "a project repo the rules don't cover is named, with the command" 'grep "FIX   Rius doesn.t trace" <<<"$out" | grep -q "$repo" && ! grep "FIX   Rius doesn.t trace" <<<"$out" | grep -q " $FACTORY_CREW_HOME" && grep -q "/rius:enable-here" <<<"$out"'
jq -n --arg c "$crew_real" '{enabled_paths: ["/"], disabled_paths: [$c]}' > "$HOME/.claude/rius/config.json"
check "a disable rule wins over an enable" 'grep "FIX   Rius doesn.t trace" <<<"$("$F" doctor 2>/dev/null)" | grep -q " $FACTORY_CREW_HOME"'
jq -n '{enabled_paths: ["/"]}' > "$HOME/.claude/rius/config.json"
check "a rule the plugin ignores (the root) covers nothing" 'grep "FIX   Rius doesn.t trace" <<<"$("$F" doctor 2>/dev/null)" | grep -q " $FACTORY_CREW_HOME"'
jq -n --arg t "$(cd "$T" && pwd -P)" '{enabled_paths: ($t + "/*")}' | jq '.enabled_paths |= [.]' > "$HOME/.claude/rius/config.json"
out=$("$F" doctor 2>/dev/null)
check "a glob rule covering everything: no folder named" '! grep -q "Rius doesn.t trace" <<<"$out"'
check "but not logged in: doctor says so" 'grep -q "FIX   the rius plugin is not logged in" <<<"$out"'
echo '{}' > "$HOME/.claude/rius/credentials.json"
check "all covered and logged in: doctor is content" 'grep -q "ok    Rius traces the crew home and every project repo" <<<"$("$F" doctor 2>/dev/null)" && ! grep -q "not logged in" <<<"$("$F" doctor 2>/dev/null)"'
rm -rf "$HOME/.claude/rius" "$HOME/.claude/settings.json"
jq '.rius.enabled = false' "$FACTORY_CREW_HOME/crew.json" > "$T/c.json" && mv "$T/c.json" "$FACTORY_CREW_HOME/crew.json"

rm -rf "$T"
[ $fail = 0 ] && echo "crew: all cases pass"
exit $fail
