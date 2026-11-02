#!/usr/bin/env bash
# Checks what the crew's machine reports about what the crew spends, on a real Tares with a fake claude:
# every station runs the plugin's status line, which prints a short line and sends the session's context
# window and the plan's limits to Tares; `factory context` reads a station's /context through a copy of
# its session that is never saved, with hooks and the status line off; `factory plan` reads /usage; and
# the watch answers the Crew page's ask for a reading between its rounds.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib.sh"
T=$(cd "$(mktemp -d)" && pwd -P)
mkdir -p "$T/kit-under-test"; cp -R "$here/../../claude-plugin/." "$T/kit-under-test/"; F="$T/kit-under-test/bin/factory"
export HOME="$T/home" FACTORY_CREW_HOME="$T/crew" FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
unset TARES_URL TARES_AUTH_TOKEN TARES_FACTORY_URL TARES_FACTORY_TOKEN_FILE FACTORY_REPO FACTORY_WORKTREES CLAUDE_JOB_DIR TARES_FACTORY_MCP_URL TARES_FACTORY_CRED_FILE TARES_MCP_HOST TARES_MCP_PORT CLAUDE_PLUGIN_DATA
mkdir -p "$HOME"
chmod +x "$CLAUDE_BIN"
WATCH=""
trap '[ -n "$WATCH" ] && kill "$WATCH" 2>/dev/null; stop_tares; rm -rf "$T"' EXIT
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; fi; }

start_tares "$T/tares" usage-token
trust "$FACTORY_CREW_HOME"
api PUT /api/crew/settings '{"autonomy": "L3-review", "releaser": false, "challenger": false, "builders_max": 2}' >/dev/null
"$F" crew up --yes >"$T/up.out" 2>&1 || { echo "FAIL crew up"; cat "$T/up.out"; exit 1; }
S="$FACTORY_CREW_HOME/stations/crew-orchestrator.settings.json"
SID=$(jq -r '."crew-orchestrator".sessionId' "$FACTORY_CREW_HOME/stations.json")

check "a station is told where it compacts" '[ "$(jq -r .env.FACTORY_AUTOCOMPACT "$S")" = 200000 ]'
check "a station runs the plugin's status line" \
  '[ "$(jq -r .statusLine.command "$S")" = "python3 $T/kit-under-test/scripts/statusline.py" ] && [ "$(jq -r .statusLine.type "$S")" = command ]'

# The status line, as Claude Code runs it for the station: with the station's environment and its state on stdin.
api POST /ingest/claude_code "$(jq -cn --arg s "$SID" '{type: "session_station", sessionId: $s, timestamp: (now | todate),
  station: "crew-orchestrator", role: "orchestrator", parent: "", factory_station: "crew-orchestrator"}')" >/dev/null
