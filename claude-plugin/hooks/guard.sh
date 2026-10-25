#!/usr/bin/env bash
# Enforces "one owner per action type" and "heavy commands one at a time" for factory stations.
# Sessions without FACTORY_ROLE (the human's own sessions) are never touched.
set -uo pipefail
[ -n "${FACTORY_ROLE:-}" ] || exit 0

input=$(cat)
tool=$(jq -r '.tool_name // ""' <<<"$input")
cmd=$(jq -r '.tool_input.command // ""' <<<"$input")
role=$FACTORY_ROLE
# A helper works under its parent station's role: crew-<role>, or a builder named <project>-build-<slug>.
if [ "$role" = helper ]; then
  case "${FACTORY_PARENT:-}" in crew-*) role=${FACTORY_PARENT#crew-} ;; *-build-*) role=builder ;; esac
fi

deny() {
  jq -n --arg r "$1" '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $r}}'
  if [ -n "${FACTORY_HOME:-}" ] && [ -d "$FACTORY_HOME" ]; then
    jq -cn --arg s "${FACTORY_STATION:-?}" --arg t "$tool" --arg r "$1" \
      '{at: (now | todate), station: $s, event: "guard-deny", tool: $t, reason: $r}' >> "$FACTORY_HOME/events.jsonl" 2>/dev/null || true
  fi
  exit 0
}

owner_only() { # $1 = owning role, $2 = action description
  [ "$role" = "$1" ] || deny "In this factory only the $1 station may $2. Send it a message instead (see the factory-protocol skill)."
}

case "$tool" in
  # Posting to people only; reading Slack, and every other tool, stays open to all stations.
  mcp__*slack*send*|mcp__*slack*schedule*|mcp__*slack*post*|mcp__*slack*update*message*)
    deny "Crew stations don't message people. The person reads the crew in the orchestrator's session and on Tares; send the orchestrator an ASK instead." ;;
esac

[ "$tool" = "Bash" ] || exit 0

# Text inside a quoted commit message, PR body, title or release notes, or a task handed to a station, is
# prose, not a command:
# blank it out before matching, so "fix the gh pr merge race" in a commit message doesn't trip a rule.
# A double-quoted message that substitutes a command ($(...) or backticks) runs that command, so it
# stays visible, except the "$(cat <<'EOF' ... EOF)" form, whose quoted delimiter keeps the body literal.
scan=$(perl -0777 -pe '
  my $flag = qr/(^|\s)(-m|--message|-b|--body|-t|--title|-n|--notes)(\s+|=)/;
  s{$flag"\$\(cat\s*<<-?\s*([\x27"])(\w+)\4[ \t]*\n(?:(?!\n\t*\5\n).)*?\n\t*\5\n\s*\)"}{$1$2 _}gs;
  s{$flag("(?:[^"\\]|\\.)*"|\x27[^\x27]*\x27)}{my ($all, $pre, $opt, $q) = ($&, $1, $2, $4); ($q =~ /^"/ && $q =~ /\$\(|`/) ? $all : "$pre$opt _"}ge;
  # The quoted task or prompt given to factory spawn, revive or restart is text for another session.
  s{(\bfactory\s+(?:spawn|revive|restart)\b)((?:[^\n;&|"\x27]|"(?:[^"\\]|\\.)*"|\x27[^\x27]*\x27)*)}{
    my ($c, $rest) = ($1, $2);
    $rest =~ s{("(?:[^"\\]|\\.)*"|\x27[^\x27]*\x27)}{my $q = $1; ($q =~ /^"/ && $q =~ /\$\(|`/) ? $q : "_"}ge;
    "$c$rest"
  }ge;
' <<<"$cmd")

# While the crew is halted (factory crew halt), nothing risky starts: merges, pushes, deploys, tags, sessions.
if [ -n "${FACTORY_HOME:-}" ] && [ -f "$FACTORY_HOME/HALT" ]; then
  if grep -Eq '(gh[[:space:]]+pr[[:space:]]+merge|git[[:space:]]+push|git[[:space:]]+tag[[:space:]]+[^-]|gh[[:space:]]+release[[:space:]]+create|kubectl([[:space:]]+[^[:space:];&|]+)*[[:space:]]+(apply|rollout|delete|scale|set|patch|replace|create)([[:space:]]|$)|helm([[:space:]]+[^[:space:];&|]+)*[[:space:]]+(install|upgrade|uninstall|rollback)([[:space:]]|$)|factory[[:space:]]+(spawn|revive)|claude[[:space:]]+(--bg|--background))' <<<"$scan"; then
    deny "The crew is halted ($(jq -r '.reason // "no reason given"' "$FACTORY_HOME/HALT" 2>/dev/null)). Finish your current step safely and start nothing new; \`factory crew resume\` lifts the halt."
  fi
fi

# Heavy commands must go through factory-heavy (one at a time, hard timeout), checked per command in
# a chain, so `factory-heavy 9 true; docker build .` doesn't carry the second one past the lock.
while IFS= read -r part; do
  grep -Eq '^[[:space:]]*factory-heavy([[:space:]]|$)' <<<"$part" && continue
  if grep -Eq '(^|[[:space:]])(docker[[:space:]]+(build|buildx)|govulncheck|kind[[:space:]]+create[[:space:]]+cluster|trivy|snyk[[:space:]]+test)([[:space:]]|$)' <<<"$part"; then
    deny "Heavy command: run it through \`factory-heavy <timeout-seconds> <command...>\` so only one runs at a time with a hard timeout."
  fi
done < <(perl -pe 's/\|\||&&|[;&|]/\n/g' <<<"$scan")

grep -Eq '(^|[;&|[:space:]])factory[[:space:]]+finish([[:space:]]|$)' <<<"$scan" && \
  deny "Only the person marks a project done from their terminal. Ask them on the desk, record their answer with desk_answer, then call finish_project."
grep -Eq 'gh[[:space:]]+pr[[:space:]]+review' <<<"$scan" && \
  deny "Factory sessions share one GitHub account and GitHub refuses self-approval. The reviewer posts a PR comment plus the factory/review commit status instead."
grep -Eq 'gh[[:space:]]+pr[[:space:]]+merge|gh[[:space:]]+api[^;&|]*pulls/[0-9]+/merge' <<<"$scan" && owner_only builder "merge pull requests"
grep -Eq 'statuses/' <<<"$scan" && grep -Eq 'gh[[:space:]]+api' <<<"$scan" && owner_only reviewer "set commit statuses"
grep -Eq '(git[[:space:]]+tag[[:space:]]+[^-]|git[[:space:]]+push[^;&|]*[[:space:]:](--tags|refs/tags|v[0-9]))|gh[[:space:]]+release[[:space:]]+(create|edit|delete)' <<<"$scan" && owner_only releaser "create tags or releases"
grep -Eq '(kubectl([[:space:]]+[^[:space:];&|]+)*[[:space:]]+(apply|rollout|delete|scale|set|patch|replace|create)([[:space:]]|$)|helm([[:space:]]+[^[:space:];&|]+)*[[:space:]]+(install|upgrade|uninstall|rollback)([[:space:]]|$))' <<<"$scan" && owner_only releaser "deploy, roll out or roll back"
grep -Eq '(^|[;&|[:space:]])(claude[[:space:]]+(--bg|--background|stop|kill|rm|respawn)|factory[[:space:]]+(spawn|retire|revive|restart|down|halt)|factory[[:space:]]+crew[[:space:]]+(up|down|halt|resume|freeze|unfreeze|update|restart))([[:space:]]|$)' <<<"$scan" && owner_only orchestrator "start, stop or revive stations"

exit 0
