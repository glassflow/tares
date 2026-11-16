#!/bin/sh
# The gallery screenshot check (TR-1269), run in the Linux image CI uses, so a Mac draws what CI draws.
#   npm run test:visual            compare against the committed pictures
#   npm run test:visual:update     redraw them, after a change that moves pixels on purpose
# Arguments go to `playwright test`. Needs Docker. The image installs its own node_modules into a
# Docker volume, so ui/node_modules (built for macOS) is never touched; it rebuilds ui/dist.
# The diff images land in ui/test-results/visual. The image tag moves with @playwright/test.
set -e
UI=$(cd "$(dirname "$0")/../.." && pwd)
IMAGE=mcr.microsoft.com/playwright:v1.60.0-noble
exec docker run --rm --ipc=host -e CI -v "$UI:/ui" -v tares-visual-node-modules:/ui/node_modules -w /ui "$IMAGE" \
  sh -c 'npm ci --no-audit --no-fund --loglevel=error && npm run -s build && npx playwright test -c playwright.visual.config.ts "$@"' sh "$@"
