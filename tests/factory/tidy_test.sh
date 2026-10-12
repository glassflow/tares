#!/usr/bin/env bash
# Checks factory tidy: it removes worktrees, scratch folders, files and links inside the crew's own
# folders, never follows a link out, and refuses anything else. Every station may run it.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib.sh"
F="$here/../../claude-plugin/bin/factory"
T=$(cd "$(mktemp -d)" && pwd -P)
export HOME="$T/home" FACTORY_CREW_HOME="$T/crew" FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
unset TARES_URL TARES_AUTH_TOKEN TARES_FACTORY_URL TARES_FACTORY_TOKEN_FILE FACTORY_REPO FACTORY_WORKTREES CLAUDE_JOB_DIR
mkdir -p "$HOME" "$FACTORY_CREW_HOME/worktrees" "$FACTORY_CREW_HOME/tmp"
chmod +x "$CLAUDE_BIN"
trap 'stop_tares; rm -rf "$T"' EXIT
start_tares "$T/tares"
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; fi; }

# A project repo, and something outside every crew folder that must survive.
R="$T/repo"; git init -q --bare "$T/origin.git"; git clone -q "$T/origin.git" "$R" 2>/dev/null
echo a > "$R/tracked.txt" && git -C "$R" add . && git -C "$R" commit -q -m init && git -C "$R" push -q origin HEAD 2>/dev/null
O="$T/outside"; mkdir -p "$O/node_modules" && echo keep > "$O/node_modules/keep" && echo keep > "$O/file"
hand_over p "$R" >/dev/null   # the project's repos are where its worktrees may be tidied

# A review worktree with a node_modules link to the real checkout and an untracked file.
W="$FACTORY_CREW_HOME/worktrees/p-pr1"
git -C "$R" worktree add -q --detach "$W" HEAD && ln -s "$O/node_modules" "$W/node_modules" && echo x > "$W/scratch.txt"
"$F" tidy "$W" >/dev/null 2>&1
check "a review worktree with scratch files needs --force" '[ -d "$W" ]'
"$F" tidy --force "$W" >/dev/null 2>&1
check "a review worktree is removed with --force" '[ ! -e "$W" ] && ! git -C "$R" worktree list | grep -q p-pr1'
check "its node_modules link was not followed" '[ -f "$O/node_modules/keep" ]'

# Another station's worktree with work in it is refused unless --force.
B="$R/.factory/worktrees/T2"; git -C "$R" worktree add -q --detach "$B" HEAD && echo changed > "$B/tracked.txt"
"$F" tidy "$B" >"$T/b.txt" 2>&1; rc=$?
check "a worktree with uncommitted changes is refused" '[ $rc != 0 ] && [ -d "$B" ] && grep -q -- "--force" "$T/b.txt"'
git -C "$B" checkout -q -- tracked.txt && git -C "$B" commit -q --allow-empty -m wip
"$F" tidy "$B" >/dev/null 2>&1
check "a worktree with unpushed commits is refused" '[ -d "$B" ]'
"$F" tidy --force "$B" >/dev/null 2>&1
check "--force removes it" '[ ! -e "$B" ]'
B2="$R/.factory/worktrees/T3"; git -C "$R" worktree add -q --detach "$B2" HEAD && echo new > "$B2/new-file.txt"
"$F" tidy "$B2" >/dev/null 2>&1
check "a worktree with new, never-added files is refused" '[ -f "$B2/new-file.txt" ]'
"$F" tidy --force "$B2" >/dev/null 2>&1

# A link on its own: the link goes, what it points at stays.
ln -s "$O" "$FACTORY_CREW_HOME/tmp/link"
"$F" tidy "$FACTORY_CREW_HOME/tmp/link" >/dev/null 2>&1
check "a link is removed, not followed" '[ ! -L "$FACTORY_CREW_HOME/tmp/link" ] && [ -f "$O/file" ]'
mkdir -p "$FACTORY_CREW_HOME/tmp/inner" && echo keep > "$FACTORY_CREW_HOME/tmp/inner/keep" && ln -s "$FACTORY_CREW_HOME/tmp/inner" "$FACTORY_CREW_HOME/tmp/lg"
"$F" tidy "$FACTORY_CREW_HOME/tmp/lg/" >/dev/null 2>&1
check "a link given with a trailing slash goes, its target stays" '[ ! -L "$FACTORY_CREW_HOME/tmp/lg" ] && [ -f "$FACTORY_CREW_HOME/tmp/inner/keep" ]'