status() { # used% five-hour% → the status line's output
  jq -cn --arg s "$SID" --argjson p "$1" --argjson h "$2" '{session_id: $s, session_name: "crew-orchestrator",
    model: {id: "claude-opus-5-5", display_name: "Opus 5.5"},
    context_window: {total_input_tokens: 95300, total_output_tokens: 800, context_window_size: 1000000, used_percentage: $p},
    rate_limits: {five_hour: {used_percentage: $h, resets_at: 4102444800}, seven_day: {used_percentage: 6, resets_at: 4102444800}},
    cost: {total_cost_usd: 1.5}}' \
  | env $(jq -r '.env | to_entries[] | "\(.key)=\(.value)"' "$S") python3 "$T/kit-under-test/scripts/statusline.py"
}
out=$(status 48 31)
check "the status line prints the window and the plan" '[ "$out" = "Opus 5.5 · context 48% of 1.0M · 5h 31% · week 6%" ]'
check "and Tares has the station's window, and how near it is to compacting" '[ "$(api GET /api/crew | jq -c ".stations[] | select(.name == \"crew-orchestrator\") | .context | [.pct, .compact_at]")" = "[48,200000]" ]'
check "and the plan's limits" '[ "$(api GET /api/claude-code/usage | jq -c "[.plan.limits[] | [.kind, .percent]]")" = "[[\"session\",31],[\"weekly_all\",6]]" ]'
sleep 1   # a report a second later than the status line's, to tell the two apart
api POST /ingest/claude_code "$(jq -cn --arg s "$SID" '{type: "session_status", sessionId: $s, timestamp: (now | todate), status: {context_window: {context_window_size: 200000, used_percentage: 1}}}')" >/dev/null
status 48 31 >/dev/null
check "an unchanged report within the minute is not sent again" '[ "$(api GET /api/crew | jq -r ".stations[] | select(.name == \"crew-orchestrator\") | .context.pct")" = 1 ]'
status 52 33 >/dev/null
check "a changed one is" '[ "$(api GET /api/crew | jq -r ".stations[] | select(.name == \"crew-orchestrator\") | .context.pct")" = 52 ]'
out=$(echo 'not json' | python3 "$T/kit-under-test/scripts/statusline.py"); rc=$?
check "bad input: a plain line, no failure" '[ $rc = 0 ] && [ "$out" = Claude ]'
out=$(status 52 33 2>&1 | TARES_FACTORY_URL=http://127.0.0.1:9 python3 "$T/kit-under-test/scripts/statusline.py" 2>&1; echo "rc=$?")
check "no Tares: the line still prints" 'grep -q "rc=0" <<<"$out"'

# /context of a station: through a copy of its session that is never saved, in its folder, with its settings.
"$F" context orchestrator >"$T/context.out" 2>&1; rc=$?
call=$(grep '"/context"' "$FAKE_CLAUDE_STATE/calls.jsonl" | tail -1)
check "factory context reads the station" '[ $rc = 0 ] && grep -q "crew-orchestrator: 95300 of 200000 tokens (48%), sent to Tares" "$T/context.out"'
check "through a copy that is never saved" 'jq -e --arg s "$SID" ".args | (index(\"--resume\") as \$i | .[\$i + 1] == \$s) and index(\"--fork-session\") and index(\"--no-session-persistence\") and index(\"-p\")" <<<"$call" >/dev/null'
check "in the station's folder, with its plugin" 'jq -e --arg d "$FACTORY_CREW_HOME" --arg k "$T/kit-under-test" ".cwd == \$d and (.args | index(\$k))" <<<"$call" >/dev/null'
check "with its settings, hooks and the status line off" 'jq -e ".disableAllHooks == true and (has(\"statusLine\") | not) and .env.FACTORY_STATION == \"crew-orchestrator\"" "$FAKE_CLAUDE_STATE/p-settings.json" >/dev/null'
check "Tares has the reading" '[ "$(api GET /api/crew/crew-orchestrator | jq -c "[.context_detail.total_tokens, .context_detail.mcp_servers[0].name]")" = "[95300,\"tares\"]" ]'
"$F" context nobody >"$T/nobody.out" 2>&1; rc=$?
check "an unknown station: said plainly" '[ $rc != 0 ] && grep -q "unknown station nobody" "$T/nobody.out"'

# /usage: every limit of the plan (a second after the last status line, so the newer reading is clear).
sleep 1
"$F" plan >"$T/plan.out" 2>&1; rc=$?
check "factory plan reads and sends the limits" '[ $rc = 0 ] && grep -q "session: 27% used" "$T/plan.out"'
check "with hooks off, nothing saved" 'grep "\"/usage\"" "$FAKE_CLAUDE_STATE/calls.jsonl" | tail -1 | jq -e ".args | index(\"--no-session-persistence\")" >/dev/null && jq -e ".disableAllHooks == true" "$FAKE_CLAUDE_STATE/p-settings.json" >/dev/null'
check "Tares has /usage's limits" '[ "$(api GET /api/claude-code/usage | jq -c "[.plan.limits[] | select(.from == \"usage\") | .kind]")" != "[]" ]'
FAKE_CLAUDE_NO_PLAN=1 "$F" plan >"$T/noplan.out" 2>&1; rc=$?
check "no plan to read (an API key): said plainly" '[ $rc != 0 ] && grep -q "Pro or Max" "$T/noplan.out"'

# The watch answers an ask from the Crew page between its rounds.
before=$(grep -c '"/context"' "$FAKE_CLAUDE_STATE/calls.jsonl")
api POST /api/crew/crew-orchestrator/context >/dev/null
FACTORY_ASK_POLL=1 FACTORY_PLAN_EVERY=0 "$F" crew watch --interval 60 >"$T/watch.out" 2>&1 & WATCH=$!
for _ in $(seq 40); do [ "$(api GET /api/crew/context/asks | jq '.asks | length')" = 0 ] && break; sleep 0.25; done
kill "$WATCH" 2>/dev/null; wait "$WATCH" 2>/dev/null; WATCH=""
check "the watch reads what the Crew page asked for" '[ "$(api GET /api/crew/context/asks | jq ".asks | length")" = 0 ] && [ "$(grep -c "\"/context\"" "$FAKE_CLAUDE_STATE/calls.jsonl")" -gt "$before" ]'
check "and reads no plan when told not to" '[ "$(grep -c "\"/usage\"" "$FAKE_CLAUDE_STATE/calls.jsonl")" = 2 ]'

[ $fail = 0 ] && echo "usage: all cases pass"
exit $fail
