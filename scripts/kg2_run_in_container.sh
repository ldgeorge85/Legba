#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# K-G2 — run the typing bake-off in a DEDICATED one-off container.
#
# Why not `docker exec` into the live runtime: the two self-hosted planes need
# the credential vault (so the harness must run somewhere holding
# LEGBA_DATA_MASTER_KEY), but the live runtime is production — it gets recreated
# by deploys and its /tmp goes with it, taking a half-finished bake-off along.
# This spins a throwaway container off the SAME image, on the compose network,
# with the work directory bind-mounted from the host, so results land outside
# the container and a production restart cannot touch the run.
#
# Usage:
#   scripts/kg2_run_in_container.sh <work-dir> [extra args for kg2_typing_bakeoff.py]
#
# <work-dir> must already contain sample_payloads.json.

set -euo pipefail

WORKDIR="${1:?usage: kg2_run_in_container.sh <work-dir> [args...]}"
shift

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${KG2_ENV_FILE:-/usr/local/deployments/active/legba/.env}"
IMAGE="${KG2_IMAGE:-legba/legba-runtime-dapr:latest}"
NETWORK="${KG2_NETWORK:-legba_default}"
SITE="/install/lib/python3.11/site-packages/legba"

mkdir -p "$WORKDIR/results"

# The Postgres coordinates are set by docker-compose's `environment:` block, not
# by .env, so --env-file alone leaves them unset. They are the compose literals.
exec docker run --rm \
  --network "$NETWORK" \
  --env-file "$ENV_FILE" \
  -e LEGBA_DATA_PG_HOST="${LEGBA_DATA_PG_HOST:-postgres}" \
  -e LEGBA_DATA_PG_PORT="${LEGBA_DATA_PG_PORT:-5432}" \
  -e LEGBA_DATA_PG_USER="${LEGBA_DATA_PG_USER:-legba}" \
  -e LEGBA_DATA_PG_PASSWORD="${LEGBA_DATA_PG_PASSWORD:-legba}" \
  -e LEGBA_DATA_PG_DB="${LEGBA_DATA_PG_DB:-legba}" \
  -v "$WORKDIR:/work" \
  -v "$REPO_ROOT/src/legba/data/analysts/relationship_typing_batch.py:$SITE/data/analysts/relationship_typing_batch.py:ro" \
  -v "$REPO_ROOT/src/legba/data/analysts/edge_qualification.py:$SITE/data/analysts/edge_qualification.py:ro" \
  -v "$REPO_ROOT/scripts/kg2_typing_bakeoff.py:/work/kg2_typing_bakeoff.py:ro" \
  --entrypoint python \
  "$IMAGE" /work/kg2_typing_bakeoff.py --dir /work "$@"
