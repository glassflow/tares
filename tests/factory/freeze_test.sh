#!/usr/bin/env bash
# Checks `crew freeze` and `crew unfreeze` on a real Tares with a fake claude: the freeze is kept in the
# crew settings on Tares and shown by status and doctor; a frozen crew's watch reports a newer kit once,
# moves no station onto it and does not restart onto it itself, yet still revives a station that stopped,
# on a copy of the kit as it was when it was frozen; unfreeze lets the existing upgrade path run, and
# the copy goes once no station runs from it.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib.sh"
T=$(cd "$(mktemp -d)" && pwd -P)
mkdir -p "$T/kit-under-test"; cp -R "$here/../../claude-plugin/." "$T/kit-under-test/"; F="$T/kit-under-test/bin/factory"
export HOME="$T/home" FACTORY_CREW_HOME="$T/crew" FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
unset TARES_URL TARES_AUTH_TOKEN TARES_FACTORY_URL TARES_FACTORY_TOKEN_FILE FACTORY_REPO FACTORY_WORKTREES CLAUDE_JOB_DIR TARES_FACTORY_MCP_URL TARES_FACTORY_CRED_FILE TARES_MCP_HOST TARES_MCP_PORT
mkdir -p "$HOME"
chmod +x "$CLAUDE_BIN"
WATCH=""
trap '[ -n "$WATCH" ] && kill "$WATCH" 2>/dev/null; [ -n "$STUB" ] && kill "$STUB" 2>/dev/null; stop_tares; rm -rf "$T"' EXIT
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; fi; }
st() { jq -r --arg s "$1" ".[\$s]$2 // empty" "$FACTORY_CREW_HOME/stations.json"; }
agent() { local tmp; tmp=$(mktemp); jq --arg id "$(st "$1" .id)" "map(if .id == \$id then $2 else . end)" "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"; }
launches() { grep -c '"--bg"' "$FAKE_CLAUDE_STATE/calls.jsonl" 2>/dev/null; true; }
resumes() { grep -c '"--resume"' "$FAKE_CLAUDE_STATE/calls.jsonl" 2>/dev/null; true; }
last() { jq -c "select(.args[2] == \"$1\") | .args" "$FAKE_CLAUDE_STATE/calls.jsonl" | tail -1; }
plugin_dir() { last "$1" | jq -r '.[(index("--plugin-dir") // -2) + 1]'; }
events() { jq -r --arg e "$1" 'select(.event == $e) | .detail' "$FACTORY_CREW_HOME/events.jsonl" 2>/dev/null | wc -l | tr -d ' '; }
frozen() { api GET /api/crew/settings | jq -c ".kit.frozen$1"; }
older() { local tmp; tmp=$(mktemp); jq --arg s "$1" '.[$s].kit = "older"' "$FACTORY_CREW_HOME/stations.json" > "$tmp" && mv "$tmp" "$FACTORY_CREW_HOME/stations.json"; }
change_kit() { echo "<!-- $1 -->" >> "$T/kit-under-test/skills/factory-protocol/SKILL.md"; }
stopped() { local tmp; tmp=$(mktemp); jq --arg id "$(st "$1" .id)" 'map(if .id == $id then .state = "stopped" | del(.pid) else . end)' "$FAKE_CLAUDE_STATE/agents.json" > "$tmp" && mv "$tmp" "$FAKE_CLAUDE_STATE/agents.json"; }
wait_for() { local _; for _ in $(seq 40); do eval "$1" && return 0; sleep 0.5; done; return 1; }
SNAP="$FACTORY_CREW_HOME/frozen-kit"
CHECKOUT=$(cd -P "$T/kit-under-test" && pwd)
STUB=""

start_tares "$T/tares"
trust "$FACTORY_CREW_HOME"
api PUT /api/crew/settings '{"autonomy": "L3-review", "releaser": false, "challenger": false, "builders_max": 2}' >/dev/null
"$F" crew up --yes >"$T/up.out" 2>&1 || { echo "FAIL crew up"; cat "$T/up.out"; exit 1; }
for s in crew-orchestrator crew-reviewer; do agent $s '.state = "done"'; done
# From here on the crew home is reached through a link; stations record the real path of the copy.
ln -s "$T/crew" "$T/crewlink"; export FACTORY_CREW_HOME="$T/crewlink"
SNAP="$T/crew/frozen-kit"

