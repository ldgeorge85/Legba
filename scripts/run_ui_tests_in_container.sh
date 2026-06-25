#!/usr/bin/env bash
# Run legba-ui-v3 lint (tsc --noEmit) + unit tests (vitest) in a node container.
# There is no node on the host; this is the v4 agents' SECOND verify gate, after
# the container image build (`docker compose build legba-ui-build`). node_modules
# is the host-mounted dir populated by the W0 dep install.
set -euo pipefail
cd "$(dirname "$0")/../legba-ui-v3"
docker run --rm -v "$PWD":/app -w /app node:20-slim sh -c \
  '[ -d node_modules ] || npm install --no-audit --no-fund; npm run lint && npm run test'
