#!/usr/bin/env bash
# Checks `tares factory session` (TR-520): it opens the person's own Claude Code session with this
# plugin, bound to a planned project (TARES_PROJECT) or planning a new idea lightly; a project the
# crew holds is taken over only on the person's yes, recorded on Tares in their words; no argument
# asks which project. Uses a fake claude and a real Tares.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib.sh"
F="$here/../../claude-plugin/bin/factory"
T=$(cd "$(mktemp -d)" && pwd -P)
export HOME="$T/home" FACTORY_CREW_HOME="$T/crew" FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
unset TARES_URL TARES_FACTORY_URL TARES_FACTORY_TOKEN_FILE FACTORY_REPO TARES_FACTORY_MCP_URL TARES_FACTORY_CRED_FILE TARES_MCP_HOST TARES_MCP_PORT FACTORY_STATION TARES_PROJECT
mkdir -p "$HOME"
chmod +x "$CLAUDE_BIN"
trap 'stop_tares; rm -rf "$T"' EXIT
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; fi; }
last() { tail -1 "$FAKE_CLAUDE_STATE/calls.jsonl"; }
calls() { [ -f "$FAKE_CLAUDE_STATE/calls.jsonl" ] && wc -l < "$FAKE_CLAUDE_STATE/calls.jsonl" | tr -d ' ' || echo 0; }
envof() { grep "^$1=" "$FAKE_CLAUDE_STATE/last.env" | head -1 | cut -d= -f2-; }
plugin=$(cd -P "$here/../../claude-plugin" && pwd)
mkrepo() { mkdir -p "$1" && git -C "$1" init -q && echo x > "$1/README.md" && git -C "$1" add . && git -C "$1" commit -q -m init; }
newproject() { api POST /api/projects "$(jq -cn --arg n "$1" '{template: "custom", kind: "software_factory", name: $n, objects: []}')" | jq -r .id; }