# Not frozen: nothing says so, the setting is null and there is no copy.
"$F" crew status >"$T/status0.out" 2>&1
check "an unfrozen crew's status has no FROZEN line" '! grep -q "^FROZEN" "$T/status0.out"'
check "an unfrozen crew's setting is null" '[ "$(frozen "")" = null ]'
"$F" doctor >"$T/doctor0.out" 2>&1
check "doctor says the kit follows the checkout" 'grep -q "ok    the kit follows the plugin checkout (not frozen)" "$T/doctor0.out"'
"$F" crew unfreeze >"$T/unfreeze0.out" 2>&1; rc=$?
check "unfreeze when not frozen says so and exits 0" '[ $rc = 0 ] && grep -q "the kit is not frozen" "$T/unfreeze0.out"'

# Freeze: the setting on Tares (when, who, which kit) and a copy of the kit as it is now.
"$F" crew freeze >"$T/freeze.out" 2>&1; rc=$?
check "freeze exits 0" '[ $rc = 0 ]'
check "freeze keeps when, who and the kit version on Tares" '[ "$(frozen "|keys|join(\",\")")" = "\"at,by,version\"" ] && [ "$(frozen .by)" = "\"$(id -un)\"" ]'
check "the copy is the kit as it was" 'diff -r "$T/kit-under-test" "$SNAP" >/dev/null && [ -x "$SNAP/bin/factory" ]'
check "freeze is in the events" '[ "$(events freeze)" = 1 ]'
V0=$(frozen .version | tr -d '"'); AT0=$(frozen .at)
"$F" crew status >"$T/status1.out" 2>&1
check "status shows the freeze, when and by whom" 'grep -q "^FROZEN: the kit since .* by $(id -un); stations that start or revive use it, running ones keep reading the checkout until they restart; the watch moves no station onto a newer kit\$" "$T/status1.out"'
"$F" doctor >"$T/doctor1.out" 2>&1
check "doctor shows the freeze" 'grep -q "ok    the kit is frozen since .* by $(id -un); the checkout has nothing newer" "$T/doctor1.out"'
sleep 1
"$F" crew freeze >"$T/freeze2.out" 2>&1; rc=$?
check "freezing twice says so, exits 0 and keeps the first time and who" '[ $rc = 0 ] && grep -q "already frozen" "$T/freeze2.out" && [ "$(frozen .at)" = "$AT0" ] && [ "$(events freeze)" = 1 ]'
check "freezing again takes no second copy" '[ ! -e "$SNAP.new" ]'

# A newer kit, while frozen, with every station up and idle on the old kit: the running watch reports it
# once and moves nothing, and does not restart itself onto it.
launches0=$(launches); resumes0=$(resumes); o=$(st crew-orchestrator .id); r=$(st crew-reviewer .id)
"$F" crew watch --interval 1 >"$T/watch.out" 2>&1 &
WATCH=$!
sleep 2
change_kit v1
check "the watch reports the newer kit" 'wait_for "[ \"\$(events kit-newer)\" = 1 ]"'
sleep 4
check "the watch says so once, not every round" '[ "$(events kit-newer)" = 1 ] && [ "$(grep -c "is newer than the frozen kit $V0; not moving stations" "$T/watch.out")" = 1 ]'
check "the watch does not restart onto the new kit" '! grep -q "the watch restarts on the new version" "$T/watch.out" && kill -0 "$WATCH" 2>/dev/null'
check "no station restarts for a kit while frozen" '[ "$(launches)" = "$launches0" ] && [ "$(resumes)" = "$resumes0" ] && [ "$(events upgraded)" = 0 ] && [ "$(st crew-orchestrator .id)" = "$o" ] && [ "$(st crew-reviewer .id)" = "$r" ]'
kill "$WATCH" 2>/dev/null; wait "$WATCH" 2>/dev/null; WATCH=""
check "the report names the kit and the way out" 'grep -q "crew unfreeze" "$T/watch.out"'
"$F" crew watch --once >"$T/once1.out" 2>&1
check "a later round, even a new watch, does not report it again" '[ "$(events kit-newer)" = 1 ] && [ "$(st crew-orchestrator .id)" = "$o" ]'
"$F" crew status >"$T/status2.out" 2>&1
check "status says a newer kit is available" 'grep -q "^FROZEN: .*(a newer kit is available)\$" "$T/status2.out"'
check "status says the stations are held by the freeze" 'grep -q "^plugin: 2 station(s) run an older version; the freeze holds them" "$T/status2.out"'
"$F" doctor >"$T/doctor2.out" 2>&1
check "doctor warns that a newer kit is not followed" 'grep -q "warn  the kit is frozen since .*the checkout has a newer kit that no station moves onto" "$T/doctor2.out"'
change_kit v2
"$F" crew watch --once >"$T/once2.out" 2>&1
"$F" crew watch --once >"$T/once3.out" 2>&1
check "another newer kit is reported once more" '[ "$(events kit-newer)" = 2 ]'
check "still nothing restarted" '[ "$(launches)" = "$launches0" ] && [ "$(events upgraded)" = 0 ]'

