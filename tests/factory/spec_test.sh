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
unset TARES_URL TARES_FACTORY_URL TARES_FACTORY_TOKEN_FILE FACTORY_REPO TARES_FACTORY_MCP_URL TARES_FACTORY_CRED_FILE TARES_MCP_HOST TARES_MCP_PORT
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
nomcp="http://127.0.0.1:$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')/mcp"
(cd "$T/shop/src" && TARES_FACTORY_MCP_URL=$nomcp "$F" spec "an order form" >"$T/nomcp.out" 2>&1)
check "without an MCP server the spec says how to start one" 'grep -q "no MCP server answers at $nomcp" "$T/nomcp.out" && grep -q "tares mcp --transport streamable-http" "$T/nomcp.out" && [ ! -f "$FAKE_CLAUDE_STATE/calls.jsonl" ]'
(cd "$T/shop/src" && "$F" spec "an order form for the bakery shop" >"$T/spec.out" 2>&1)
check "the spec runs in the repo root" '[ "$(last | jq -r .cwd)" = "$T/shop" ]'
check "the session is named after the idea" '[ "$(last | jq -r ".args[1]")" = an-order-form-for-spec ]'
check "it loads this plugin" '[ "$(last | jq -r ".args | index(\"--plugin-dir\") as \$i | .[\$i+1]")" = "$plugin" ]'
check "it runs /tares:spec with the idea" '[ "$(last | jq -r ".args[-1]")" = "/tares:spec an order form for the bakery shop" ]'
check "the spec session turns the plugin on too" 'jq -e ".enabledPlugins[\"tares@inline\"] == true" "$(last | jq -r ".args | index(\"--settings\") as \$i | .[\$i+1]")" >/dev/null'
check "it is the person's own session, not a background one" '! last | jq -e ".args | index(\"--bg\")" >/dev/null'
check "it says the plan goes to Tares" 'grep -q "into a project on Tares ($TARESD_URL)" "$T/spec.out"'
check "the spec session is told the shared MCP endpoint" 'grep -qx "TARES_FACTORY_MCP_URL=${TARES_FACTORY_MCP_URL:-http://127.0.0.1:8788/mcp}" "$FAKE_CLAUDE_STATE/last.env"'
check "without a token there is no token file to name" '! grep -q "^TARES_FACTORY_CRED_FILE=" "$FAKE_CLAUDE_STATE/last.env"'
check "nothing is written into the repo" '[ -z "$(git -C "$T/shop" status --porcelain)" ] && [ ! -e "$T/shop/.factory" ]'

(cd "$T/shop" && "$F" spec --project "Bakery orders" >/dev/null 2>&1)
check "a project name names the session and goes to /tares:spec" '[ "$(last | jq -r ".args[1]")" = bakery-orders-spec ] && [ "$(last | jq -r ".args[-1]")" = "/tares:spec (project name: Bakery orders)" ]'
(cd "$T/shop" && TARES_AUTH_TOKEN=spec-tok "$F" spec "with a token" >/dev/null 2>&1)
check "with a token the spec session gets the token file under both names" 'grep -qx "TARES_FACTORY_CRED_FILE=$FACTORY_CREW_HOME/.tares-token" "$FAKE_CLAUDE_STATE/last.env" && grep -qx "TARES_FACTORY_TOKEN_FILE=$FACTORY_CREW_HOME/.tares-token" "$FAKE_CLAUDE_STATE/last.env"'

# A folder that holds the project's repos: the spec runs there and sees them all.
W="$T/work"; mkrepo "$W/api"; mkrepo "$W/web"; mkdir -p "$W/notes" "$W/zz-not-a-repo"
(cd "$W" && "$F" spec "orders across api and web" >"$T/multi.out" 2>&1)
check "a folder of repos: the spec runs in the folder" '[ "$(last | jq -r .cwd)" = "$W" ]'
check "and names the repos it holds" 'grep -q "holds 2 repo(s): api, web" "$T/multi.out"'

mkdir -p "$T/empty"
(cd "$T/empty" && "$F" spec "nothing here" >"$T/empty.out" 2>&1); rc=$?
check "a folder with no repo is refused plainly" '[ $rc != 0 ] && grep -q "inside the git repo the project builds on, or in a folder that holds its repos" "$T/empty.out"'
mkdir -p "$T/fresh" && git -C "$T/fresh" init -q
(cd "$T/fresh" && "$F" spec "x" >"$T/fresh.out" 2>&1); rc=$?
check "a repo with no commit is refused plainly" '[ $rc != 0 ] && grep -q "has no commit yet" "$T/fresh.out"'

