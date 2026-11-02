#!/usr/bin/env bash
# Proves that real Claude Code sessions with this branch's plugin reach Tares through one shared HTTP
# MCP endpoint, each under its own station identity and with the crew's token from a file, and that
# none of them starts tares-mcp. Two sessions run against a throwaway taresd and `tares mcp`: a
# reviewer station, which Tares must refuse a desk entry, and an orchestrator station, which may
# write one. Tares's own desk is the judge, not the model's words.
#
# Costs a few Haiku calls. Needs `claude` on PATH and the worktree's .venv (uv venv && uv pip
# install -e .). Never touches the live Tares on 8787: the sessions ship their transcripts to the
# throwaway taresd too.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || { echo "FAIL  no $PY: run uv venv && uv pip install -e . in $REPO"; exit 1; }
command -v claude >/dev/null || { echo "FAIL  claude is not on PATH"; exit 1; }

T=$(cd "$(mktemp -d)" && pwd -P)
PIDS=""
cleanup() { local p; for p in $PIDS; do kill "$p" 2>/dev/null || true; wait "$p" 2>/dev/null || true; done; rm -rf "$T"; }
trap cleanup EXIT
free_port() { "$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }

# b. a throwaway taresd
ROOT_TOKEN="e2e-$("$PY" -c 'import secrets; print(secrets.token_hex(16))')"
dport=$(free_port); DAEMON="http://127.0.0.1:$dport"
mkdir -p "$T/tares"
env -u TARES_FACTORY_URL TARES_DB="$T/tares/tares.duckdb" TARES_CATALOG="$T/tares/none.yaml" TARES_OTLP_GRPC_PORT=off \
  TARES_HOST=127.0.0.1 TARES_PORT="$dport" TARES_AUTH_TOKEN="$ROOT_TOKEN" HOME="$T/tares" \
  "$PY" -c "from tares.cli import run_daemon; run_daemon()" >"$T/taresd.log" 2>&1 &
PIDS="$PIDS $!"
up=0; for _ in $(seq 150); do curl -sf "$DAEMON/health" >/dev/null 2>&1 && { up=1; break; }; sleep 0.2; done
[ $up = 1 ] || { echo "FAIL  taresd did not start"; cat "$T/taresd.log"; exit 1; }

# c. the shared MCP endpoint, as a person runs it
mport=$(free_port); MCP="http://127.0.0.1:$mport/mcp"
env -u TARES_FACTORY_URL TARES_AUTH_TOKEN="$ROOT_TOKEN" HOME="$T/tares" \
  "$REPO/.venv/bin/tares" mcp --transport streamable-http --host 127.0.0.1 --port "$mport" --taresd "$DAEMON" \
  >"$T/mcp.log" 2>&1 &
PIDS="$PIDS $!"
init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"e2e","version":"0"}}}'
up=0
for _ in $(seq 150); do
  printf 'Authorization: Bearer %s\n' "$ROOT_TOKEN" | curl -sf --max-time 2 -X POST -H @- -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' --data "$init" "$MCP" 2>/dev/null | grep -q serverInfo && { up=1; break; }
  sleep 0.2
done
[ $up = 1 ] || { echo "FAIL  the MCP server did not start"; cat "$T/mcp.log"; exit 1; }

# d. the crew's token, in a file only the person reads
(umask 077; printf '%s\n' "$ROOT_TOKEN" > "$T/cred")

# e. a tares-mcp that leaves a mark if any session starts it
mkdir -p "$T/bin"
printf '#!/bin/sh\ntouch "%s/tares-mcp-started"\nexit 1\n' "$T" > "$T/bin/tares-mcp"; chmod +x "$T/bin/tares-mcp"

# f. one real session per station. Only the branch plugin loads: a person who turned tares@inline off
# gets it back on, and an installed tares plugin from a marketplace is turned off for these sessions.
disabled=$(jq -c '[.plugins // {} | keys[] | select(startswith("tares@"))] | map({(.): false}) | add // {}' \
  "$HOME/.claude/plugins/installed_plugins.json" 2>/dev/null || echo '{}')
for role in reviewer orchestrator; do
  jq -n --arg role "$role" --arg mcp "$MCP" --arg cred "$T/cred" --arg daemon "$DAEMON" --argjson off "$disabled" '{
    env: {FACTORY_STATION: ("e2e-" + $role), FACTORY_ROLE: $role,
          TARES_FACTORY_MCP_URL: $mcp, TARES_FACTORY_CRED_FILE: $cred,
          TARES_FACTORY_URL: $daemon, TARES_FACTORY_TOKEN_FILE: $cred},
    enabledPlugins: ($off + {"tares@inline": true})
  }' > "$T/$role.settings.json"
  (cd "$T" && PATH="$T/bin:$PATH" claude -p "Call the tool mcp__plugin_tares_tares__desk_add exactly once with question 'e2e-$role', recommendation 'r', why 'w'. Then print the tool result verbatim." \
    --plugin-dir "$REPO/claude-plugin" --settings "$T/$role.settings.json" \
    --allowedTools mcp__plugin_tares_tares__desk_add --model haiku < /dev/null > "$T/$role.out" 2>&1) || true
done

# g. judge by Tares's own record
desk=$(printf 'Authorization: Bearer %s\n' "$ROOT_TOKEN" | curl -sf -H @- "$DAEMON/api/desk?state=" || echo '{}')
fail=0
has() { jq -e --arg q "$1" '[.desk[]?.question] | index($q) != null' <<<"$desk" >/dev/null; }
# the refusal text proves the reviewer's call reached Tares, rather than never being made
if ! has e2e-reviewer && grep -qi "only the orchestrator" "$T/reviewer.out"; then echo "ok    reviewer station refused desk_add"
else echo "FAIL  reviewer station refused desk_add"; cat "$T/reviewer.out"; fail=1; fi
if has e2e-orchestrator; then echo "ok    orchestrator station wrote the desk entry"
else echo "FAIL  orchestrator station wrote the desk entry"; cat "$T/orchestrator.out"; fail=1; fi
if [ ! -e "$T/tares-mcp-started" ]; then echo "ok    no tares-mcp process started by the sessions"
else echo "FAIL  no tares-mcp process started by the sessions"; cat "$T/reviewer.out" "$T/orchestrator.out"; fail=1; fi
exit $fail
