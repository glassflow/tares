#!/usr/bin/env bash
# Checks `tares factory spec`: it opens the person's own Claude Code session with /tares:spec in the
# repo the project builds on (from anywhere inside it), or in a folder that holds its repos, names the
# session after the idea, loads this plugin, and writes nothing into the repos. Uses a fake claude.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib.sh"
F="$here/../../claude-plugin/bin/factory"
T=$(cd "$(mktemp -d)" && pwd -P)
export HOME="$T/home" FACTORY_CREW_HOME="$T/crew" FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
unset TARES_URL TARES_FACTORY_URL TARES_FACTORY_TOKEN_FILE FACTORY_REPO
mkdir -p "$HOME"
chmod +x "$CLAUDE_BIN"
trap 'stop_tares; rm -rf "$T"' EXIT
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; fi; }
last() { tail -1 "$FAKE_CLAUDE_STATE/calls.jsonl"; }
plugin=$(cd -P "$here/../../claude-plugin" && pwd)

mkrepo() { mkdir -p "$1" && git -C "$1" init -q && echo x > "$1/README.md" && git -C "$1" add . && git -C "$1" commit -q -m init; }
mkrepo "$T/shop"; mkdir -p "$T/shop/src"

(cd "$T/shop/src" && TARESD_URL=http://127.0.0.1:9 "$F" spec "an order form" >"$T/notares.out" 2>&1)
check "without Tares the spec says how to start one" 'grep -q "no Tares answers" "$T/notares.out" && [ ! -f "$FAKE_CLAUDE_STATE/calls.jsonl" ]'

start_tares "$T/tares"
(cd "$T/shop/src" && "$F" spec "an order form for the bakery shop" >"$T/spec.out" 2>&1)
check "the spec runs in the repo root" '[ "$(last | jq -r .cwd)" = "$T/shop" ]'
check "the session is named after the idea" '[ "$(last | jq -r ".args[1]")" = an-order-form-for-spec ]'
check "it loads this plugin" '[ "$(last | jq -r ".args | index(\"--plugin-dir\") as \$i | .[\$i+1]")" = "$plugin" ]'
check "it runs /tares:spec with the idea" '[ "$(last | jq -r ".args[-1]")" = "/tares:spec an order form for the bakery shop" ]'
check "it is the person's own session, not a background one" '! last | jq -e ".args | index(\"--bg\")" >/dev/null'
check "it says the plan goes to Tares" 'grep -q "into a project on Tares ($TARESD_URL)" "$T/spec.out"'
check "nothing is written into the repo" '[ -z "$(git -C "$T/shop" status --porcelain)" ] && [ ! -e "$T/shop/.factory" ]'

(cd "$T/shop" && "$F" spec --project "Bakery orders" >/dev/null 2>&1)
check "a project name names the session and goes to /tares:spec" '[ "$(last | jq -r ".args[1]")" = bakery-orders-spec ] && [ "$(last | jq -r ".args[-1]")" = "/tares:spec (project name: Bakery orders)" ]'

# A folder that holds the project's repos: the spec runs there and sees them all.
W="$T/work"; mkrepo "$W/api"; mkrepo "$W/web"; mkdir -p "$W/notes"
(cd "$W" && "$F" spec "orders across api and web" >"$T/multi.out" 2>&1)
check "a folder of repos: the spec runs in the folder" '[ "$(last | jq -r .cwd)" = "$W" ]'
check "and names the repos it holds" 'grep -q "holds 2 repo(s): api, web" "$T/multi.out"'

mkdir -p "$T/empty"
(cd "$T/empty" && "$F" spec "nothing here" >"$T/empty.out" 2>&1); rc=$?
check "a folder with no repo is refused plainly" '[ $rc != 0 ] && grep -q "inside the git repo the project builds on, or in a folder that holds its repos" "$T/empty.out"'
mkdir -p "$T/fresh" && git -C "$T/fresh" init -q
(cd "$T/fresh" && "$F" spec "x" >"$T/fresh.out" 2>&1); rc=$?
check "a repo with no commit is refused plainly" '[ $rc != 0 ] && grep -q "has no commit yet" "$T/fresh.out"'

[ $fail = 0 ] && echo "spec: all cases pass"
exit $fail