# A project that already exists, led by someone else: a contributor session. (Auth off: the one person
# is `local`, who leads what they make, so this is the lead's own flow, unchanged.)
api POST /api/projects '{"template": "custom", "kind": "software_factory", "name": "Lead case", "objects": []}' >/dev/null
(cd "$T/shop/src" && "$F" spec --project "Lead case" >"$T/leadcase.out" 2>&1)
check "the lead of an existing project gets the usual prompt" '[ "$(last | jq -r ".args[-1]")" = "/tares:spec (project name: Lead case)" ] && [ "$(last | jq -r .cwd)" = "$T/shop" ] && ! grep -q "^TARES_PROJECT=" "$FAKE_CLAUDE_STATE/last.env" && ! grep -q contributor "$T/leadcase.out"'

# Auth on, three people with a key each: Ashish leads "Acme web", Kiran does not, the root token is no person.
start_tares "$T/tares2" root-tok
root_tok=$TARES_AUTH_TOKEN
for who in Ashish Kiran; do
  low=$(tr '[:upper:]' '[:lower:]' <<<"$who")
  r=$(api POST /api/keys "$(jq -cn --arg n "$who" --arg e "$low@example.com" '{name: ($n + "-laptop"), scopes: ["admin"], person: {email: $e, name: $n}}')")
  eval "${low}_tok=$(jq -r .secret <<<"$r")"
done
mkproj() { # token name
  TARES_AUTH_TOKEN=$1 api POST /api/projects "$(jq -cn --arg n "$2" '{template: "custom", kind: "software_factory", name: $n, objects: []}')" >/dev/null
}
mkproj "$ashish_tok" "Acme web"
mkproj "$root_tok" "Nobody leads"
check "fixture: Ashish leads Acme web, Nobody leads has no lead" '[ "$(api GET /api/projects | jq -r ".projects[] | select(.name == \"Acme web\") | .lead.name")" = Ashish ] && [ "$(api GET /api/projects | jq -r ".projects[] | select(.name == \"Nobody leads\") | .lead // \"none\"")" = none ]'
mkdir -p "$T/plain"
want="You are contributing to Acme web led by Ashish. Load the spec-project skill, section Contributor."

(cd "$T/plain" && TARES_AUTH_TOKEN=$kiran_tok "$F" spec --project "Acme web" >"$T/contrib.out" 2>&1); rc=$?
check "a contributor gets the contributor prompt, naming the project and its lead" '[ $rc = 0 ] && [ "$(last | jq -r ".args[-1]")" = "$want" ] && last | jq -r ".args[-1]" | grep -q "contributing to"'
check "the contributor session is bound to the project" 'grep -qx "TARES_PROJECT=Acme web" "$FAKE_CLAUDE_STATE/last.env"'
check "a contributor needs no git repo: it opens in the current folder, named for the project" '[ "$(last | jq -r .cwd)" = "$T/plain" ] && [ "$(last | jq -r ".args[1]")" = acme-web-spec ]'
check "the contributor session carries the contributor marker, so its hook does not say to build" 'grep -qx "TARES_SESSION_KIND=contributor" "$FAKE_CLAUDE_STATE/last.env" && ! grep -q "^FACTORY_ROLE=" "$FAKE_CLAUDE_STATE/last.env"'
check "a contributor is told it is a contributor session" 'grep -q "as a contributor to Acme web, which Ashish leads" "$T/contrib.out"'
check "a contributor session does not run /tares:spec" '! last | jq -r ".args[-1]" | grep -q "/tares:spec"'
check "it loads this plugin and turns it on" '[ "$(last | jq -r ".args | index(\"--plugin-dir\") as \$i | .[\$i+1]")" = "$plugin" ] && jq -e ".enabledPlugins[\"tares@inline\"] == true" "$(last | jq -r ".args | index(\"--settings\") as \$i | .[\$i+1]")" >/dev/null'
(cd "$T/shop/src" && TARES_AUTH_TOKEN=$kiran_tok "$F" spec "acme web" >/dev/null 2>&1)
check "the idea text finds the project too, and a contributor inside a repo opens at its root" '[ "$(last | jq -r ".args[-1]")" = "$want" ] && [ "$(last | jq -r .cwd)" = "$T/shop" ]'
check "a contributor session writes nothing into the repo" '[ -z "$(git -C "$T/shop" status --porcelain)" ]'

