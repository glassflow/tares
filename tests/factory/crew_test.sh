#!/usr/bin/env bash
# Checks one crew for all projects, on a real Tares: crew up from a home folder (settings asked once and
# kept on Tares, the releaser only when asked for), builders in the repos of a project handed over on
# Tares, revive under the same name, halt and resume, the desk read from Tares, a permission allowed in
# Tares reaching its station through the watch, down. Uses a fake claude.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib.sh"
T=$(mktemp -d)
mkdir -p "$T/kit-under-test"; cp -R "$here/../../claude-plugin/." "$T/kit-under-test/"; F="$T/kit-under-test/bin/factory"
export HOME="$T/home" FACTORY_CREW_HOME="$T/crew" FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
unset TARES_URL TARES_AUTH_TOKEN TARES_FACTORY_URL TARES_FACTORY_TOKEN_FILE FACTORY_REPO FACTORY_WORKTREES CLAUDE_JOB_DIR TARES_FACTORY_MCP_URL TARES_FACTORY_CRED_FILE TARES_MCP_HOST TARES_MCP_PORT
mkdir -p "$HOME"
chmod +x "$CLAUDE_BIN"
trap 'stop_tares; rm -rf "$T"' EXIT
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; fi; }
st() { jq -r --arg s "$1" ".[\$s]$2 // empty" "$FACTORY_CREW_HOME/stations.json"; }
envof() { jq -r --arg k "$2" '.env[$k] // empty' "$FACTORY_CREW_HOME/stations/$1.settings.json"; }
allow_has() { jq -e --arg s "$2" 'any(.autoMode.allow[]; contains($s))' "$FACTORY_CREW_HOME/stations/$1.settings.json" >/dev/null; }
launches() { grep -c '"--bg"' "$FAKE_CLAUDE_STATE/calls.jsonl" 2>/dev/null || echo 0; }

# No Tares: every command says so plainly and starts nothing.
TARESD_URL=http://127.0.0.1:9 "$F" crew up --yes >"$T/notares.out" 2>&1
check "without Tares crew up stops plainly" 'grep -q "no Tares answers at http://127.0.0.1:9" "$T/notares.out" && grep -q "tares up" "$T/notares.out" && [ "$(launches)" = 0 ]'

start_tares "$T/tares"
# An untrusted crew home: without a terminal to ask in, crew up stops and says how to trust it.
"$F" crew up --yes </dev/null >"$T/untrusted.out" 2>&1
check "an untrusted crew home stops crew up plainly" 'grep -q "Claude Code has not trusted the crew home $FACTORY_CREW_HOME yet" "$T/untrusted.out" && grep -q "pick \"Yes, I trust this folder\"" "$T/untrusted.out" && [ "$(launches)" = 0 ]'
trust "$FACTORY_CREW_HOME"
# First run asks the settings one at a time; answers: L3-review, a releaser, no challenger, 3 builders,
# then the release profile and prod pattern (defaults), and one grant.
printf 'L3-review\nyes\n\nno\n3\nthe releaser may restart the staging database\n\n' | "$F" crew up >"$T/up.out" 2>&1
rc=$?
check "crew up exits 0" '[ $rc -eq 0 ]'
check "three crew stations started (a releaser was asked for)" '[ "$(launches)" = 3 ]'
for r in orchestrator reviewer releaser; do
  check "crew-$r recorded" '[ "$(st crew-$r .status)" = active ]'
  check "crew-$r runs in the crew home" '[ "$(st crew-$r .cwd)" = "$FACTORY_CREW_HOME" ]'
  check "crew-$r env FACTORY_STATION" '[ "$(envof crew-$r FACTORY_STATION)" = crew-$r ]'
  check "crew-$r env FACTORY_ROLE" '[ "$(envof crew-$r FACTORY_ROLE)" = $r ]'
  check "crew-$r env FACTORY_PARENT empty" 'jq -e ".env.FACTORY_PARENT == \"\"" "$FACTORY_CREW_HOME/stations/crew-$r.settings.json" >/dev/null'
  check "crew-$r has no TARES_PROJECT" '[ -z "$(envof crew-$r TARES_PROJECT)" ]'
  check "crew-$r reaches the same Tares" '[ "$(envof crew-$r TARES_FACTORY_URL)" = "$TARESD_URL" ]'
  check "crew-$r is told the shared MCP endpoint" '[ "$(envof crew-$r TARES_FACTORY_MCP_URL)" = "${TARES_FACTORY_MCP_URL:-http://127.0.0.1:8788/mcp}" ]'
