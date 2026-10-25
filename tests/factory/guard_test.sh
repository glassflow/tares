#!/usr/bin/env bash
# Checks the PreToolUse guard: each rule fires for the wrong station and lets the owner through.
set -uo pipefail
G="$(cd "$(dirname "$0")/../.." && pwd)/claude-plugin/hooks/guard.sh"
export FACTORY_HOME=/nonexistent FACTORY_STATION=test
fail=0
t() { # role tool command expected
  local out got=allow
  out=$(jq -n --arg t "$2" --arg c "$3" '{tool_name: $t, tool_input: {command: $c}}' | FACTORY_ROLE=$1 bash "$G")
  grep -q '"deny"' <<<"$out" && got=deny
  if [ "$got" != "$4" ]; then echo "FAIL $1 | $3 | expected $4, got $got"; fail=1; fi
}
t builder Bash 'gh pr merge 12 --squash --delete-branch' allow
t reviewer Bash 'gh pr merge 12 --squash' deny
t builder Bash 'gh pr review 12 --approve' deny
t reviewer Bash 'gh pr review 12 --comment -b hi' deny
t reviewer Bash 'gh api repos/o/r/statuses/abc -f state=success -f context=factory/review' allow
t builder Bash 'gh api repos/o/r/statuses/abc -f state=success' deny
t builder Bash 'gh api repos/o/r/commits/abc/status' allow
t releaser Bash 'git tag v1.2.0 && git push origin v1.2.0' allow
t builder Bash 'git tag v1.2.0' deny
t builder Bash 'git push -u origin factory/T1' allow
t builder Bash 'kubectl apply -k deploy/' deny
t builder Bash 'kubectl --context kind-factory -n app apply -f x.yaml' deny
t releaser Bash 'kubectl --context kind-factory rollout undo deploy/app' allow
t builder Bash 'kubectl get pods -n app' allow
t builder Bash 'kubectl logs deploy/app --tail 50' allow
t builder Bash 'helm upgrade --install app ./chart' deny
t builder Bash 'helm template ./chart' allow
t releaser Bash 'helm -n app upgrade app ./chart' allow
t builder Bash 'docker build -t app .' deny
t releaser Bash 'cd app && docker build -t app:1 .' deny
t builder Bash 'factory-heavy 900 docker build -t app .' allow
t builder Bash 'echo "docker build is heavy" > notes.md' allow
t builder Bash 'factory spawn builder x "y"' deny
t orchestrator Bash 'factory spawn builder m1 "do T1"' allow
t builder Bash 'factory roster' allow
t orchestrator Bash 'factory spawn builder shop m1-api glassflow/api "[TF:TASK] T1. Merge with gh pr merge --squash once factory/review is success"' allow
t orchestrator Bash 'factory revive shop-build-m1 "carry on; the gate is gh pr merge after the verdict"' allow
t orchestrator Bash 'factory spawn builder shop m1-api glassflow/api "$(gh pr merge 1)"' deny
t builder Bash 'factory spawn builder shop m1 glassflow/api "T1"' deny
t reviewer mcp__plugin_slack_slack__slack_send_message '' deny   # no station messages people
t builder Bash 'factory restart crew-reviewer' deny
t orchestrator Bash 'factory finish shop' deny
t orchestrator Bash 'cd ~/factory && factory finish shop' deny
t orchestrator Bash 'factory restart crew-reviewer' allow
t builder mcp__plugin_slack_slack__slack_send_message '' deny
t builder mcp__plugin_slack_slack__slack_read_channel '' allow
t reviewer Bash 'git commit -m "fix the gh pr merge race and the docker build cache"' allow
t builder Bash 'gh pr create --title "Speed up docker build" --body "Releases: git tag v1.2 later, kubectl apply stays manual"' allow
t builder Bash "git commit -m 'gh pr merge was racing'" allow
t builder Bash 'git commit -m "x" && gh pr merge 3' allow
t reviewer Bash 'git commit -m "x" && gh pr merge 3' deny
t orchestrator mcp__plugin_slack_slack__slack_read_channel '' allow
t orchestrator mcp__plugin_tares_tares__desk_add '' allow
t orchestrator mcp__plugin_slack_slack__slack_send_message '' deny
t orchestrator Bash 'factory crew down' allow
t reviewer Bash 'factory crew halt "x"' deny
t builder Bash 'factory crew freeze' deny
t builder Bash 'factory crew unfreeze' deny
t orchestrator Bash 'factory crew freeze' allow
t orchestrator Bash 'factory crew unfreeze' allow
t builder Bash 'factory crew update' deny
t builder Bash 'tares factory crew update --wait 60' deny
t reviewer Bash 'factory crew restart' deny
t releaser Bash 'tares factory crew restart' deny
t orchestrator Bash 'factory crew update' allow
t orchestrator Bash 'tares factory crew restart' allow
# The CLI is matched by its name, whatever its path (an absolute path, a relative one, the plugin's bin).
t builder Bash '/opt/glass0/tares/claude-plugin/bin/factory crew freeze' deny
t orchestrator Bash '/opt/glass0/tares/claude-plugin/bin/factory crew freeze' allow
t builder Bash '/opt/glass0/tares/claude-plugin/bin/factory crew unfreeze' deny
t orchestrator Bash '/opt/glass0/tares/claude-plugin/bin/factory crew unfreeze' allow
t builder Bash '/opt/glass0/tares/claude-plugin/bin/factory crew up' deny
t orchestrator Bash '/opt/glass0/tares/claude-plugin/bin/factory crew up' allow
t builder Bash '/opt/glass0/tares/claude-plugin/bin/factory crew down' deny
t orchestrator Bash '/opt/glass0/tares/claude-plugin/bin/factory crew down' allow
t builder Bash '/opt/glass0/tares/claude-plugin/bin/factory crew halt' deny
t orchestrator Bash '/opt/glass0/tares/claude-plugin/bin/factory crew halt' allow
t builder Bash '/opt/glass0/tares/claude-plugin/bin/factory crew resume' deny
t orchestrator Bash '/opt/glass0/tares/claude-plugin/bin/factory crew resume' allow
t builder Bash '/opt/glass0/tares/claude-plugin/bin/factory crew update' deny
t orchestrator Bash '/opt/glass0/tares/claude-plugin/bin/factory crew update' allow
t builder Bash '/opt/glass0/tares/claude-plugin/bin/factory crew restart' deny
t orchestrator Bash '/opt/glass0/tares/claude-plugin/bin/factory crew restart' allow
t builder Bash '/opt/glass0/tares/claude-plugin/bin/factory spawn x' deny
t orchestrator Bash '/opt/glass0/tares/claude-plugin/bin/factory spawn x' allow
t builder Bash '/opt/glass0/tares/claude-plugin/bin/factory retire x' deny
t orchestrator Bash '/opt/glass0/tares/claude-plugin/bin/factory retire x' allow
t builder Bash '/opt/glass0/tares/claude-plugin/bin/factory revive x' deny
t orchestrator Bash '/opt/glass0/tares/claude-plugin/bin/factory revive x' allow
t builder Bash '/opt/glass0/tares/claude-plugin/bin/factory restart x' deny
t orchestrator Bash '/opt/glass0/tares/claude-plugin/bin/factory restart x' allow
t builder Bash '/opt/glass0/tares/claude-plugin/bin/factory down' deny
t orchestrator Bash '/opt/glass0/tares/claude-plugin/bin/factory down' allow
t builder Bash '/opt/glass0/tares/claude-plugin/bin/factory halt' deny
t orchestrator Bash '/opt/glass0/tares/claude-plugin/bin/factory halt' allow
t builder Bash './claude-plugin/bin/factory crew freeze' deny
t builder Bash 'cd x && /abs/bin/factory crew halt "y"' deny
t builder Bash '$HOME/x/factory crew down' deny
t builder Bash '/abs/bin/factory finish p' deny
t builder Bash 'factory finish p' deny
t builder Bash 'ls /opt/glass0/tares/.factory/worktrees && cat .factory/halt.md' allow
t builder Bash '/opt/glass0/tares/.factory/halt crew' allow
# A halted crew refuses a new session however the CLI is spelled.
H=$(mktemp -d); echo '{"reason":"test"}' > "$H/HALT"
for c in 'factory spawn x' '/abs/bin/factory spawn x' '/abs/bin/factory revive x'; do
  out=$(jq -n --arg c "$c" '{tool_name: "Bash", tool_input: {command: $c}}' | FACTORY_HOME=$H FACTORY_ROLE=orchestrator bash "$G")
  grep -q 'crew is halted' <<<"$out" || { echo "FAIL halted | $c | expected the halt to refuse it"; fail=1; }
