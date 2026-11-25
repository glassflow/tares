#!/usr/bin/env bash
# Builds the marketing screenshot library of the factory console.
# Usage, from a worktree with the console built (cd ui && npm ci && npm run build) and .venv installed:
#   scripts/marketing-screenshots/run.sh <libDir> [slugRegex]
# Starts a scratch factory cell on $PORT (default 8851) with a fresh data dir, seeds "Bakery orders",
# shoots scenes.cjs, writes manifest.json and README.md, then stops only the daemon it started.
set -euo pipefail

here="scripts/marketing-screenshots"
lib="${1:?usage: run.sh <libDir> [slugRegex]}"
port="${PORT:-8851}"

[[ -f "$here/run.sh" ]] || { echo "run from the repo root" >&2; exit 1; }
[[ -f ui/dist/index.html ]] || { echo "build the console first: cd ui && npm run build" >&2; exit 1; }
for p in "$port" $((port + 1)); do
  if lsof -tiTCP:"$p" -sTCP:LISTEN >/dev/null; then echo "port $p is busy" >&2; exit 1; fi
done

data=$(mktemp -d)
env -i HOME="$HOME" PATH="$PATH" TARES_EDITION=factory TARES_OTLP_GRPC_PORT=off \
  TARES_MCP_PORT=$((port + 1)) TARES_INSTANCE_NAME=demo \
  .venv/bin/tares up --port "$port" --data-dir "$data" >"$data/daemon.log" 2>&1 &
daemon=$!
trap 'kill "$daemon" 2>/dev/null || true; wait "$daemon" 2>/dev/null || true; rm -rf "$data"' EXIT
for _ in $(seq 1 30); do curl -sf "127.0.0.1:$port/health" >/dev/null && break; sleep 1; done

project=$(.venv/bin/python scripts/seed_factory_demo.py "http://127.0.0.1:$port" | tail -1 |
  .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["project"])')

PORT="$port" PROJECT="$project" node "$here/capture.cjs" "$lib" "${2:-}"
cp "$here/library-README.md" "$lib/README.md"
if [[ -z "${2:-}" ]]; then node "$here/manifest.cjs" "$lib"; fi