(cd "$T/shop/src" && TARES_AUTH_TOKEN=$ashish_tok "$F" spec --project "Acme web" >"$T/lead.out" 2>&1)
check "the lead's session has no contributor marker" '! grep -q "^TARES_SESSION_KIND=" "$FAKE_CLAUDE_STATE/last.env"'
check "the lead keeps today's prompt" '[ "$(last | jq -r ".args[-1]")" = "/tares:spec (project name: Acme web)" ] && [ "$(last | jq -r .cwd)" = "$T/shop" ] && ! grep -q "^TARES_PROJECT=" "$FAKE_CLAUDE_STATE/last.env" && ! grep -q contributor "$T/lead.out"'
(cd "$T/plain" && TARES_AUTH_TOKEN=$ashish_tok "$F" spec --project "Acme web" >"$T/leadplain.out" 2>&1); rc=$?
check "the lead outside a repo is still refused as today" '[ $rc != 0 ] && grep -q "inside the git repo the project builds on" "$T/leadplain.out"'

(cd "$T/shop/src" && TARES_AUTH_TOKEN=$kiran_tok "$F" spec --project "No such project" >/dev/null 2>&1)
check "an unknown project name stays today's flow" '[ "$(last | jq -r ".args[-1]")" = "/tares:spec (project name: No such project)" ] && ! grep -q "^TARES_PROJECT=" "$FAKE_CLAUDE_STATE/last.env"'
(cd "$T/shop/src" && TARES_AUTH_TOKEN=$kiran_tok "$F" spec --project "Nobody leads" >/dev/null 2>&1)
check "a project with no lead stays today's flow" '[ "$(last | jq -r ".args[-1]")" = "/tares:spec (project name: Nobody leads)" ] && ! grep -q "^TARES_PROJECT=" "$FAKE_CLAUDE_STATE/last.env"'
(cd "$T/shop/src" && TARES_AUTH_TOKEN=$root_tok "$F" spec --project "Acme web" >/dev/null 2>&1)
check "a caller with no person (the root token) stays today's flow" '[ "$(last | jq -r ".args[-1]")" = "/tares:spec (project name: Acme web)" ] && ! grep -q "^TARES_PROJECT=" "$FAKE_CLAUDE_STATE/last.env"'

# Only a software factory project, and only one the caller can see, makes a contributor session.
TARES_AUTH_TOKEN=$ashish_tok api POST /api/projects '{"template": "custom", "name": "Plain notes", "objects": []}' >/dev/null
mkproj "$ashish_tok" "Secret plan"
hid=$(api GET /api/projects | jq -r '.projects[] | select(.name == "Secret plan") | .id')
TARES_AUTH_TOKEN=$ashish_tok api PATCH "/api/projects/$hid" '{"scope": "private"}' >/dev/null
check "fixture: Plain notes is led by Ashish and is not a software factory project" '[ "$(api GET /api/projects | jq -r ".projects[] | select(.name == \"Plain notes\") | .lead.name + \"/\" + (.kind // \"none\")" | cut -d/ -f1)" = Ashish ] && [ "$(api GET /api/projects | jq -r ".projects[] | select(.name == \"Plain notes\") | .kind")" != software_factory ]'
check "fixture: Kiran's list shows Ashish's private project only as a hidden row with a lead and no kind" '[ "$(TARES_AUTH_TOKEN=$kiran_tok api GET /api/projects | jq -r ".projects[] | select(.name == \"Secret plan\") | [.hidden, .lead.name, (.kind // \"none\")] | join(\",\")")" = true,Ashish,none ] && [ "$(TARES_AUTH_TOKEN=$ashish_tok api GET /api/projects | jq -r ".projects[] | select(.name == \"Secret plan\") | .kind")" = software_factory ]'
(cd "$T/shop/src" && TARES_AUTH_TOKEN=$kiran_tok "$F" spec --project "Plain notes" >"$T/plainnotes.out" 2>&1)
check "a project that is not a software factory one, led by someone else, keeps the lead's flow" '[ "$(last | jq -r ".args[-1]")" = "/tares:spec (project name: Plain notes)" ] && ! grep -q "^TARES_PROJECT=" "$FAKE_CLAUDE_STATE/last.env" && ! grep -q "^TARES_SESSION_KIND=" "$FAKE_CLAUDE_STATE/last.env" && ! grep -q contributor "$T/plainnotes.out"'
(cd "$T/shop/src" && TARES_AUTH_TOKEN=$kiran_tok "$F" spec --project "Secret plan" >"$T/secret.out" 2>&1)
check "a private project the caller is not on keeps the lead's flow" '[ "$(last | jq -r ".args[-1]")" = "/tares:spec (project name: Secret plan)" ] && ! grep -q "^TARES_PROJECT=" "$FAKE_CLAUDE_STATE/last.env" && ! grep -q "^TARES_SESSION_KIND=" "$FAKE_CLAUDE_STATE/last.env" && ! grep -q contributor "$T/secret.out"'