done
check "the settings command shows the MCP endpoint too" '[ "$("$F" settings crew-builder-x builder | jq -r .env.TARES_FACTORY_MCP_URL)" = "${TARES_FACTORY_MCP_URL:-http://127.0.0.1:8788/mcp}" ]'
check "a local Tares: MCP on its own port" '[ "$(env -u TARES_FACTORY_MCP_URL TARESD_URL=http://localhost:8787 "$F" _mcp-url)" = http://127.0.0.1:8788/mcp ]'
check "a local Tares: TARES_MCP_PORT moves it" '[ "$(env -u TARES_FACTORY_MCP_URL TARESD_URL=http://127.0.0.1:8787 TARES_MCP_PORT=9911 "$F" _mcp-url)" = http://127.0.0.1:9911/mcp ]'
check "a remote Tares: MCP on its own host" '[ "$(env -u TARES_FACTORY_MCP_URL TARESD_URL=https://cell.example.com/ "$F" _mcp-url)" = https://cell.example.com/mcp ]'
check "TARES_FACTORY_MCP_URL wins" '[ "$(TARES_FACTORY_MCP_URL=http://mcp.example:1/mcp/ TARESD_URL=https://cell.example.com "$F" _mcp-url)" = http://mcp.example:1/mcp ]'
check "no Rius, no comms" '! grep -q -i -e rius -e comms "$FACTORY_CREW_HOME/stations/crew-reviewer.settings.json"'
check "orchestrator started first" '[ "$(jq -r "select(.args[0] == \"--bg\") | .args[2]" "$FAKE_CLAUDE_STATE/calls.jsonl" | head -1)" = crew-orchestrator ]'
check "stations turn the plugin on, whatever the person's settings say" 'jq -e ".enabledPlugins[\"tares@inline\"] == true" "$FACTORY_CREW_HOME/stations/crew-reviewer.settings.json" >/dev/null'
check "stations load this plugin" '[ "$(jq -r "select(.args[0] == \"--bg\") | .args | index(\"--plugin-dir\") as \$i | .[\$i+1]" "$FAKE_CLAUDE_STATE/calls.jsonl" | sort -u)" = "$(cd -P "$T/kit-under-test" && pwd)" ]'
check "settings saved on Tares" '[ "$(api GET /api/crew/settings | jq -c "[.autonomy, .releaser, .release_profile, .challenger, .builders_max]")" = "[\"L3-review\",true,\"kind\",false,3]" ]'
check "settings not kept locally" '[ "$(jq -c .settings "$FACTORY_CREW_HOME/crew.json")" = null ]'
check "no local desk or grant files" '[ ! -f "$FACTORY_CREW_HOME/DESK.md" ] && [ ! -f "$FACTORY_CREW_HOME/GRANTS.md" ]'
today=$(date -u +%Y-%m-%d)
check "grant saved on Tares as the person said it" 'api GET /api/grants | jq -r .all | grep -qx -- "- \"the releaser may restart the staging database\" ($today)"'
check "grant quoted in the allow rules" 'allow_has crew-releaser "granted this on $today for all projects, in their own words: \"the releaser may restart the staging database\". Acting"'
check "orchestrator may spawn" 'jq -e ".permissions.allow | index(\"Bash(factory spawn *)\")" "$FACTORY_CREW_HOME/stations/crew-orchestrator.settings.json" >/dev/null'
check "stations launch with a 200k autocompact window" 'jq -e "select(.args[2] == \"crew-orchestrator\") | .args | index(\"--autocompact\") as \$i | .[\$i+1] == \"200000\"" "$FAKE_CLAUDE_STATE/calls.jsonl" >/dev/null'
check "nothing goes to production without the person" 'jq -e ".autoMode.soft_deny | any(contains(\"needs\") and contains(\"prod\"))" "$FACTORY_CREW_HOME/stations/crew-releaser.settings.json" >/dev/null && ! grep -q L5 "$FACTORY_CREW_HOME/stations/crew-releaser.settings.json"'
check "crew up points at the Crew page" 'grep -q "$TARESD_URL/crew" "$T/up.out"'

# The desk lives on Tares: status shows the next question with its options, crew desk shows every one.
check "empty desk: crew desk says nothing waits" 'grep -q "Nothing waits for you" <<<"$("$F" crew desk 2>&1)"'
check "empty desk: status says nothing waits" 'grep -q "^desk: nothing waits" <<<"$("$F" crew status 2>&1)"'
P1=$(hand_over "Links App" "$T/none")
api POST /api/desk "$(jq -cn --arg p "$P1" '{question: "Keep the old endpoint?", recommendation: "keep it until v2 ships", why: "two callers still use it", options: ["keep it until v2 ships", "drop it now"], blocks: "T7", blocking: true, project: $p}')" >/dev/null
api POST /api/desk "$(jq -cn '{question: "Rename the bucket?", recommendation: "leave it", why: "nothing reads the name", options: ["rename now", "leave it"]}')" >/dev/null
"$F" crew status >"$T/status-desk.out" 2>&1
"$F" crew desk >"$T/desk.out" 2>&1
check "status counts the questions" 'grep -q "^desk: 2 waiting for you" "$T/status-desk.out"'
check "status shows the blocking one first, with its project" 'grep -q "D1 (blocks work) · Links App: Keep the old endpoint?" "$T/status-desk.out"'
check "status shows its options and recommendation" 'grep -q "A) keep it until v2 ships" "$T/status-desk.out" && grep -q "B) drop it now" "$T/status-desk.out" && grep -q "recommended: keep it until v2 ships, since two callers still use it" "$T/status-desk.out"'
check "status leaves out the later question" '! grep -q "Rename the bucket" "$T/status-desk.out"'
check "crew desk shows every question" 'grep -q "Keep the old endpoint?" "$T/desk.out" && grep -q "D2: Rename the bucket?" "$T/desk.out" && grep -q "blocks: T7" "$T/desk.out"'
check "crew desk says where to answer" 'grep -q "tares factory crew attach orchestrator" "$T/desk.out"'
check "factory desk is short for crew desk" '[ "$("$F" desk 2>&1)" = "$(cat "$T/desk.out")" ]'

# An installed tares plugin of another version may take this one's place: crew up and doctor warn and
# say how to remove it, and crew up still starts the crew.
mkdir -p "$HOME/.claude/plugins"
jq -n '{version: 2, plugins: {"tares@tares": [{scope: "project", projectPath: "/w/main", version: "0.2.1"}]}}' > "$HOME/.claude/plugins/installed_plugins.json"
"$F" crew up --yes >"$T/clash.out" 2>&1
check "an installed older tares plugin is named with how to remove it" 'grep -q "warning: another version of the tares plugin is installed" "$T/clash.out" && grep -q "claude plugin uninstall tares@tares --scope project (run it in /w/main)" "$T/clash.out"'
check "doctor says the same" 'grep -q "warn  another version of the tares plugin is installed from a marketplace (tares@tares 0.2.1 project (/w/main))" <<<"$("$F" doctor 2>&1)"'
mine=$(jq -r .version "$T/kit-under-test/.claude-plugin/plugin.json")
jq --arg v "$mine" '.plugins["tares@tares"][0].version = $v' "$HOME/.claude/plugins/installed_plugins.json" > "$T/ip" && mv "$T/ip" "$HOME/.claude/plugins/installed_plugins.json"
check "the same version installed is fine" '! grep -q "another version of the tares plugin" <<<"$("$F" doctor 2>&1)"'
rm -f "$HOME/.claude/plugins/installed_plugins.json"