mkrepo "$T/shop"; mkdir -p "$T/elsewhere"
(cd "$T/elsewhere" && TARESD_URL=http://127.0.0.1:9 "$F" session Shop >"$T/notares.out" 2>&1)
check "without Tares it says how to start one" 'grep -q "no Tares answers" "$T/notares.out" && [ "$(calls)" = 0 ]'

start_tares "$T/tares"
shop=$(newproject Shop); notes=$(newproject "Notes app")
api POST /api/projects '{"template":"custom","name":"Not factory","objects":[]}' >/dev/null

(cd "$T/elsewhere" && FACTORY_STATION=crew-orchestrator "$F" session Shop >"$T/station.out" 2>&1); rc=$?
check "a station may not open one" '[ $rc != 0 ] && grep -q "only the person runs this" "$T/station.out" && [ "$(calls)" = 0 ]'

(cd "$T/elsewhere" && "$F" session "Notes app" </dev/null >"$T/notes.out" 2>&1)
check "a planned project: the person's own session" '[ "$(calls)" = 1 ] && ! last | jq -e ".args | index(\"--bg\")" >/dev/null'
check "named after the project" '[ "$(last | jq -r ".args[1]")" = notes-app-session ]'
check "it loads this plugin and turns it on" '[ "$(last | jq -r ".args | index(\"--plugin-dir\") as \$i | .[\$i+1]")" = "$plugin" ] && jq -e ".enabledPlugins[\"tares@inline\"] == true" "$(last | jq -r ".args | index(\"--settings\") as \$i | .[\$i+1]")" >/dev/null'
check "it runs /tares:session with the project" '[ "$(last | jq -r ".args[-1]")" = "/tares:session Notes app" ]'
check "the session is bound to the project" '[ "$(envof TARES_PROJECT)" = "$notes" ] && [ "$(envof TARES_PROJECT_NAME)" = "Notes app" ] && [ "$(envof TARES_SESSION_NAME)" = notes-app-session ] && [ -z "$(envof TARES_SESSION_TAKEOVER)" ]'
check "and told the Tares and its MCP endpoint" '[ "$(envof TARESD_URL)" = "$TARESD_URL" ] && [ "$(envof TARES_FACTORY_MCP_URL)" = "$TARES_FACTORY_MCP_URL" ]'
check "with no repo of its own it opens where you are" '[ "$(last | jq -r .cwd)" = "$T/elsewhere" ]'
(cd "$T/elsewhere" && "$F" session notes-app </dev/null >/dev/null 2>&1)
check "the project's slug finds it too" '[ "$(last | jq -r ".args[-1]")" = "/tares:session Notes app" ]'

(cd "$T/shop" && "$F" session "a tiny url shortener" </dev/null >"$T/idea.out" 2>&1)
check "an idea: plan lightly and build here" '[ "$(last | jq -r ".args[-1]")" = "/tares:session new: a tiny url shortener" ] && [ "$(last | jq -r ".args[1]")" = a-tiny-url-shortener-session ]'
check "not bound to a project yet" '[ -z "$(envof TARES_PROJECT)" ]'
check "it opens in the repo you are in" '[ "$(last | jq -r .cwd)" = "$T/shop" ]'
check "it says the plan goes to Tares and the build happens here" 'grep -q "builds it in the same" "$T/idea.out"'
(cd "$T/shop" && "$F" session --new "Shop" </dev/null >/dev/null 2>&1)
check "--new makes even a project name an idea" '[ "$(last | jq -r ".args[-1]")" = "/tares:session new: Shop" ]'

n=$(calls)
(cd "$T/elsewhere" && printf '2\n' | "$F" session Widgets >"$T/nope.out" 2>&1); rc=$?
check "one unknown word: no, I meant a project, lists the open ones" '[ $rc != 0 ] && grep -q "the open projects: " "$T/nope.out" && grep -q "Shop" "$T/nope.out" && ! grep -q "Not factory" "$T/nope.out" && [ "$(calls)" = "$n" ]'
(cd "$T/elsewhere" && printf '1\n' | "$F" session Widgets >/dev/null 2>&1)
check "one unknown word: yes, a new project" '[ "$(last | jq -r ".args[-1]")" = "/tares:session new: Widgets" ]'

# The crew holds Shop: a builder holds T1 with a PR open.
api POST "/api/projects/$shop/handover" "$(jq -cn --arg p "$T/shop" '{repos: [{path: $p, name: "acme/shop"}]}')" >/dev/null
api POST "/api/projects/$shop/tickets" '{"title": "Catalog"}' >/dev/null
n=$(calls)
(cd "$T/elsewhere" && printf '\n' | "$F" session Shop >"$T/keep.out" 2>&1)
check "the crew holds it: Enter leaves it with the crew" 'grep -q "Left with the crew" "$T/keep.out" && grep -q "crew attach orchestrator" "$T/keep.out" && [ "$(calls)" = "$n" ]'
check "and nothing is recorded" '[ "$(api GET /api/crew/projects | jq -r "[.projects[].name] | index(\"Shop\") != null")" = true ] && [ "$(api GET /api/crew/projects | jq ".releasing | length")" = 0 ]'
(cd "$T/elsewhere" && printf '1\nsmall, I will finish it myself\n' | "$F" session Shop >"$T/take.out" 2>&1)
check "yes: taken over, in the person's words" '[ "$(api GET "/api/projects/$shop/crew" | jq -r .handover.takeover.words)" = "small, I will finish it myself" ]'
check "off the crew's list, releasing" '[ "$(api GET /api/crew/projects | jq -r "[.releasing[].name] | join(\",\")")" = Shop ] && [ "$(api GET /api/crew/projects | jq -r "[.projects[].name] | index(\"Shop\")")" = null ]'
check "the session knows it took it over" '[ "$(last | jq -r ".args[-1]")" = "/tares:session Shop (taken over from the crew)" ] && [ "$(envof TARES_SESSION_TAKEOVER)" = 1 ] && [ "$(envof TARES_PROJECT)" = "$shop" ]'
check "it opens in the project's repo" '[ "$(last | jq -r .cwd)" = "$T/shop" ]'
(cd "$T/elsewhere" && "$F" session Shop </dev/null >"$T/again.out" 2>&1)
check "while it is being released, a new session carries on the takeover without asking" '[ "$(last | jq -r ".args[-1]")" = "/tares:session Shop (taken over from the crew)" ] && grep -q "was taken over from the crew" "$T/again.out"'

# No argument: the open projects (marked) and something new.
(cd "$T/elsewhere" && printf '\n' | "$F" session >"$T/menu.out" 2>&1)
check "no argument lists the open factory projects" 'grep -q "Shop (taken over, being released)" "$T/menu.out" && grep -q "Notes app" "$T/menu.out" && grep -q "something new" "$T/menu.out" && ! grep -q "Not factory" "$T/menu.out"'
pos=$(grep -E '^  [0-9]+\) Notes app' "$T/menu.out" | sed -E 's/^  ([0-9]+)\).*/\1/')
(cd "$T/elsewhere" && printf '%s\n' "$pos" | "$F" session >/dev/null 2>&1)
check "picking a project opens it" '[ "$(last | jq -r ".args[-1]")" = "/tares:session Notes app" ]'
new=$(grep -E '^  [0-9]+\) something new' "$T/menu.out" | sed -E 's/^  ([0-9]+)\).*/\1/')
(cd "$T/elsewhere" && printf '%s\na habit tracker\n' "$new" | "$F" session >/dev/null 2>&1)
check "something new asks what to build" '[ "$(last | jq -r ".args[-1]")" = "/tares:session new: a habit tracker" ]'

check "nothing is written into the repo" '[ -z "$(git -C "$T/shop" status --porcelain)" ]'
[ $fail = 0 ] && echo "session: all checks passed"
exit $fail