# The session-start hook of a contributor session says to follow the Contributor section, never the
# `session` skill (which would have it build the project); a plain bound session still says `session`.
hook="$here/../../claude-plugin/hooks/session-start.sh"
hookctx() { # source, then VAR=value... for the hook
  local src=$1; shift
  echo "{\"source\": \"$src\"}" | env -u FACTORY_ROLE -u TARES_PROJECT -u TARES_SESSION_KIND -u TARES_SESSION_TAKEOVER "$@" bash "$hook" | jq -r '.hookSpecificOutput.additionalContext'
}
for src in startup compact resume; do
  ctx=$(hookctx $src TARES_PROJECT="Acme web" TARES_SESSION_KIND=contributor)
  check "contributor hook ($src): spec-project, section Contributor, no building" 'grep -q "contributes to the Tares project .Acme web. led by someone else" <<<"$ctx" && grep -q "spec-project. skill, section Contributor" <<<"$ctx" && grep -q "Do not build tickets" <<<"$ctx" && ! grep -q "\`session\` skill" <<<"$ctx" && ! grep -q "re-read the project.s tickets" <<<"$ctx"'
done
ctx=$(hookctx startup TARES_PROJECT="Acme web" TARES_SESSION_KIND=contributor)
check "contributor hook: no resume text on a fresh start" '! grep -q "resumed or compacted" <<<"$ctx"'
ctx=$(hookctx compact TARES_PROJECT="Acme web" TARES_SESSION_KIND=contributor)
check "contributor hook: after compaction it says to load the skill again" 'grep -q "resumed or compacted: load the .spec-project. skill again, section Contributor" <<<"$ctx"'
for src in startup compact; do
  ctx=$(hookctx $src TARES_PROJECT="Acme web")
  check "a plain bound session ($src) still follows the session skill" 'grep -q "Follow the .session. skill\|load the .session. skill again" <<<"$ctx" && ! grep -q "Contributor" <<<"$ctx"'
done
check "a marker without a project: the hook is silent" '[ -z "$(echo "{\"source\": \"startup\"}" | env -u FACTORY_ROLE -u TARES_PROJECT TARES_SESSION_KIND=contributor bash "$hook")" ]'
skill="$here/../../claude-plugin/skills/spec-project/SKILL.md"
check "the Contributor section asks my_work for this project's questions (Q numbers restart per project)" 'sed -n "/^## Contributor/,/^## Light mode/p" "$skill" | tr -s " \n" " " | grep -q "Call .my_work(project=<project>). and read .person. (who this is) and .spec_questions." && sed -n "/^## Contributor/,/^## Light mode/p" "$skill" | tr -s " \n" " " | grep -q "restart in each project"'

# A lookup that fails must not block the normal flow: a curl that fails for one route.
mkdir -p "$T/shim"
real_curl=$(command -v curl)
cat > "$T/shim/curl" <<SHIM
#!/usr/bin/env bash
for a in "\$@"; do case "\$a" in *"\$SHIM_FAIL") exit 22 ;; esac; done
exec "$real_curl" "\$@"
SHIM
chmod +x "$T/shim/curl"
for route in /api/whoami /api/projects; do
  (cd "$T/shop/src" && PATH="$T/shim:$PATH" SHIM_FAIL=$route TARES_AUTH_TOKEN=$kiran_tok "$F" spec --project "Acme web" >/dev/null 2>&1)
  check "a failed $route lookup stays today's flow" '[ "$(last | jq -r ".args[-1]")" = "/tares:spec (project name: Acme web)" ] && ! grep -q "^TARES_PROJECT=" "$FAKE_CLAUDE_STATE/last.env"'
done
(cd "$T/plain" && PATH="$T/shim:$PATH" SHIM_FAIL=/api/whoami TARES_AUTH_TOKEN=$kiran_tok "$F" spec --project "Acme web" >"$T/failplain.out" 2>&1); rc=$?
check "and a failed lookup outside a repo is refused as today, not opened as a contributor" '[ $rc != 0 ] && grep -q "inside the git repo the project builds on" "$T/failplain.out"'

[ $fail = 0 ] && echo "spec: all cases pass"
exit $fail