# cmux says "Waiting for you" about a minute after any station's turn ends (its agentIdleReminder, on by
# default). Claude Code has no setting for dropping only that notice, so doctor reads the person's cmux
# file and says what to set; it never edits it, and the stations' settings keep cmux's permission alerts.
cmuxf="$HOME/.config/cmux/cmux.json"
"$F" doctor >"$T/cmux0.out" 2>&1
check "no cmux, nothing said about it" '! grep -qi cmux "$T/cmux0.out"'
mkdir -p "$HOME/.config/cmux"
printf '// a comment cmux writes\n{\n  "schemaVersion": 1\n}\n' > "$cmuxf"
"$F" doctor >"$T/cmux1.out" 2>&1
check "cmux with its default idle reminder: doctor says what to set" 'grep -q "warn  cmux shows \"Waiting for you\" about a minute after every station.s turn ends" "$T/cmux1.out" && grep -q "agentIdleReminder\": false} in $cmuxf" "$T/cmux1.out"'
printf '{"notifications": {"agentIdleReminder": false}}\n' > "$cmuxf"
"$F" doctor >"$T/cmux2.out" 2>&1
check "cmux idle reminder off: doctor is content" 'grep -q "ok    cmux.s idle reminder is off" "$T/cmux2.out" && ! grep -q "Waiting for you" "$T/cmux2.out"'
printf '{"notifications": {"agentIdleReminder": true}}\n' > "$cmuxf"
"$F" doctor >"$T/cmux3.out" 2>&1
check "cmux idle reminder on: doctor warns" 'grep -q "warn  cmux shows" "$T/cmux3.out"'
printf '{not json' > "$cmuxf"
"$F" doctor >"$T/cmux4.out" 2>&1
check "an unreadable cmux file is said, not guessed" 'grep -q "warn  could not read $cmuxf" "$T/cmux4.out"'
check "doctor never edits the cmux file" '[ "$(cat "$cmuxf")" = "{not json" ]'
check "stations leave cmux hooks on, so its permission alerts stay" '[ -z "$("$F" settings crew-builder-x builder | jq -r ".env | keys[] | select(test(\"CMUX\"))")" ]'
rm -rf "$HOME/.config/cmux"

# A second run asks nothing and starts nothing new.
"$F" crew up </dev/null >"$T/up2.out" 2>&1
check "second crew up asks nothing" '! grep -q "Choice \[" "$T/up2.out"'
check "second crew up starts nothing" '[ "$(launches)" = 3 ]'

# Builders start inside a repo of a project handed over on Tares, with the project in their env.
repo="$T/links"; mkdir -p "$repo"; git -C "$repo" init -q
repo=$(git -C "$repo" rev-parse --show-toplevel)
api POST "/api/projects/$P1/handover" "$(jq -cn --arg r "$repo" '{repos: [{path: $r}]}')" >/dev/null
"$F" spawn builder "Links App" m1 "$repo" "work T1 and T2" >/dev/null 2>&1
b=links-app-build-m1
check "builder recorded" '[ "$(st $b .role)" = builder ]'
check "builder runs in its repo" '[ "$(jq -r "select(.args[2] == \"$b\") | .cwd" "$FAKE_CLAUDE_STATE/calls.jsonl")" = "$repo" ]'
check "builder TARES_PROJECT" '[ "$(envof $b TARES_PROJECT)" = "Links App" ]'
check "builder FACTORY_PARENT" '[ "$(envof $b FACTORY_PARENT)" = crew-orchestrator ]'
check "builder FACTORY_REPO" '[ "$(envof $b FACTORY_REPO)" = "$repo" ]'
check "builder worktrees in its repo" '[ "$(envof $b FACTORY_WORKTREES)" = "$repo/.factory/worktrees" ]'
check "L3: builder waits for the relayed yes" 'jq -e ".autoMode.allow | any(test(\"relayed .*yes\"))" "$FACTORY_CREW_HOME/stations/$b.settings.json" >/dev/null'
check "L3: the no-CI clause holds at L3 too" 'jq -e ".autoMode.allow | any(test(\"relayed .*yes\") and contains(\"no CI checks configured\"))" "$FACTORY_CREW_HOME/stations/$b.settings.json" >/dev/null'
check "L3: no pre-authorized merge" '! jq -e ".autoMode.allow | any(test(\"pre-authorized at level\"))" "$FACTORY_CREW_HOME/stations/$b.settings.json" >/dev/null'
"$F" spawn builder "Links App" m1 "$repo" "again" >/dev/null 2>&1
check "no duplicate builder" '[ "$(launches)" = 4 ]'
"$F" spawn builder "Nobody's" m1 "$repo" "x" >"$T/unknown.out" 2>&1
check "a project not handed over is refused" 'grep -q "is not handed to the crew on Tares" "$T/unknown.out" && [ "$(launches)" = 4 ]'
other="$T/other"; mkdir -p "$other"; git -C "$other" init -q
"$F" spawn builder "Links App" m2 "$other" "x" >"$T/norepo.out" 2>&1
check "a repo the project was not handed over with is refused" 'grep -q "is not one of Links App.s repos on Tares" "$T/norepo.out" && [ "$(launches)" = 4 ]'
repo2="$T/shop"; mkdir -p "$repo2"; git -C "$repo2" init -q; repo2=$(git -C "$repo2" rev-parse --show-toplevel)
hand_over shop "$repo2" >/dev/null
"$F" spawn builder shop m1 "$repo2" "work T1" >/dev/null 2>&1
"$F" spawn builder shop m2 "$repo2" "work T2" >/dev/null 2>&1
repo3="$T/blog"; mkdir -p "$repo3"; git -C "$repo3" init -q; repo3=$(git -C "$repo3" rev-parse --show-toplevel)
hand_over blog "$repo3" >/dev/null
"$F" spawn builder blog m1 "$repo3" "work T1" >"$T/max.out" 2>&1
check "builders_max is honoured" 'grep -q "builders_max is 3" "$T/max.out"'
hand_over "Crew Tools" "$repo3" >/dev/null
"$F" spawn builder "Crew Tools" m1 "$repo3" "x" >"$T/crewname.out" 2>&1
check "a project can't be named like the crew" 'grep -q "can.t start with" "$T/crewname.out" && [ -z "$(st crew-tools-build-m1 .role)" ]'
check "roster lists project" '"$F" roster | grep -q "^links-app-build-m1	builder	Links App	working"'
"$F" projects >"$T/projects.out" 2>&1
check "projects lists each handed-over project with its builders and repos" 'grep -E "^Links App +0/0 +- +1 +.*$repo" "$T/projects.out" >/dev/null && grep -E "^shop +0/0 +- +2 " "$T/projects.out" >/dev/null'
# A helper keeps the parent's project and works under its role.
"$F" spawn helper "$b" rebase "rebase T2" >/dev/null 2>&1
check "helper name" '[ "$(st $b-rebase .role)" = helper ]'
check "helper parent" '[ "$(envof $b-rebase FACTORY_PARENT)" = $b ]'
check "helper project" '[ "$(envof $b-rebase TARES_PROJECT)" = "Links App" ]'
# Retire tells Tares the station ended (TR-565): give it a session on Tares first, as the plugin would.
api POST /api/sources '{"name":"claude_code","connector":"claude_code","poll":"10s","config":{"push":true}}' >/dev/null
jq -cn --arg n "$b-rebase" --arg at "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" \
  '{type: "session_station", sessionId: "s-rebase", cwd: "/w/crew", timestamp: $at, station: $n, role: "helper", factory_station: $n}' |
  curl -sS -X POST -H 'Content-Type: application/x-ndjson' --data-binary @- "$TARESD_URL/ingest/claude_code" >/dev/null
