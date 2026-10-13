#!/usr/bin/env bash
# Checks the crew against a Tares with auth on: the first crew up asks and saves on Tares, every call
# carries the token, stations get the token only as a file the person can read, a second machine asks
# nothing and writes the same settings, a builder gets its project's grants, and the orchestrator's own
# `factory spawn` (run with its settings env, not the person's shell) still reaches Tares.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib.sh"
F="$here/../../claude-plugin/bin/factory"
T=$(cd "$(mktemp -d)" && pwd -P)
export FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
unset TARES_URL TARES_FACTORY_URL TARES_FACTORY_TOKEN_FILE FACTORY_REPO FACTORY_WORKTREES CLAUDE_JOB_DIR TARES_FACTORY_MCP_URL TARES_FACTORY_CRED_FILE TARES_MCP_HOST TARES_MCP_PORT
chmod +x "$CLAUDE_BIN"
trap 'stop_tares; rm -rf "$T"' EXIT
start_tares "$T/tares" stub-token
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; fi; }
allow_has() { jq -e --arg s "$2" 'any(.autoMode.allow[]; contains($s))' "$FACTORY_CREW_HOME/stations/$1.settings.json" >/dev/null; }
envof() { jq -r --arg k "$2" '.env[$k] // empty' "$FACTORY_CREW_HOME/stations/$1.settings.json"; }

machine() { # name → sets HOME and FACTORY_CREW_HOME for one "machine", its crew home trusted
  export HOME="$T/$1/home" FACTORY_CREW_HOME="$T/$1/crew"; mkdir -p "$HOME"
  trust "$FACTORY_CREW_HOME"
}

machine one
TARES_AUTH_TOKEN=wrong "$F" crew up --yes >"$T/wrong.out" 2>&1
check "a wrong token stops crew up plainly" 'grep -q "could not read the crew settings from Tares" "$T/wrong.out" && [ ! -f "$FAKE_CLAUDE_STATE/calls.jsonl" ]'
printf 'L4-ship\nno\nyes\n2\nthe reviewer may comment on draft PRs\n\n' | "$F" crew up >"$T/up1.out" 2>&1
rc=$?
check "first crew up exits 0" '[ $rc -eq 0 ] && grep -q "started crew-reviewer" "$T/up1.out"'
check "without a releaser, two stations" '! grep -q "started crew-releaser" "$T/up1.out"'
check "the release questions are not asked without a releaser" '! grep -q "How does the releaser ship" "$T/up1.out"'
check "settings saved on Tares" '[ "$(api GET /api/crew/settings | jq -c "[.autonomy, .releaser, .challenger, .builders_max]")" = "[\"L4-ship\",false,true,2]" ]'
check "grant saved on Tares" 'api GET /api/grants | jq -r .all | grep -q "the reviewer may comment on draft PRs"'
check "the token is kept in a file only the person reads" '[ "$(cat "$FACTORY_CREW_HOME/.tares-token")" = stub-token ] && [ "$(stat -c %a "$FACTORY_CREW_HOME/.tares-token" 2>/dev/null || stat -f %Lp "$FACTORY_CREW_HOME/.tares-token")" = 600 ]'
check "stations are told where the token is" '[ "$(envof crew-reviewer TARES_FACTORY_TOKEN_FILE)" = "$FACTORY_CREW_HOME/.tares-token" ]'
check "the plugin's header helper gets the same token file" '[ -n "$(envof crew-reviewer TARES_FACTORY_CRED_FILE)" ] && [ "$(envof crew-reviewer TARES_FACTORY_CRED_FILE)" = "$(envof crew-reviewer TARES_FACTORY_TOKEN_FILE)" ]'
check "no token in settings files" '! grep -rq stub-token "$FACTORY_CREW_HOME/stations"'
check "stations reach the same Tares" '[ "$(envof crew-reviewer TARES_FACTORY_URL)" = "$TARESD_URL" ]'
today=$(date -u +%Y-%m-%d)
check "grant from Tares quoted with its date" 'allow_has crew-reviewer "granted this on $today for all projects, in their own words: \"the reviewer may comment on draft PRs\". Acting"'
check "no undated or doubly quoted grants" '! jq -e "any(.autoMode.allow[]; contains(\"undated\") or contains(\"\\\"\\\"\"))" "$FACTORY_CREW_HOME/stations/crew-reviewer.settings.json" >/dev/null'
one_home=$FACTORY_CREW_HOME

# A second machine: nothing is asked again and the station settings come out the same.
nomcp="http://127.0.0.1:$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')/mcp"
TARES_FACTORY_MCP_URL=$nomcp "$F" crew up --yes >"$T/nomcp.out" 2>&1
check "no MCP server with auth on: the command carries a token placeholder, never the token" 'grep -q "TARES_AUTH_TOKEN=<your token> tares mcp --transport streamable-http" "$T/nomcp.out" && ! grep -q stub-token "$T/nomcp.out"'
"$F" doctor >"$T/doctor.out" 2>&1
check "the MCP probe carries the token (auth on)" 'grep -qx "  ok    MCP endpoint answers at $TARES_FACTORY_MCP_URL" "$T/doctor.out"'

