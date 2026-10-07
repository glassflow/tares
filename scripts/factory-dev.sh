#!/bin/bash
# Local setup for trying the software factory branch (P-TR-176, M1..M7) with the tares-factory
# kit. Run from the root of a tares checkout on ashish/software-factory-m2-m7.
#
#   scripts/factory-dev.sh install   uv venv + the branch's tares, and the console build
#   scripts/factory-dev.sh up        a Tares on 127.0.0.1:8787, data in ~/.tares-factory-dev
#   scripts/factory-dev.sh seed      a crew-built demo project ("Bakery orders") to look at
#   scripts/factory-dev.sh env       the lines to paste into the shell that runs `factory crew up`
#   scripts/factory-dev.sh claude    Claude Code with this branch's plugin (for /tares:spec)
#
# The branch's plugin is loaded from claude-plugin/ with --plugin-dir. An installed `tares` plugin
# from the marketplace wins over it (and the kit uses it for every station), so disable that one
# while testing: `claude plugin disable tares@tares` (enable it again afterwards).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DATA=${TARES_DEV_HOME:-$HOME/.tares-factory-dev}
PORT=${TARES_DEV_PORT:-8787}

case "${1:-help}" in
  install)
    cd "$ROOT"
    command -v uv >/dev/null || { echo "needs uv: https://docs.astral.sh/uv/"; exit 1; }
    [ -d .venv ] || uv venv
    uv pip install -e ".[otlp-grpc]"
    (cd ui && npm ci && npm run build)
    echo "installed: $ROOT/.venv, console built in ui/dist"
    ;;
  up)
    mkdir -p "$DATA"
    echo "Tares on http://127.0.0.1:$PORT (data in $DATA); Ctrl-C stops it"
    TARES_OTLP_GRPC_PORT=off exec "$ROOT/.venv/bin/tares" up --port "$PORT" --data-dir "$DATA"
    ;;
  seed)
    exec "$ROOT/.venv/bin/python" "$ROOT/scripts/seed_factory_demo.py" "http://127.0.0.1:$PORT"
    ;;
  env)
    cat <<EOF
export PATH="$ROOT/.venv/bin:\$PATH"            # this branch's tares-mcp for every station
export TARES_URL=http://127.0.0.1:$PORT          # the kit finds Tares here
export TARES_PLUGIN_DIR="$ROOT/claude-plugin"    # stations load this branch's plugin
export CLAUDE_PLUGIN_OPTION_TARES_URL=http://127.0.0.1:$PORT
EOF
    ;;
  claude)
    shift
    export PATH="$ROOT/.venv/bin:$PATH"
    export CLAUDE_PLUGIN_OPTION_TARES_URL="http://127.0.0.1:$PORT"
    exec claude --plugin-dir "$ROOT/claude-plugin" "$@"
    ;;
  *)
    sed -n '2,15p' "$0"
    ;;
esac