"$F" retire "$b-rebase" >"$T/retire.out" 2>&1
check "retire marks the station ended on Tares" '[ "$(api GET "/api/crew/$b-rebase" | jq -r ".state + \"/\" + .state_reason")" = ended/retired ] && ! grep -q "could not tell Tares" "$T/retire.out"'
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
check "roster shows a finished station with no process as gone" '"$F" roster | grep -q "^crew-reviewer	reviewer		gone"'
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
check "listed as working with no process: roster says gone" '"$F" roster | grep -q "^crew-orchestrator	orchestrator		gone"'
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

# restart waits for the stop before resuming.
er=$(earlier crew-reviewer)
FACTORY_STOP_WAIT=2 "$F" restart reviewer >/dev/null 2>&1
check "restart resumes the reviewer" '[ "$(earlier crew-reviewer)" = $((er + 1)) ]'

# Stopping the crew never waits for Tares: halt, resume and status of the sessions work while it is down.
TARESD_URL=http://127.0.0.1:9 "$F" crew halt "tares is down" >"$T/halt-down.out" 2>&1
check "halt works while Tares is down" '[ -f "$FACTORY_CREW_HOME/HALT" ] && ! grep -q "no Tares answers" "$T/halt-down.out"'
TARESD_URL=http://127.0.0.1:9 "$F" crew resume >/dev/null 2>&1
check "and resume" '[ ! -f "$FACTORY_CREW_HOME/HALT" ]'
TARESD_URL=http://127.0.0.1:9 "$F" roster >"$T/roster-down.out" 2>&1
check "roster works while Tares is down" 'grep -q "^crew-orchestrator	orchestrator" "$T/roster-down.out"'

# halt blocks risky actions in every station until resume.
"$F" crew halt "testing" >/dev/null 2>&1
check "halt file" '[ -f "$FACTORY_CREW_HOME/HALT" ]'
"$F" spawn builder blog m1 "$repo3" "x" >"$T/halt.out" 2>&1
check "spawn refused while halted" 'grep -q "halted" "$T/halt.out"'
out=$(jq -n '{tool_name: "Bash", tool_input: {command: "gh pr merge 3 --squash"}}' | FACTORY_ROLE=builder FACTORY_HOME="$FACTORY_CREW_HOME" bash "$here/../../claude-plugin/hooks/guard.sh")
check "guard denies merge while halted" 'grep -q "halted" <<<"$out"'
"$F" crew resume >/dev/null 2>&1
out=$(jq -n '{tool_name: "Bash", tool_input: {command: "gh pr merge 3 --squash"}}' | FACTORY_ROLE=builder FACTORY_HOME="$FACTORY_CREW_HOME" bash "$here/../../claude-plugin/hooks/guard.sh")
check "guard allows merge after resume" '[ -z "$out" ]'

ctx=$(echo '{"source": "resume"}' | env $(jq -r '.env | to_entries[] | "\(.key)=\(.value)"' "$FACTORY_CREW_HOME/stations/$b.settings.json" | tr ' ' '_') bash "$here/../../claude-plugin/hooks/session-start.sh")
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
check "status counts stations on an older kit" 'grep -q "^plugin: 3 station(s) run an older version" <<<"$("$F" crew status 2>/dev/null)"'
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
check "in the same conversation, told why" 'last crew-reviewer | jq -e --arg s "$rs" "index(\"--resume\") as \$i | \$i != null and .[\$i+1] == \$s" >/dev/null && last crew-releaser | grep -q "Tares plugin was updated"'
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

