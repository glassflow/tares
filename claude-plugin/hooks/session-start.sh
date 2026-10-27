#!/usr/bin/env bash
# Reminds a tares-factory crew station who it is on start, resume and after compaction, and a
# person's own session bound to a project (`tares factory session`, TARES_PROJECT set) which project
# it works on (or, with TARES_SESSION_KIND=contributor, that it only contributes to it).
# Silent in every other session.
set -euo pipefail

source_kind=$(jq -r '.source // "startup"' 2>/dev/null || echo startup)

if [ -z "${FACTORY_ROLE:-}" ]; then
  [ -n "${TARES_PROJECT:-}" ] || exit 0
  if [ "${TARES_SESSION_KIND:-}" = contributor ]; then
    # `tares factory spec <project>` run by someone who does not lead it: answer, check, sign off; never build
    context="This session contributes to the Tares project \`${TARES_PROJECT}\` led by someone else, on ${TARES_FACTORY_URL:-the Tares} through the Tares MCP tools. Follow the \`spec-project\` skill, section Contributor. Do not build tickets."
    [ "$source_kind" = "startup" ] || context="$context This session was resumed or compacted: load the \`spec-project\` skill again, section Contributor, and carry on from its steps."
    jq -n --arg c "$context" '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $c}}'
    exit 0
  fi
  context="This session works on the Tares project \`${TARES_PROJECT}\` by itself (tares factory session), on ${TARES_FACTORY_URL:-the Tares} through the Tares MCP tools. Follow the \`session\` skill."
  if [ "$source_kind" = "startup" ]; then
    [ -n "${TARES_SESSION_TAKEOVER:-}" ] && context="$context The person took this project over from the crew: do the skill's \"Taking over\" steps first."
  else
    context="$context This session was resumed or compacted: load the \`session\` skill again, re-read the project's tickets and the working doc of the one in progress, then carry on where you left off (a takeover you already asked for is not asked again)."
  fi
  jq -n --arg c "$context" '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $c}}'
  exit 0
fi

skill=$FACTORY_ROLE
if [ "$skill" = helper ]; then
  crew=${FACTORY_CREW:-crew}
  case "${FACTORY_PARENT:-}" in "$crew"-orchestrator*|"$crew"-reviewer*|"$crew"-releaser*) skill=${FACTORY_PARENT#"$crew"-} ;; *-build-*) skill=builder ;; esac
fi

context="You are the ${FACTORY_ROLE} station \`${FACTORY_STATION:-?}\` of the tares-factory crew."
[ -n "${TARES_PROJECT:-}" ] && context="$context You work on project \`${TARES_PROJECT}\` in ${FACTORY_REPO:-?}."
[ -n "${FACTORY_PARENT:-}" ] && context="$context You were started by \`${FACTORY_PARENT}\`."
context="$context The plan, tickets, assumptions, checks, reviews and the desk are on Tares (${TARES_FACTORY_URL:-the crew Tares}), through the Tares MCP tools."
context="$context Your rules are in the skills \`factory-protocol\` and \`station-${skill}\`."
if [ "$source_kind" != "startup" ]; then
  context="$context This session was ${source_kind}d: load both skills again, re-read your report and your work on Tares, check CronList and re-arm your schedule if it is missing, then carry on where your report left off."
fi
if [ -n "${FACTORY_HOME:-}" ] && [ -f "$FACTORY_HOME/HALT" ]; then
  context="$context The crew is halted ($(jq -r '.reason // ""' "$FACTORY_HOME/HALT" 2>/dev/null)): start nothing new until it is resumed."
fi

jq -n --arg c "$context" '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $c}}'