machine two
"$F" crew up </dev/null >"$T/up2.out" 2>&1
check "second machine asks nothing" '! grep -q "Choice \[" "$T/up2.out" && grep -q "started crew-reviewer" "$T/up2.out"'
for r in orchestrator reviewer; do
  check "same settings for crew-$r" 'diff <(sed "s|$one_home|CREW|g" "$one_home/stations/crew-$r.settings.json") <(sed "s|$FACTORY_CREW_HOME|CREW|g" "$FACTORY_CREW_HOME/stations/crew-$r.settings.json") >/dev/null'
done

# A builder gets its project's grants as well.
repo="$T/links"; mkdir -p "$repo"; git -C "$repo" init -q
P=$(hand_over "Links App" "$repo")
api POST /api/grants "$(jq -cn --arg p "$P" '{words: "builders may bump patch versions of dependencies", scope: "project", project: $p}')" >/dev/null
"$F" spawn builder "Links App" m1 "$repo" "work T1" >/dev/null 2>&1
check "project grant quoted for the builder" 'allow_has links-app-build-m1 "for project Links App, in their own words: \"builders may bump patch versions of dependencies\". Acting"'
check "and the grants for all" 'allow_has links-app-build-m1 "in their own words: \"the reviewer may comment on draft PRs\""'
check "L4: builder may merge on the gate" 'jq -e ".autoMode.allow | any(test(\"pre-authorized at level L4-ship\"))" "$FACTORY_CREW_HOME/stations/links-app-build-m1.settings.json" >/dev/null'
"$F" retire links-app-build-m1 >/dev/null 2>&1

# The orchestrator runs `factory spawn` with its settings env, not the human's shell: it still finds
# the crew home, Tares and the token, and gives the builder the same.
orch_env=(HOME="$HOME" PATH="$PATH" CLAUDE_BIN="$CLAUDE_BIN" FAKE_CLAUDE_STATE="$FAKE_CLAUDE_STATE" FACTORY_PAUSE=0)
while IFS= read -r kv; do orch_env+=("$kv"); done < <(jq -r '.env | to_entries[] | "\(.key)=\(.value)"' "$FACTORY_CREW_HOME/stations/crew-orchestrator.settings.json")
env -i "${orch_env[@]}" "$F" spawn builder "Links App" m3 "$repo" "work T3" >"$T/orch.out" 2>&1
check "orchestrator's own spawn reaches Tares with the token file" 'grep -q "started links-app-build-m3" "$T/orch.out"'
check "its builder gets the token file too" '[ "$(envof links-app-build-m3 TARES_FACTORY_TOKEN_FILE)" = "$FACTORY_CREW_HOME/.tares-token" ]'
"$F" retire links-app-build-m3 >/dev/null 2>&1
mv "$FACTORY_CREW_HOME/.tares-token" "$T/token.bak"
env -i "${orch_env[@]}" "$F" spawn builder "Links App" m4 "$repo" "work T4" >"$T/notoken.out" 2>&1
check "unreadable crew settings stop a spawn plainly" 'grep -q "could not read the crew settings" "$T/notoken.out" && ! grep -q "integer expression" "$T/notoken.out" && [ ! -f "$FACTORY_CREW_HOME/stations/links-app-build-m4.settings.json" ]'
mv "$T/token.bak" "$FACTORY_CREW_HOME/.tares-token"

# Progress comes from Tares: status and projects read the project's tickets.
api POST "/api/projects/$P/milestones" '{"name": "Links", "goal": "short links"}' >/dev/null
api POST "/api/projects/$P/tickets" '{"title": "Store", "milestone": "Links"}' >/dev/null
api POST "/api/projects/$P/tickets" '{"title": "Redirect", "milestone": "Links"}' >/dev/null
api POST "/api/projects/$P/tickets/T1/stage" '{"stage": "doing"}' >/dev/null
api POST "/api/projects/$P/tickets/T1/stage" '{"stage": "merged"}' >/dev/null
check "status has a line per project from Tares" 'grep -q "^project Links App: 1/2 tickets done, on Links 1/2" <<<"$("$F" crew status 2>/dev/null)"'
check "projects reads the same" 'grep -E "^Links App +1/2 +Links 1/2 " <<<"$("$F" projects 2>/dev/null)" >/dev/null'

# Tares down: commands say so instead of guessing.
stop_tares
"$F" settings crew-reviewer >"$T/down.json" 2>"$T/down.err"
check "unreachable Tares is reported, with how to start one" 'grep -q "no Tares answers at" "$T/down.err" && grep -q "tares up" "$T/down.err" && [ ! -s "$T/down.json" ]'

[ $fail = 0 ] && echo "tares: all cases pass"
exit $fail