# A permission the person allows in the console is a grant on Tares. The watch restarts each live station
# whose grants changed, once it is between turns, in the same conversation: its fresh settings have the
# grant and its prompt quotes it. Once per change; a busy station waits, and a Tares whose grants can't
# be read changes nothing.
idle() { agent "$1" '.state = "working" | .status = "idle"'; job "$1" 'del(.inFlight)'; }
for s in $(jq -r 'to_entries[] | select(.value.status == "active") | .key' "$FACTORY_CREW_HOME/stations.json"); do agent "$s" '.state = "working" | .status = "busy"'; done
check "stations record the grants they started with" '[ -n "$(st shop-build-m1 .grants)" ] && grep -q "staging database" "$FACTORY_CREW_HOME/stations/shop-build-m1.grants"'
SHOP=$(api GET /api/crew/projects | jq -r '.projects[] | select(.name == "shop") | .id')
grant="shop-build-m1 may run gh pr merge 12 --squash in shop"
dk=$(api POST /api/desk "$(jq -cn --arg p "$SHOP" --arg g "$grant" '{question: "shop-build-m1 was refused a command. Allow it?", recommendation: $g, why: "it merges its own pull request", project: $p, station: "shop-build-m1", command: "gh pr merge 12 --squash"}')" | jq -r .label)
"$F" crew desk >"$T/desk-perm.out" 2>&1
check "crew desk shows the refused command and where to allow it" 'grep -q "shop-build-m1 was refused: gh pr merge 12 --squash" "$T/desk-perm.out" && grep -q "allow it in Tares" "$T/desk-perm.out"'
api POST "/api/desk/$dk/allow" "$(jq -cn --arg g "$grant" '{words: $g, scope: "project"}')" >/dev/null
m1=$(st shop-build-m1 .id); m2=$(st shop-build-m2 .id); m1s=$(st shop-build-m1 .sessionId); r=$(st crew-reviewer .id); before=$(launches)
"$F" crew watch --once >/dev/null 2>&1
check "a busy station waits for its new grant" '[ "$(st shop-build-m1 .id)" = "$m1" ] && [ "$(launches)" = "$before" ]'
check "status says it has new permissions waiting" '"$F" crew status 2>/dev/null | grep "^shop-build-m1 " | grep -q "NEW PERMISSIONS: the watch restarts it"'
idle shop-build-m1; idle crew-reviewer
"$F" crew watch --once >"$T/regrant.out" 2>&1
check "between turns it is restarted with the grant" 'moved shop-build-m1 "$m1" && grep -q "shop-build-m1 has new permissions from Tares" "$T/regrant.out" && grep -q "\"event\":\"regranted\"" "$FACTORY_CREW_HOME/events.jsonl"'
check "in the same conversation" 'last shop-build-m1 | jq -e --arg s "$m1s" "index(\"--resume\") as \$i | \$i != null and .[\$i+1] == \$s" >/dev/null'
check "its fresh settings have the grant" 'allow_has shop-build-m1 "for project shop, in their own words: \"$grant\". Acting"'
check "its prompt quotes the new grant and says to run the refused command again" 'last shop-build-m1 | jq -r ".[-1]" | grep -q "allowed new permissions in Tares" && last shop-build-m1 | jq -r ".[-1]" | grep -qF "\"$grant\"" && last shop-build-m1 | jq -r ".[-1]" | grep -q "run it again now"'
check "and only the new one" '! last shop-build-m1 | jq -r ".[-1]" | grep -q "staging database"'
check "a busy builder of the same project still waits" '[ "$(st shop-build-m2 .id)" = "$m2" ]'
check "a station whose grants did not change is left alone" '[ "$(st crew-reviewer .id)" = "$r" ] && [ $(( $(launches) - before )) = 1 ]'
idle shop-build-m1; m1=$(st shop-build-m1 .id)
"$F" crew watch --once >/dev/null 2>&1
check "once per change" '[ "$(st shop-build-m1 .id)" = "$m1" ]'
# A live helper gets a permission allowed for it too. A station someone has open in a terminal waits until
# they leave it: a restart would end the conversation they are in.
"$F" spawn helper shop-build-m1 e2e "run the e2e tests" >/dev/null 2>&1
idle shop-build-m1-e2e; idle shop-build-m2; hp=$(st shop-build-m1-e2e .id); m2=$(st shop-build-m2 .id)
api POST /api/grants "$(jq -cn --arg p "$SHOP" '{words: "shop-build-m1-e2e may run npm run e2e in shop", scope: "project", project: $p}')" >/dev/null
bash -c "exec -a 'claude attach $m2' sleep 60" & apid=$!
"$F" crew watch --once >/dev/null 2>&1
check "a live helper is restarted with its grant" 'moved shop-build-m1-e2e "$hp" && allow_has shop-build-m1-e2e "shop-build-m1-e2e may run npm run e2e"'
check "a station open in a terminal waits" '[ "$(st shop-build-m2 .id)" = "$m2" ]'
check "status says it waits for you to leave it" '"$F" crew status 2>/dev/null | grep "^shop-build-m2 " | grep -q "once you leave its session"'
kill "$apid" 2>/dev/null; wait "$apid" 2>/dev/null
"$F" crew watch --once >/dev/null 2>&1
check "and gets it once they leave" 'moved shop-build-m2 "$m2" && allow_has shop-build-m2 "shop-build-m1-e2e may run npm run e2e"'
"$F" retire shop-build-m1-e2e >/dev/null 2>&1
# A grant for every project; a Tares that answers its health check but can't give the grants.
api POST /api/grants '{"words": "the reviewer may label pull requests", "scope": "all"}' >/dev/null
fake="$T/fake-tares"; mkdir -p "$fake"; echo ok > "$fake/health"
fport=$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
"$PY" -m http.server "$fport" --bind 127.0.0.1 --directory "$fake" >/dev/null 2>&1 & fpid=$!
for _ in $(seq 50); do curl -sf "http://127.0.0.1:$fport/health" >/dev/null 2>&1 && break; sleep 0.1; done
before=$(launches)
TARESD_URL="http://127.0.0.1:$fport" "$F" crew watch --once >"$T/unread-grants.out" 2>&1
kill "$fpid" 2>/dev/null; wait "$fpid" 2>/dev/null
check "grants that can't be read restart nothing" '[ "$(launches)" = "$before" ] && ! grep -q "new permissions" "$T/unread-grants.out"'
"$F" crew halt "testing" >/dev/null 2>&1
"$F" crew watch --once >/dev/null 2>&1
check "nothing restarts while halted" '[ "$(launches)" = "$before" ]'
"$F" crew resume >/dev/null 2>&1
# Blocked on a person, for minutes only: restarted at once, since it may wait on this very permission.
agent crew-reviewer '.state = "working" | .status = "waiting"'; job crew-reviewer '.state = "working" | .updatedAt = (now | todate)'
"$F" crew watch --once >/dev/null 2>&1
check "a station blocked on a person gets the grant at once" 'moved crew-reviewer "$r" && allow_has crew-reviewer "for all projects, in their own words: \"the reviewer may label pull requests\""'
check "busy crew stations still wait" '[ "$(st crew-orchestrator .grants)" != "$(st crew-reviewer .grants)" ]'