done
rm -rf "$H"
# A quoted message is prose, but a command substituted inside it runs.
t reviewer Bash 'git commit -m "$(gh pr merge 1)"' deny
t reviewer Bash 'git commit -m "notes `gh pr merge 1`"' deny
t reviewer Bash 'git commit --message="x $(gh pr merge 1) y"' deny
t builder Bash 'gh pr create --title "t" --body "$(kubectl apply -f prod.yaml)"' deny
t builder Bash 'gh pr create -b "$(git tag v9 && git push origin v9)"' deny
t reviewer Bash 'bash -c "gh pr merge 1"' deny
t reviewer Bash "sh -c 'gh pr merge 1'" deny
t reviewer Bash 'eval "gh pr merge 1"' deny
t reviewer Bash $'bash <<EOF\ngh pr merge 1\nEOF' deny
t reviewer Bash $'git commit -m "$(cat <<\'EOF\'\nFix the gh pr merge race\n\nThe "docker build" cache too.\nEOF\n)"' allow
t reviewer Bash $'git commit -m "$(cat <<\'EOF\'\nx\nEOF\n) $(gh pr merge 1) $(cat <<\'EOF\'\ny\nEOF\n)"' deny
t reviewer Bash $'git commit -m "$(cat <<EOF\n$(gh pr merge 1)\nEOF\n)"' deny
t reviewer Bash 'gh api -X PUT repos/o/r/pulls/1/merge' deny
t builder Bash 'git push origin HEAD:refs/tags/v1' deny
t builder Bash 'git push origin HEAD:factory/T1' allow
# Heavy commands are checked per command in a chain.
t builder Bash 'factory-heavy 10 true; docker build -t x .' deny
t builder Bash $'factory-heavy 10 true\ndocker build -t x .' deny
t builder Bash 'factory-heavy 900 docker build . && docker build -t y .' deny
t builder Bash 'cd app && factory-heavy 900 docker build -t app .' allow
helper() { # parent command expected: a helper works under its parent's role
  local out got=allow
  out=$(jq -n --arg c "$2" '{tool_name: "Bash", tool_input: {command: $c}}' | FACTORY_ROLE=helper FACTORY_PARENT=$1 bash "$G")
  grep -q '"deny"' <<<"$out" && got=deny
  if [ "$got" != "$3" ]; then echo "FAIL helper of $1 | $2 | expected $3, got $got"; fail=1; fi
}
helper crew-releaser 'kubectl --context kind-factory rollout undo deploy/app' allow
helper crew-releaser 'gh pr merge 3' deny
helper shop-build-m1 'gh pr merge 3 --squash' allow
helper shop-build-m1 'kubectl apply -k deploy/' deny
out=$(jq -n '{tool_name: "Bash", tool_input: {command: "gh pr merge 1"}}' | env -u FACTORY_ROLE bash "$G")
[ -z "$out" ] || { echo "FAIL a session without FACTORY_ROLE was touched"; fail=1; }
[ $fail = 0 ] && echo "guard: all cases pass"
exit $fail
