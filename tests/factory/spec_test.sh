#!/usr/bin/env bash
# Checks that factory spec gives each project its own git worktree: two projects in one repo keep
# separate plans, a project can be picked up again, --here plans in place, and a project worktree
# counts as trusted when its main repo is. Uses a fake claude.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
F="$here/../plugin/bin/factory"
T=$(cd "$(mktemp -d)" && pwd -P)
export HOME="$T/home" FACTORY_CREW_HOME="$T/crew" FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
unset TARES_URL TARES_AUTH_TOKEN TARES_PLUGIN_DIR FACTORY_REPO
mkdir -p "$HOME"
chmod +x "$CLAUDE_BIN"
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; fi; }
last_call() { tail -1 "$FAKE_CLAUDE_STATE/calls.jsonl"; }

# A repo with an origin whose default branch is main.
git init -q --bare "$T/origin.git"
git clone -q "$T/origin.git" "$T/shop" 2>/dev/null
( cd "$T/shop" && git checkout -q -b main && echo hi > README.md && git add . && git commit -qm init && git push -q -u origin main && git remote set-head origin main ) >/dev/null 2>&1

WA="$FACTORY_CREW_HOME/projects/feat-a/shop"
( cd "$T/shop" && "$F" spec --project feat-a "add search" ) >"$T/a.out" 2>&1
check "feat-a gets its own worktree" '[ -f "$WA/.factory/factory.json" ]'
check "feat-a worktree is a git worktree of shop" '[ "$(git -C "$WA" rev-parse --path-format=absolute --git-common-dir)" = "$T/shop/.git" ]'
check "feat-a project name" '[ "$(jq -r .project "$WA/.factory/factory.json")" = feat-a ]'
check "feat-a starts from origin/main" '[ "$(jq -r .worktree.base "$WA/.factory/factory.json")" = origin/main ]'
check "spec opens Claude in the feat-a worktree" '[ "$(last_call | jq -r .cwd)" = "$WA" ]'
check "spec session is named feat-a-spec" 'last_call | jq -e ".args | index(\"-n\") as \$i | .[\$i+1] == \"feat-a-spec\"" >/dev/null'
check "spec gets the idea" 'last_call | jq -e ".args[-1] == \"/tares-factory:spec add search\"" >/dev/null'
check "the main checkout has no plan" '[ ! -e "$T/shop/.factory" ]'
echo "plan A" > "$WA/.factory/docs/plan.md"

( cd "$T/shop" && "$F" spec "Feature B shows the price history" ) >"$T/b.out" 2>&1
WB="$FACTORY_CREW_HOME/projects/feature-b-shows-the/shop"
check "a second project in the same repo gets a second worktree" '[ -f "$WB/.factory/factory.json" ]'
check "the two plans are separate" '[ "$(cat "$WA/.factory/docs/plan.md")" = "plan A" ] && ! grep -q "plan A" "$WB/.factory/docs/plan.md"'

# An idea over several lines still gives a one-line name from its first words.
( cd "$T/shop" && "$F" spec "Price alerts
1. email when a price drops
2. a weekly digest" ) >"$T/ml.out" 2>&1
check "a multi-line idea gives a one-line name" '[ -f "$FACTORY_CREW_HOME/projects/price-alerts-1-email/shop/.factory/factory.json" ]'
check "no folder with a line break in its name" '[ -z "$(find "$FACTORY_CREW_HOME/projects" -maxdepth 1 -name "*
*")" ]'

# From a folder of repos (here a workspace root that is itself a repo with no commits, like /opt/glass0):
# the person picks the plan's repo at a terminal, even when there is only one; without a terminal the
# command names them and asks for --repo, and makes nothing. The folder is kept for the hand-over.
W="$T/workspace"; git init -q "$W"
git clone -q "$T/origin.git" "$W/api" 2>/dev/null
( cd "$W" && "$F" spec --project one-repo "x" </dev/null ) >"$T/ws1.out" 2>&1
check "one repo and no terminal: it is named, nothing is made" 'grep -q "holds 1 repo(s) (api); name the one that holds the plan: factory spec --repo" "$T/ws1.out" && [ ! -e "$FACTORY_CREW_HOME/projects/one-repo" ]'
( cd "$W" && "$F" spec --project one-repo --repo api "x" </dev/null ) >/dev/null 2>&1
check "--repo plans in it" '[ "$(jq -r .worktree.of "$FACTORY_CREW_HOME/projects/one-repo/api/.factory/factory.json")" = "$W/api" ]'
check "and keeps the folder for the hand-over" '[ "$(jq -r .workspace "$FACTORY_CREW_HOME/projects/one-repo/api/.factory/factory.json")" = "$W" ]'
git clone -q "$T/origin.git" "$W/web" 2>/dev/null; git clone -q "$T/origin.git" "$W/my api" 2>/dev/null
mkdir -p "$W/notes"; git init -q "$W/fresh"
( cd "$W" && "$F" spec --project two-repos "x" </dev/null ) >"$T/ws2.out" 2>&1
check "several repos: each named once, a repo with no commits left out" 'grep -q "holds 3 repo(s) (api, my api, web); name the one" "$T/ws2.out" && [ ! -e "$FACTORY_CREW_HOME/projects/two-repos" ]'
( cd "$W" && "$F" spec --project two-repos --repo web "x" </dev/null ) >"$T/ws3.out" 2>&1
check "--repo picks the plan's repo" '[ "$(jq -r .worktree.of "$FACTORY_CREW_HOME/projects/two-repos/web/.factory/factory.json")" = "$W/web" ]'
check "and the spec opens there" '[ "$(last_call | jq -r .cwd)" = "$FACTORY_CREW_HOME/projects/two-repos/web" ]'
( cd "$W" && "$F" spec --project two-repos --repo api "x" </dev/null ) >"$T/ws6.out" 2>&1
check "--repo that disagrees with the project's repo is refused" 'grep -q "already plans in" "$T/ws6.out"'
out5="$T/ws5.out"; : >"$out5"
( cd "$W" && { for _ in $(seq 200); do grep -q "repo \[1-" "$out5" && break; sleep 0.05; done; printf '2\n'; } \
  | script -q /dev/null "$F" spec --project picked "x" ) >"$out5" 2>&1