"$F" crew down >"$T/down.out" 2>&1
check "stations that never reported to Tares retire without a warning" '! grep -q "could not tell Tares" "$T/down.out"'
check "down retires all" '[ "$(jq "[.[] | select(.status == \"active\")] | length" "$FACTORY_CREW_HOME/stations.json")" = 0 ]'

# A crew.json written by hand gets the template's defaults under it. Without a releaser (the default),
# crew up starts only the orchestrator and the reviewer.
api PUT /api/crew/settings '{"releaser": false}' >/dev/null
H="$T/hand"; mkdir -p "$H"; echo '{"stations": {"reviewer": {"model": "sonnet"}}}' > "$H/crew.json"
before=$(launches)
trust "$H"; FACTORY_CREW_HOME="$H" "$F" crew up --yes >/dev/null 2>&1
check "hand-made crew.json keeps its choice" '[ "$(jq -r .stations.reviewer.model "$H/crew.json")/$(jq -r .stations.reviewer.effort "$H/crew.json")" = sonnet/high ]'
check "hand-made crew.json gains defaults" '[ "$(jq -r .stations.orchestrator.model "$H/crew.json")" = opus ] && [ "$(jq -r .limits.autocompact_tokens "$H/crew.json")" = 200000 ]'
check "no releaser unless asked for" '[ $(( $(launches) - before )) = 2 ] && [ -z "$(jq -r ".[\"crew-releaser\"].status // empty" "$H/stations.json")" ]'

# The person marks a project done from their terminal; a station can't.
FP=$(hand_over finishing "$repo")
api POST "/api/projects/$FP/tickets" '{"title": "only one"}' >/dev/null
"$F" finish finishing "all shipped" >"$T/fin1.out" 2>&1
check "finish refuses a project with an open ticket" 'grep -q "still has open tickets: T1" "$T/fin1.out"'
api POST "/api/projects/$FP/tickets/T1/stage" '{"stage": "doing"}' >/dev/null
api POST "/api/projects/$FP/tickets/T1/stage" '{"stage": "merged"}' >/dev/null
FACTORY_STATION=crew-orchestrator "$F" finish finishing >"$T/fin2.out" 2>&1
check "a station can't run finish" 'grep -q "only the person runs this" "$T/fin2.out"'
"$F" finish finishing "all shipped" >"$T/fin3.out" 2>&1
check "the person finishes it" 'grep -q "✓ finishing is done" "$T/fin3.out" && [ "$(api GET /api/crew/projects | jq -r "[.finished[].name] | index(\"finishing\") != null")" = true ]'
check "and it is off the crew's list" '! "$F" projects 2>/dev/null | grep -q "^finishing "'

# A finished project's questions (TR-559): a running crew still sees them, crew down withdraws them,
# and a stopped crew's status leaves them out and shows no empty station table.
E="$T/ended"; trust "$E"; FACTORY_CREW_HOME="$E" "$F" crew up --yes >/dev/null 2>&1
api POST /api/desk "$(jq -cn --arg p "$FP" '{question: "No project is left. Stop the crew?", recommendation: "yes", why: "nothing to build", project: $p, blocking: true}')" >/dev/null
FACTORY_CREW_HOME="$E" "$F" crew desk >"$T/ended-desk.out" 2>&1
check "a running crew sees a finished project's question" 'grep -q "finishing: No project is left" "$T/ended-desk.out"'
check "a question with nothing in blocks prints no empty blocks line" '! grep -qx "  blocks: *" "$T/ended-desk.out"'
# TR-599: the same question asked with no project, and one crew-wide question that stays
api POST /api/desk "$(jq -cn '{question: "No project is left. Stop the crew? Run `tares factory crew down` in your terminal.", recommendation: "yes", why: "nothing to build"}')" >/dev/null
api POST /api/desk "$(jq -cn '{question: "Nothing left to build. Stop the crew?", recommendation: "yes", why: "shorter words"}')" >/dev/null
api POST /api/desk "$(jq -cn '{question: "Keep nightly dependency bumps?", recommendation: "yes", why: "cheap"}')" >/dev/null
FACTORY_CREW_HOME="$E" "$F" crew down >/dev/null 2>&1
check "crew down withdraws it" 'api GET "/api/desk?state=withdrawn" | jq -e "any(.desk[]; .question == \"No project is left. Stop the crew?\")" >/dev/null'
check "and the stop-the-crew question asked with no project" 'api GET "/api/desk?state=withdrawn" | jq -e "any(.desk[]; (.question | contains(\"crew down\")) and .project == null and .reason == \"the person stopped the crew\")" >/dev/null'
check "and one in other words that still asks to stop the crew" 'api GET "/api/desk?state=withdrawn" | jq -e "any(.desk[]; .question == \"Nothing left to build. Stop the crew?\")" >/dev/null'
check "a crew-wide question about something else stays open" 'api GET /api/desk | jq -e "any(.desk[]; .question == \"Keep nightly dependency bumps?\")" >/dev/null'
api POST /api/desk "$(jq -cn --arg p "$FP" '{question: "Stop the crew now?", recommendation: "yes", why: "late", project: $p, blocking: true}')" >/dev/null
FACTORY_CREW_HOME="$E" "$F" crew status >"$T/ended.out" 2>&1
open_n=$(api GET /api/desk | jq '.desk | length')
check "a stopped crew's status leaves it out" 'grep -q "^desk: $((open_n - 1)) waiting" "$T/ended.out" && ! grep -q "Stop the crew now" "$T/ended.out"'
check "and says no stations run instead of an empty table" 'grep -q "^no stations running; tares factory crew up starts them" "$T/ended.out" && ! grep -q "^STATION " "$T/ended.out"'

