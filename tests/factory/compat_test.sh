#!/usr/bin/env bash
# Checks the crew CLI against the capabilities a cell lists on /health: `crew up` stops with an upgrade
# message when the cell lacks `integrations`, goes on when it has it, and `crew status` and `crew down`
# work either way (a version mismatch must never stop someone seeing or stopping their crew).
# A fake /health stands in for an old cell; a real daemon is the cell that has the capability.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib.sh"
F="${FACTORY_BIN:+$here/factory-bin}"; F="${F:-$here/../../claude-plugin/bin/factory}"
T=$(cd "$(mktemp -d)" && pwd -P)
export HOME="$T/home" FACTORY_CREW_HOME="$T/crew" FAKE_CLAUDE_STATE="$T/claude" CLAUDE_BIN="$here/fake-claude" FACTORY_PAUSE=0
unset TARES_URL TARES_AUTH_TOKEN TARES_FACTORY_URL TARES_FACTORY_TOKEN_FILE FACTORY_REPO FACTORY_WORKTREES CLAUDE_JOB_DIR TARES_FACTORY_MCP_URL TARES_FACTORY_CRED_FILE TARES_MCP_HOST TARES_MCP_PORT
mkdir -p "$HOME"
chmod +x "$CLAUDE_BIN"
trap 'stop_tares; rm -rf "$T"' EXIT
fail=0
check() { if eval "$2"; then :; else echo "FAIL $1"; fail=1; fi; }
launches() { grep -c '"--bg"' "$FAKE_CLAUDE_STATE/calls.jsonl" 2>/dev/null || echo 0; }

# A fake cell: answers /health with the given JSON body, on a free port, until the test ends.
fake_cell() { # json body → exports FAKE_URL
  local port; port=$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
  "$PY" - "$port" "$1" >"$T/fake.log" 2>&1 <<'PYEOF' &
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
body = sys.argv[2].encode()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PYEOF
  TARES_PIDS="$TARES_PIDS $!"
  FAKE_URL="http://127.0.0.1:$port"
  local _; for _ in $(seq 50); do curl -sf "$FAKE_URL/health" >/dev/null 2>&1 && return 0; sleep 0.1; done
  echo "FAIL the fake cell did not start"; cat "$T/fake.log"; exit 1
}

trust "$FACTORY_CREW_HOME"
MSG="This needs a newer Tares on the cell (missing integrations). Upgrade Tares, then run this again."

# An old cell: /health answers, with no capabilities at all.
fake_cell '{"status":"ok","auth_required":false,"sources":[],"pct_used":null,"version":"1.0.0"}'
old=$FAKE_URL
TARESD_URL=$old "$F" crew up --yes >"$T/old.out" 2>&1; rc=$?
check "crew up against a cell with no capabilities stops with the upgrade message" '[ $rc -ne 0 ] && grep -qF "$MSG" "$T/old.out"'
check "it starts nothing" '[ "$(launches)" = 0 ]'

# A cell that lists capabilities but not this one.
fake_cell '{"status":"ok","auth_required":false,"sources":[],"pct_used":null,"capabilities":["merge_queue"]}'
other=$FAKE_URL
TARESD_URL=$other "$F" crew up --yes >"$T/other.out" 2>&1; rc=$?
check "crew up against a cell with other capabilities stops the same way" '[ $rc -ne 0 ] && grep -qF "$MSG" "$T/other.out" && [ "$(launches)" = 0 ]'

# A real cell lists the capability, and crew up goes on to ask its settings and start the crew.
start_tares "$T/tares"
check "a real cell lists integrations on /health" 'curl -sf "$TARESD_URL/health" | jq -e ".capabilities | index(\"integrations\")" >/dev/null'
printf 'L3-review\nno\nno\n2\n\n' | "$F" crew up >"$T/up.out" 2>&1; rc=$?
check "crew up against a cell with the capability proceeds" '[ $rc -eq 0 ] && ! grep -qF "needs a newer Tares" "$T/up.out" && grep -q "started crew-reviewer" "$T/up.out"'
"$F" crew status >"$T/status3.out" 2>&1; rc=$?
check "crew status works against a cell with the capability" '[ $rc -eq 0 ] && grep -q "crew-reviewer" "$T/status3.out"'


# With a crew running, status and down never ask for a capability: both work against a cell that lacks it.
TARESD_URL=$old "$F" crew status >"$T/status.out" 2>&1; rc=$?
check "crew status works against a cell without the capability" '[ $rc -eq 0 ] && ! grep -qF "needs a newer Tares" "$T/status.out"'
TARESD_URL=$old "$F" crew down >"$T/down.out" 2>&1; rc=$?
check "crew down works against a cell without the capability" '[ $rc -eq 0 ] && ! grep -qF "needs a newer Tares" "$T/down.out" && grep -q "stopped\|down" "$T/down.out"'
TARESD_URL=$other "$F" crew status >"$T/status2.out" 2>&1; rc=$?
check "crew status works against a cell with other capabilities" '[ $rc -eq 0 ] && ! grep -qF "needs a newer Tares" "$T/status2.out"'
check "the CLI never asks for a capability in status or down" '! awk "/^cmd_status\(\)/,/^}/" "$F" | grep -q need_capability && ! awk "/^cmd_down\(\)/,/^}/" "$F" | grep -q need_capability'

[ $fail = 0 ] && echo "compat: all checks passed"
exit $fail