# Scratch folders and files in the crew's tmp, a builder's worktrees folder and the job's scratch.
mkdir -p "$FACTORY_CREW_HOME/tmp/s/deep" && echo x > "$FACTORY_CREW_HOME/tmp/f.txt"
"$F" tidy "$FACTORY_CREW_HOME/tmp/s" "$FACTORY_CREW_HOME/tmp/f.txt" >/dev/null 2>&1
check "a scratch folder and a file are removed" '[ ! -e "$FACTORY_CREW_HOME/tmp/s" ] && [ ! -e "$FACTORY_CREW_HOME/tmp/f.txt" ]'
mkdir -p "$R/.factory/worktrees/T1/x"
"$F" tidy "$R/.factory/worktrees/T1" >/dev/null 2>&1
check "a project's .factory/worktrees is inside" '[ ! -e "$R/.factory/worktrees/T1" ]'
J="$T/job"; mkdir -p "$J/tmp/v1"
CLAUDE_JOB_DIR="$J" "$F" tidy "$J/tmp/v1" >/dev/null 2>&1
check "the job's own scratch folder is inside" '[ ! -e "$J/tmp/v1" ]'
mkdir -p "$J/tmp/v2"
"$F" tidy "$J/tmp/v2" >/dev/null 2>&1
check "another job's scratch folder is not" '[ -d "$J/tmp/v2" ]'

# Refusals: outside, a root itself, an escape through .., and a link pointing out given by its target.
"$F" tidy "$O" >"$T/out.txt" 2>&1; rc=$?
check "outside the crew's folders is refused" '[ $rc != 0 ] && [ -d "$O" ] && grep -q "outside the crew" "$T/out.txt"'
"$F" tidy "$FACTORY_CREW_HOME/worktrees" >/dev/null 2>&1
check "a root itself is refused" '[ -d "$FACTORY_CREW_HOME/worktrees" ]'
mkdir -p "$FACTORY_CREW_HOME/worktrees/a"
"$F" tidy "$FACTORY_CREW_HOME/worktrees/a/.." "$FACTORY_CREW_HOME/worktrees/a/../../crew-x" >/dev/null 2>&1
check "an escape through .. is refused" '[ -d "$FACTORY_CREW_HOME/worktrees/a" ]'
"$F" tidy "$HOME" >/dev/null 2>&1
check "the home folder is refused" '[ -d "$HOME" ]'
ln -s "$O" "$T/linkroot"
FACTORY_WORKTREES="$T/linkroot" "$F" tidy "$T/linkroot/file" >/dev/null 2>&1
check "a root that is a link is not a root" '[ -f "$O/file" ]'
FACTORY_WORKTREES="$HOME" "$F" tidy "$HOME/.claude" >/dev/null 2>&1; mkdir -p "$HOME/x"
FACTORY_WORKTREES="$HOME" "$F" tidy "$HOME/x" >/dev/null 2>&1
check "the home folder is never a root" '[ -d "$HOME/x" ]'
"$F" tidy "$T/gone" >"$T/gone.txt" 2>&1; rc=$?
check "something already gone is fine" '[ $rc = 0 ] && grep -q "already gone" "$T/gone.txt"'

# Every station may run it without a prompt.
cp "$here/../../claude-plugin/templates/crew.json" "$FACTORY_CREW_HOME/crew.json"
api PUT /api/crew/settings '{"autonomy": "L4-ship", "releaser": true, "release_profile": "kind", "prod_pattern": "prod", "challenger": false, "builders_max": 2}' >/dev/null
for s in crew-orchestrator crew-reviewer crew-releaser; do
  "$F" settings "$s" >"$T/$s.json" 2>/dev/null
  check "$s may run factory tidy" 'jq -e ".permissions.allow | index(\"Bash(factory tidy *)\")" "$T/$s.json" >/dev/null'
done

[ $fail = 0 ] && echo "tidy: all cases pass"
exit $fail