# crew up starts the watch in the background, once; status says so; crew down stops it first.
WH="$T/watched"; trust "$WH"
FACTORY_NO_WATCH= FACTORY_WATCH_INTERVAL=1 FACTORY_CREW_HOME="$WH" "$F" crew up --yes >"$T/w1.out" 2>&1
wpid=$(cat "$WH/watch.pid" 2>/dev/null)
check "crew up starts the watch in the background" 'grep -q "started the watch" "$T/w1.out" && [ -n "$wpid" ] && kill -0 "$wpid" 2>/dev/null'
for _ in $(seq 20); do grep -q "watching the crew" "$WH/watch.log" 2>/dev/null && break; sleep 0.3; done
check "it writes to its log" 'grep -q "watching the crew" "$WH/watch.log"'
FACTORY_NO_WATCH= FACTORY_CREW_HOME="$WH" "$F" crew up --yes >"$T/w2.out" 2>&1
check "a second crew up keeps the one watch" 'grep -q "already running, pid $wpid" "$T/w2.out" && [ "$(cat "$WH/watch.pid")" = "$wpid" ]'
FACTORY_CREW_HOME="$WH" "$F" crew status >"$T/ws.out" 2>&1; check "status says the watch runs" 'grep -q "^watch: running (pid $wpid)" "$T/ws.out"'
FACTORY_CREW_HOME="$WH" "$F" crew down >"$T/wd.out" 2>&1
for _ in $(seq 20); do kill -0 "$wpid" 2>/dev/null || break; sleep 0.2; done
check "crew down stops the watch" '! kill -0 "$wpid" 2>/dev/null && [ ! -f "$WH/watch.pid" ] && grep -q "stopped the watch" "$T/wd.out"'
FACTORY_NO_WATCH= FACTORY_CREW_HOME="$WH" "$F" crew up --yes --no-watch >"$T/w3.out" 2>&1
check "--no-watch starts none" '! grep -q "the watch" "$T/w3.out" && [ ! -f "$WH/watch.pid" ]'
FACTORY_CREW_HOME="$WH" "$F" crew down >/dev/null 2>&1

# A running watch whose plugin is updated starts again on the new version, once the update has settled.
K="$T/kit"; mkdir -p "$K"; cp -R "$here/../../claude-plugin/." "$K/"
FACTORY_CREW_HOME="$H" FACTORY_WATCH_INTERVAL=1 "$K/bin/factory" crew watch >"$T/kw.out" 2>&1 &
kw=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do grep -q "watching the crew" "$T/kw.out" && break; sleep 0.3; done
sleep 1
echo "# updated" >> "$K/skills/factory-protocol/SKILL.md"
for _ in $(seq 40); do [ "$(grep -c "watching the crew" "$T/kw.out")" -ge 2 ] && break; sleep 0.5; done
kill "$kw" 2>/dev/null; wait "$kw" 2>/dev/null
check "the watch restarts itself on an updated plugin" 'grep -q "the plugin was updated" "$T/kw.out" && [ "$(grep -c "watching the crew" "$T/kw.out")" -ge 2 ]'

# A project handed over stricter keeps it for every builder of it; a crew lowered later takes it down.
L4="$T/l4"; mkdir -p "$L4" "$T/core"; git -C "$T/core" init -q; core1=$(git -C "$T/core" rev-parse --show-toplevel)
api PUT /api/crew/settings '{"autonomy": "L4-ship"}' >/dev/null
SP=$(hand_over strict "$core1")
api POST "/api/projects/$SP/handover" '{"autonomy": "L3-review"}' >/dev/null
trust "$L4"; FACTORY_CREW_HOME="$L4" "$F" crew up --yes >/dev/null 2>&1
FACTORY_CREW_HOME="$L4" "$F" spawn builder strict m1 "$core1" "T1" >/dev/null 2>&1
check "a project handed over stricter keeps it" 'jq -e ".autoMode.allow | any(test(\"relayed .*yes\"))" "$L4/stations/strict-build-m1.settings.json" >/dev/null'
FACTORY_CREW_HOME="$L4" "$F" spawn builder --autonomy L5-dark strict m2 "$core1" "T2" >"$T/l5.out" 2>&1
check "L5 is no level any more" 'grep -q "autonomy must be L3-review or L4-ship" "$T/l5.out"'
FACTORY_CREW_HOME="$L4" "$F" retire strict-build-m1 >/dev/null 2>&1
LP=$(hand_over loose "$core1")
FACTORY_CREW_HOME="$L4" "$F" spawn builder loose m1 "$core1" "T1" >/dev/null 2>&1
check "L4: the builder merges on the gate" 'jq -e ".autoMode.allow | any(test(\"pre-authorized at level L4-ship\"))" "$L4/stations/loose-build-m1.settings.json" >/dev/null'
api PUT /api/crew/settings '{"autonomy": "L3-review"}' >/dev/null
FACTORY_CREW_HOME="$L4" "$F" settings loose-build-m1 >"$T/b.json" 2>/dev/null
check "a crew lowered later takes the project down with it" 'jq -e ".autoMode.allow | any(test(\"relayed .*yes\"))" "$T/b.json" >/dev/null'

