#!/usr/bin/env bash
# Run legba-ui-v3 lint (tsc --noEmit) + unit tests (vitest) in a node container.
# There is no node on the host; this is the v4 agents' SECOND verify gate, after
# the container image build (`docker compose build legba-ui-build`). node_modules
# is the host-mounted dir, refreshed with `npm ci` when missing or older than
# the lockfile (a stale tree tested the wrong maplibre-gl for a day, roll 81).
set -euo pipefail
cd "$(dirname "$0")/../legba-ui-v3"
docker run --rm -v "$PWD":/app -w /app node:20-slim sh -c \
  'if [ ! -f node_modules/.package-lock.json ] || [ package-lock.json -nt node_modules/.package-lock.json ]; then npm ci --no-audit --no-fund; fi && npm run lint && npm run test'