# A station that stopped is still revived, on the copy of the kit as it was frozen.
stopped crew-reviewer
"$F" crew watch --once >"$T/revive.out" 2>&1
check "a stopped station is revived while frozen" '[ "$(st crew-reviewer .id)" != "$r" ] && [ "$(launches)" = $((launches0 + 1)) ]'
check "its plugin is the frozen copy" '[ "$(plugin_dir crew-reviewer)" = "$SNAP" ] && [ "$(st crew-reviewer .plugin)" = "$SNAP" ]'
check "its status line is the frozen copy's" 'jq -e --arg p "python3 $SNAP/scripts/statusline.py" ".statusLine.command == \$p" "$FACTORY_CREW_HOME/stations/crew-reviewer.settings.json" >/dev/null'
check "it records the frozen kit as its own" '[ "$(st crew-reviewer .kit)" = "$V0" ]'
check "the revive resumed its conversation, and nothing else moved" '[ "$(resumes)" = $((resumes0 + 1)) ] && [ "$(st crew-orchestrator .id)" = "$o" ]'

# Unfreeze: the existing upgrade path runs, one station a round, in the same conversation.
agent crew-reviewer '.state = "done"'
"$F" crew unfreeze >"$T/unfreeze.out" 2>&1; rc=$?
check "unfreeze exits 0 and clears the setting" '[ $rc = 0 ] && [ "$(frozen "")" = null ] && [ "$(events unfreeze)" = 1 ]'
check "the copy stays while a station runs from it" '[ -d "$SNAP" ] && grep -q "frozen-kit once none runs from it" "$T/unfreeze.out"'
"$F" crew status >"$T/status3.out" 2>&1
check "an unfrozen crew's status has no FROZEN line, and the watch moves the stations again" '! grep -q "^FROZEN" "$T/status3.out" && grep -q "^plugin: 2 station(s) run an older version; .*crew watch moves each onto this one" "$T/status3.out"'
orch_session=$(st crew-orchestrator .sessionId)
"$F" crew watch --once >"$T/up1.out" 2>&1
check "unfrozen: one station moves onto the new kit" '[ "$(events upgraded)" = 1 ] && [ "$(st crew-orchestrator .id)" != "$o" ] && [ "$(st crew-reviewer .id)" != "" ]'
check "in the same conversation, from the checkout" 'last crew-orchestrator | jq -e --arg s "$orch_session" "index(\"--resume\") as \$i | \$i != null and .[\$i+1] == \$s" >/dev/null && [ "$(plugin_dir crew-orchestrator)" = "$CHECKOUT" ]'
check "the copy stays while another station still runs from it" '[ -d "$SNAP" ]'
agent crew-orchestrator '.state = "done"'
"$F" crew watch --once >"$T/up2.out" 2>&1
check "the next round moves the other, off the frozen copy" '[ "$(events upgraded)" = 2 ] && [ "$(plugin_dir crew-reviewer)" = "$CHECKOUT" ]'
"$F" crew watch --once >"$T/up3.out" 2>&1
check "the copy is removed once no station runs from it" '[ ! -e "$SNAP" ]'

