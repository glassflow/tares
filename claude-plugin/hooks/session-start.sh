#!/usr/bin/env bash
# Reminds a crew station who it is on start, resume and after compaction.
# Silent in sessions that are not factory stations.
set -euo pipefail
[ -n "${FACTORY_ROLE:-}" ] || exit 0

source_kind=$(jq -r '.source // "startup"' 2>/dev/null || echo startup)

skill=$FACTORY_ROLE
if [ "$skill" = helper ]; then
  case "${FACTORY_PARENT:-}" in crew-*) skill=${FACTORY_PARENT#crew-} ;; *-build-*) skill=builder ;; esac
fi

context="You are the ${FACTORY_ROLE} station \`${FACTORY_STATION:-?}\` of the tares-factory crew."
[ -n "${TARES_PROJECT:-}" ] && context="$context You work on project \`${TARES_PROJECT}\` in ${FACTORY_REPO:-?}."
[ -n "${FACTORY_PARENT:-}" ] && context="$context You were started by \`${FACTORY_PARENT}\`."
if [ "${FACTORY_TARES:-off}" = on ]; then
  context="$context Tares is connected: the plan, ledger, assumptions, checks, reviews and desk are on Tares through its MCP tools."
else
  context="$context Tares is not connected: use the local files in ${FACTORY_HOME:-?} and ${FACTORY_PROJECT_HOME:-the .factory folder of each project repo}."
fi
context="$context Your rules are in the skills \`factory-protocol\` and \`station-${skill}\`."
if [ "$source_kind" != "startup" ]; then
  context="$context This session was ${source_kind}d: load both skills again, re-read your report and your work (on Tares, or the local files), check CronList and re-arm your schedule if it is missing, then carry on where your report left off."
fi
if [ -n "${FACTORY_HOME:-}" ] && [ -f "$FACTORY_HOME/HALT" ]; then
  context="$context The crew is halted ($(jq -r '.reason // ""' "$FACTORY_HOME/HALT" 2>/dev/null)): start nothing new until it is resumed."
fi

jq -n --arg c "$context" '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $c}}'