# One project, several repos: each repo gets its own builder, scoped to that repo; the crew stations,
# doctor and adoption see every repo.
M="$T/multi"; mkdir -p "$M"
for r in core charts; do mkdir -p "$T/argus/$r"; git -C "$T/argus/$r" init -q; done
core=$(git -C "$T/argus/core" rev-parse --show-toplevel); charts=$(git -C "$T/argus/charts" rev-parse --show-toplevel)
hand_over argus "$core" "$charts" >/dev/null
trust "$M"; FACTORY_CREW_HOME="$M" "$F" crew up --yes >/dev/null 2>&1
FACTORY_CREW_HOME="$M" "$F" spawn builder argus m1-core "$core" "T1" >/dev/null 2>&1
FACTORY_CREW_HOME="$M" "$F" spawn builder argus m1-charts "$charts" "T2 after T1" >/dev/null 2>&1
check "multi: each builder runs in its repo" '[ "$(jq -r .env.FACTORY_REPO "$M/stations/argus-build-m1-charts.settings.json")" = "$charts" ]'
check "multi: a builder may push and merge only in its own repo" 'jq -e --arg c "$core" --arg h "$charts" "[.autoMode.allow[] | select(test(\"^(Pushing|Merging)\"))] | length == 2 and all(contains(\$h) and (contains(\$c) | not))" "$M/stations/argus-build-m1-charts.settings.json" >/dev/null'
check "multi: a builder's worktrees stay within its project" 'jq -e --arg l "$repo" --arg c "$core" ".autoMode.allow[1] | contains(\"repositories of project argus: \") and contains(\$c) and (contains(\$l) | not)" "$M/stations/argus-build-m1-charts.settings.json" >/dev/null'
FACTORY_CREW_HOME="$M" "$F" settings crew-reviewer >"$T/rev.json" 2>/dev/null
check "multi: the reviewer's grant names every repo" 'jq -e --arg c "$core" --arg h "$charts" ".autoMode.allow | any(contains(\$c) and contains(\$h) and contains(\"factory/review\"))" "$T/rev.json" >/dev/null'
tmp=$(mktemp); jq --arg c "$charts" '. + [{id: "0000beef", name: "argus-build-m2-charts", cwd: $c, state: "working", sessionId: "s-beef", startedAt: 99, pid: 4343}]' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"
FACTORY_CREW_HOME="$M" "$F" roster >/dev/null 2>&1
check "multi: an unrecorded builder in the second repo is adopted with its project" '[ "$(jq -r ".[\"argus-build-m2-charts\"] | [.project, .repo] | join(\" \")" "$M/stations.json")" = "argus $charts" ]'
mkdir -p "$T/bin"; printf '%s\n' '#!/usr/bin/env bash' 'case "$1 $2" in' '  "auth status") exit 0 ;;' '  "repo view") case "$PWD" in *charts) echo "o/charts READ" ;; *) echo "o/core WRITE" ;; esac ;;' 'esac' > "$T/bin/gh"; chmod +x "$T/bin/gh"
jq -n --arg c "$core" '{projects: {($c): {hasTrustDialogAccepted: true}}}' > "$HOME/.claude.json"
PATH="$T/bin:$PATH" FACTORY_CREW_HOME="$M" "$F" doctor >"$T/doctor.out" 2>&1
check "doctor: Tares answers" 'grep -q "ok    Tares answers at $TARESD_URL" "$T/doctor.out" && grep -q "ok    Tares lets this crew in" "$T/doctor.out"'
check "multi doctor: trust per repo" 'grep -q "ok .*project argus: $core trusted" "$T/doctor.out" && grep -q "FIX .*project argus: Claude Code has not trusted $charts" "$T/doctor.out"'
check "multi doctor: GitHub write access per repo" 'grep -q "ok .*$core is o/core on GitHub (WRITE)" "$T/doctor.out" && grep -q "FIX .*gh has READ on o/charts" "$T/doctor.out"'
check "doctor: no Rius" '! grep -qi rius "$T/doctor.out"'
check "doctor: the MCP endpoint answers" 'grep -qx "  ok    MCP endpoint answers at $TARES_FACTORY_MCP_URL" "$T/doctor.out"'
check "doctor: nothing about tares-mcp on PATH" '! grep -q "tares-mcp" "$T/doctor.out"'

# No MCP server at the endpoint: nothing starts, and the person is told what to run.
nomcp="http://127.0.0.1:$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')/mcp"
n0=$(launches)
TARES_FACTORY_MCP_URL=$nomcp FACTORY_CREW_HOME="$M" "$F" crew up --yes >"$T/nomcp.out" 2>&1
rc=$?
check "no MCP server: crew up stops" '[ $rc -ne 0 ] && [ "$(launches)" = "$n0" ]'
check "no MCP server: it names the URL and the command" 'grep -q "no MCP server answers at $nomcp" "$T/nomcp.out" && grep -q "tares mcp --transport streamable-http" "$T/nomcp.out"'
check "no MCP server: no token, so no token placeholder" '! grep -q "TARES_AUTH_TOKEN=" "$T/nomcp.out"'
TARES_FACTORY_MCP_URL=$nomcp FACTORY_CREW_HOME="$M" "$F" spawn builder argus m1-more "$core" "T3" >"$T/nomcp-spawn.out" 2>&1
check "no MCP server: spawn stops too" '[ "$(launches)" = "$n0" ] && grep -q "no MCP server answers at $nomcp" "$T/nomcp-spawn.out"'
TARES_FACTORY_MCP_URL=$TARESD_URL/mcp FACTORY_CREW_HOME="$M" "$F" crew up --yes >"$T/taresd-mcp.out" 2>&1
check "taresd itself is not an MCP server" 'grep -q "no MCP server answers at $TARESD_URL/mcp" "$T/taresd-mcp.out" && [ "$(launches)" = "$n0" ]'
TARES_FACTORY_MCP_URL=$nomcp FACTORY_CREW_HOME="$M" "$F" doctor >"$T/doctor3.out" 2>&1
check "doctor without an MCP server says how to start one" 'grep -q "FIX   no MCP server answers at $nomcp; start one with tares mcp --transport streamable-http" "$T/doctor3.out"'
TARESD_URL=http://127.0.0.1:9 FACTORY_CREW_HOME="$M" "$F" doctor >"$T/doctor2.out" 2>&1
check "doctor without Tares says how to start one" 'grep -q "FIX   no Tares answers at http://127.0.0.1:9 (from the environment): start one with \`tares up\`, connect to your Tares Cloud workspace with \`tares login\`" "$T/doctor2.out"'

[ $fail = 0 ] && echo "crew: all cases pass"
exit $fail