# Nothing newer when it is unfrozen: a station that came back on the copy still leaves it.
"$F" crew freeze >/dev/null 2>&1
agent crew-reviewer '.state = "done"'; stopped crew-reviewer
"$F" crew watch --once >/dev/null 2>&1
check "frozen again: a station revived on the new copy" '[ "$(st crew-reviewer .plugin)" = "$SNAP" ] && [ "$(st crew-reviewer .kit)" = "$(frozen .version | tr -d \")" ]'
agent crew-reviewer '.state = "done"'
"$F" crew status >"$T/status4.out" 2>&1
check "with nothing newer, the freeze holds no station back" 'grep -q "^FROZEN: the kit since " "$T/status4.out" && ! grep -q "newer kit is available" "$T/status4.out" && ! grep -q "^plugin:" "$T/status4.out"'
V1=$(frozen .version | tr -d '"')

# Unfrozen with a station still on the copy, and a factory run from that copy: it means the checkout.
"$F" crew unfreeze >/dev/null 2>&1
check "the copy stays for the station on it" '[ -d "$SNAP" ] && [ "$(st crew-reviewer .plugin)" = "$SNAP" ]'
stopped crew-orchestrator
"$SNAP/bin/factory" crew watch --once >"$T/fromcopy.out" 2>&1
check "a start by the copy's own factory uses the checkout, not the copy" '[ "$(plugin_dir crew-orchestrator)" = "$CHECKOUT" ] && [ "$(st crew-orchestrator .plugin)" = "$CHECKOUT" ]'
agent crew-orchestrator '.state = "done"'

# Freezing while a station still runs from the copy: the copy stays if it is still the checkout's kit.
touch "$SNAP/.kept"
"$F" crew freeze >"$T/refreeze.out" 2>&1; rc=$?
check "freeze keeps a copy in use when it is still the checkout's kit" '[ $rc = 0 ] && [ -e "$SNAP/.kept" ] && [ "$(frozen .version | tr -d \")" = "$V1" ] && [ ! -e "$T/crew/freezing" ]'
"$F" crew unfreeze >/dev/null 2>&1
change_kit v3
"$F" crew freeze >"$T/refuse.out" 2>&1; rc=$?
check "freeze refuses while a station runs from a copy that is not the checkout's kit" '[ $rc != 0 ] && grep -q "still run from the kit copy" "$T/refuse.out" && [ -e "$SNAP/.kept" ] && [ "$(frozen "")" = null ] && [ ! -e "$T/crew/freezing" ]'

# The watch moves the stations off the copy; a freeze under way keeps the copy, a stale marker does not.
for _ in 1 2 3 4; do
  [ "$(st crew-reviewer .plugin)" != "$SNAP" ] && break
  for s in crew-orchestrator crew-reviewer; do agent $s '.state = "done"'; done
  "$F" crew watch --once >/dev/null 2>&1
done
check "the stations left the copy" '[ "$(st crew-reviewer .plugin)" = "$CHECKOUT" ]'
touch "$T/crew/freezing"
"$F" crew watch --once >/dev/null 2>&1
check "a copy nothing runs from stays while a freeze is under way" '[ -d "$SNAP" ]'
touch -t 200001010000 "$T/crew/freezing"
"$F" crew watch --once >/dev/null 2>&1
check "a marker that old is ignored and the copy goes" '[ ! -e "$SNAP" ] && [ ! -e "$T/crew/kit-checkout" ]'
rm -f "$T/crew/freezing"

# With no station on the copy, unfreeze removes it at once; a freeze of a crew with no Tares is refused.
"$F" crew freeze >/dev/null 2>&1
check "freeze after that takes a fresh copy" '[ -x "$SNAP/bin/factory" ] && [ ! -e "$SNAP/.kept" ] && diff -r "$T/kit-under-test" "$SNAP" >/dev/null'
"$F" crew unfreeze >/dev/null 2>&1
check "unfreeze removes a copy nothing runs from" '[ ! -e "$SNAP" ] && [ "$(frozen "")" = null ]'
TARESD_URL=http://127.0.0.1:9 "$F" crew freeze >"$T/notares.out" 2>&1; rc=$?
check "freeze needs Tares" '[ $rc != 0 ] && grep -q "no Tares answers\|could not read the crew settings" "$T/notares.out" && [ ! -e "$SNAP" ]'

