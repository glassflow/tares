# Shared by the factory CLI tests: a real Tares daemon in a temporary folder, on a free port.
# Source it, then `start_tares <dir> [token]`: exports TARESD_URL (and TARES_AUTH_TOKEN with a token);
# the daemon stops when the test exits. `api METHOD PATH [JSON]` calls it.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY=python3
TARES_PIDS=""
stop_tares() { local p; for p in $TARES_PIDS; do kill "$p" 2>/dev/null; wait "$p" 2>/dev/null; done; TARES_PIDS=""; }

start_tares() { # dir [token]
  local dir=$1 token=${2:-} port
  mkdir -p "$dir"
  port=$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
  env TARES_DB="$dir/tares.duckdb" TARES_CATALOG="$dir/none.yaml" TARES_OTLP_GRPC_PORT=off \
    TARES_HOST=127.0.0.1 TARES_PORT="$port" TARES_AUTH_TOKEN="$token" HOME="$dir" \
    "$PY" -c "from tares.cli import run_daemon; run_daemon()" >"$dir/taresd.log" 2>&1 &
  TARES_PIDS="$TARES_PIDS $!"
  export TARESD_URL="http://127.0.0.1:$port"
  if [ -n "$token" ]; then export TARES_AUTH_TOKEN=$token; else unset TARES_AUTH_TOKEN; fi
  local _; for _ in $(seq 150); do curl -sf "$TARESD_URL/health" >/dev/null 2>&1 && return 0; sleep 0.2; done
  echo "FAIL Tares did not start on $TARESD_URL"; cat "$dir/taresd.log"; exit 1
}

api() { # method path [json]
  local -a a=(-sS -X "$1" -H 'Content-Type: application/json')
  [ -n "${TARES_AUTH_TOKEN:-}" ] && a+=(-H "Authorization: Bearer $TARES_AUTH_TOKEN")
  [ $# -ge 3 ] && a+=(--data "$3")
  curl "${a[@]}" "$TARESD_URL$2"
}

# A project on Tares handed to the crew with the given repo folders: name path...
hand_over() {
  local name=$1; shift
  local id; id=$(api POST /api/projects "$(jq -cn --arg n "$name" '{template: "custom", kind: "software_factory", name: $n, objects: []}')" | jq -r .id)
  local repos; repos=$(printf '%s\n' "$@" | jq -R '{path: .}' | jq -cs .)
  api POST "/api/projects/$id/handover" "$(jq -cn --argjson r "$repos" '{repos: $r}')" >/dev/null
  echo "$id"
}
