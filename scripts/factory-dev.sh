#!/bin/bash
# Trying the software factory from a tares checkout (P-TR-176). The flow itself is in
# claude-plugin/FACTORY.md; this script only does the setup steps a checkout needs.
#
#   scripts/factory-dev.sh install   the console build, then a uv venv with this checkout's tares
#   scripts/factory-dev.sh up        a Tares on 127.0.0.1:$TARES_DEV_PORT (default 8787), data in ~/.tares-factory-dev
#   scripts/factory-dev.sh seed      a crew-built demo project ("Bakery orders") to look at, no crew needed
#
# Then, in any terminal: `source .venv/bin/activate` (so `tares` is this checkout's), and
# `tares factory crew up`, `tares factory spec`. Set TARESD_URL when Tares is not on 8787.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DATA=${TARES_DEV_HOME:-$HOME/.tares-factory-dev}
PORT=${TARES_DEV_PORT:-8787}

case "${1:-help}" in
  install)
    cd "$ROOT"
    command -v uv >/dev/null || { echo "needs uv: https://docs.astral.sh/uv/"; exit 1; }
    # the console first: the package build includes ui/dist and fails without it
    (cd ui && npm ci && npm run build)
    [ -d .venv ] || uv venv
    uv pip install -e ".[otlp-grpc]"
    echo "installed: $ROOT/.venv (tares, tares-mcp), console built in ui/dist"
    echo "next: source .venv/bin/activate, then scripts/factory-dev.sh up"
    ;;
  up)
    mkdir -p "$DATA"
    echo "Tares on http://127.0.0.1:$PORT (data in $DATA); Ctrl-C stops it"
    [ "$PORT" = 8787 ] || echo "in the terminals where you run tares factory: export TARESD_URL=http://127.0.0.1:$PORT"
    TARES_OTLP_GRPC_PORT=off exec "$ROOT/.venv/bin/tares" up --port "$PORT" --data-dir "$DATA"
    ;;
  seed)
    exec "$ROOT/.venv/bin/python" "$ROOT/scripts/seed_factory_demo.py" "http://127.0.0.1:$PORT"
    ;;
  *)
    sed -n '2,11p' "$0"
    ;;
esac