# A Tares that answers the freeze without keeping it: freeze dies and leaves nothing behind.
api GET /api/crew/settings | jq 'del(.kit)' > "$T/stub-settings.json"
cat >"$T/stub.py" <<'PY'
import http.server, os, sys
body = open(sys.argv[2]).read().encode()
class H(http.server.BaseHTTPRequestHandler):
    def reply(self, b):
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/health": self.reply(b'{"ok": true}')
        elif os.path.exists(sys.argv[3]): self.send_error(500)
        else: self.reply(body)
    def do_PUT(self): self.rfile.read(int(self.headers.get("Content-Length", 0))); self.reply(body)
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
sp=$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
"$PY" "$T/stub.py" "$sp" "$T/stub-settings.json" "$T/stub-fail" >/dev/null 2>&1 &
STUB=$!
wait_for "curl -sf http://127.0.0.1:$sp/health >/dev/null"
TARESD_URL=http://127.0.0.1:$sp "$F" crew freeze >"$T/nokeep.out" 2>&1; rc=$?
check "freeze dies when Tares does not keep the setting" '[ $rc != 0 ] && grep -q "did not keep the freeze" "$T/nokeep.out"'
check "and leaves no copy, marker or setting behind" '[ ! -e "$SNAP" ] && [ ! -e "$SNAP.new" ] && [ ! -e "$T/crew/freezing" ] && [ ! -e "$T/crew/kit-checkout" ] && [ "$(frozen "")" = null ]'

# Tares answers but cannot give its settings (the blip upgrades_frozen guards): the copy on disk still
# means frozen, so nothing moves, and status does not print blank fields.
"$F" crew freeze >/dev/null 2>&1
change_kit v4
for s in crew-orchestrator crew-reviewer; do agent $s '.state = "done"'; done
launches1=$(launches)
touch "$T/stub-fail"
TARESD_URL=http://127.0.0.1:$sp "$F" crew watch --once >"$T/blip.out" 2>&1
TARESD_URL=http://127.0.0.1:$sp "$F" crew status >"$T/status5.out" 2>&1
rm -f "$T/stub-fail"
check "with the settings unreadable and the copy there, the watch does not try to move a station" '! grep -q "could not restart\|moves onto the new version once it does" "$T/blip.out" && [ "$(launches)" = "$launches1" ]'
check "and status says frozen, without blank fields" 'grep -q "^FROZEN: the kit (Tares does not say since when);" "$T/status5.out"'
"$F" crew unfreeze >/dev/null 2>&1

# A watch started from the frozen copy and left running past the unfreeze: once the copy is gone, a kit
# change restarts it on the checkout's CLI, not on the copy it came from (TR-686).
for _ in 1 2 3 4; do
  for s in crew-orchestrator crew-reviewer; do agent $s '.state = "done"'; done
  "$F" crew watch --once >/dev/null 2>&1
done
"$F" crew freeze >/dev/null 2>&1
agent crew-reviewer '.state = "done"'; stopped crew-reviewer
"$F" crew watch --once >/dev/null 2>&1
"$F" crew unfreeze >/dev/null 2>&1
check "setup: a station is on the copy, which is kept after the unfreeze" '[ -x "$SNAP/bin/factory" ] && [ "$(st crew-reviewer .plugin)" = "$SNAP" ]'
agent crew-reviewer '.state = "done"'
"$SNAP/bin/factory" crew watch --interval 1 >"$T/copywatch.out" 2>&1 &
WATCH=$!
check "the watch moves the station off the copy and removes it" 'wait_for "[ ! -e \"\$SNAP\" ]"'
check "setup: the watch still runs, from the copy it started on" 'kill -0 "$WATCH" 2>/dev/null && ps -p "$WATCH" -o command= | grep -q "$SNAP/bin/factory"'
change_kit v5
check "the watch restarts on the new version" 'wait_for "grep -q \"the watch restarts on the new version\" \"\$T/copywatch.out\"" && sleep 2'
check "and it keeps running, on the checkout's CLI, not the copy that is gone" \
  'kill -0 "$WATCH" 2>/dev/null && ps -p "$WATCH" -o command= | grep -q "$CHECKOUT/bin/factory" && ! ps -p "$WATCH" -o command= | grep -q "$SNAP" && ! grep -q "No such file" "$T/copywatch.out"'
kill "$WATCH" 2>/dev/null; wait "$WATCH" 2>/dev/null; WATCH=""

[ $fail = 0 ] && echo "freeze: all cases pass"
exit $fail