check "at a terminal it asks, and takes the answer" 'grep -q "Which one holds the plan" "$out5" && grep -q "  2) my api" "$out5" && [ "$(jq -r .worktree.of "$FACTORY_CREW_HOME/projects/picked/my api/.factory/factory.json")" = "$W/my api" ]'
mkdir -p "$T/bare-folder"
( cd "$T/bare-folder" && "$F" spec --project nothing-here "x" </dev/null ) >"$T/ws7.out" 2>&1
check "an empty folder is refused plainly" 'grep -q "run factory spec inside the git repository" "$T/ws7.out" && ! grep -q "cd:" "$T/ws7.out"'
( cd "$T/shop" && "$F" spec --project no-folder "x" </dev/null ) >/dev/null 2>&1
check "inside a repo, no folder is kept" '[ "$(jq -r ".workspace // empty" "$FACTORY_CREW_HOME/projects/no-folder/shop/.factory/factory.json")" = "" ]'
( cd "$T" && "$F" spec --project nope --repo "$T/notes-x" "x" </dev/null ) >"$T/ws4.out" 2>&1
check "--repo that isn't a repo is refused" 'grep -q "is not a git repository" "$T/ws4.out"'

# A repo with no commits gets a plain refusal and leaves nothing behind.
git init -q "$T/empty"
( cd "$T/empty" && "$F" spec --project nothing-yet "x" ) >"$T/empty.out" 2>&1
check "a repo with no commits is refused plainly" 'grep -q "has no commit to start the project from" "$T/empty.out"'
check "the refusal leaves no project folder" '[ ! -e "$FACTORY_CREW_HOME/projects/nothing-yet" ]'

n_before=$(git -C "$T/shop" worktree list | wc -l)
( cd "$T" && "$F" spec --project feat-a ) >"$T/a2.out" 2>&1
check "--project carries on in the existing worktree" '[ "$(last_call | jq -r .cwd)" = "$WA" ] && [ "$(git -C "$T/shop" worktree list | wc -l)" = "$n_before" ]'
( cd "$WA" && "$F" spec ) >"$T/a3.out" 2>&1
check "spec inside a project worktree carries on there" '[ "$(last_call | jq -r .cwd)" = "$WA" ] && [ "$(git -C "$T/shop" worktree list | wc -l)" = "$n_before" ]'

( cd "$T/shop" && "$F" spec --here "small fix" ) >"$T/here.out" 2>&1
check "--here plans in the checkout itself" '[ -f "$T/shop/.factory/factory.json" ] && [ "$(last_call | jq -r .cwd)" = "$T/shop" ]'

"$F" projects >"$T/projects.out" 2>&1
check "projects lists feat-a" 'grep -q "^feat-a .*no .*$WA" "$T/projects.out"'
check "projects lists feature-b" 'grep -q "^feature-b-shows-the " "$T/projects.out"'

# Trust: only the main repo is trusted; doctor counts the project worktree as trusted.
jq -n --arg r "$T/shop" '{projects: {($r): {hasTrustDialogAccepted: true}}}' > "$HOME/.claude.json"
cp "$here/../plugin/templates/crew.json" "$FACTORY_CREW_HOME/crew.json"
jq -n --arg p feat-a --arg r "$WA" '{($p): {repo: $r, repos: [$r]}}' > "$FACTORY_CREW_HOME/projects.json"
"$F" doctor >"$T/doctor.out" 2>&1
check "doctor: a project worktree inherits its main repo's trust" 'grep -q "project feat-a: $WA trusted" "$T/doctor.out"'

[ $fail = 0 ] && echo "spec: all cases pass"
exit $fail
