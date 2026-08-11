#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# K-G3 · Drive the whole AGE probe against a SCRATCH Postgres+AGE container.
#
# Brings up a throwaway apache/age instance pinned BY DIGEST (the same digest
# the live substrate runs, so the measurement is about the deployed engine),
# generates the synthetic world graph at two scales, loads each scale as BOTH
# an AGE graph and an entity_edges-shaped relational twin, and runs the
# measurement matrix under a stack of Postgres configurations.
#
# It NEVER touches the live substrate. Container name, port and volume are all
# probe-specific, and `--teardown` removes every one of them.
#
#   scripts/age_probe/run_probe.sh up            # start the scratch instance
#   scripts/age_probe/run_probe.sh claims        # verify the debate's claims
#   scripts/age_probe/run_probe.sh bench 100k    # generate + load + measure
#   scripts/age_probe/run_probe.sh bench 1m
#   scripts/age_probe/run_probe.sh teardown      # remove container + volume
set -euo pipefail

# The digest the live substrate runs (docker-compose.yml postgres service).
AGE_DIGEST="${AGE_DIGEST:-sha256:4241e2d8bb86a6b2ea44e9ad06c73856e12b209de295124603a599dd7feb70eb}"
# /dev/shm sizing. The live substrate ships `shm_size: 1gb`; the probe needs
# MORE, and finding that out was itself a result. At the 1M scale the
# direction-explicit Cypher rewrite (8 UNION branches over 1M edges) does not
# merely get slow under 1 GB — it dies with
#   DiskFullError: could not resize shared memory segment ... No space left on device
# because Postgres allocates parallel-query DSM segments out of /dev/shm.
# 4 GB completes the matrix. See docs/AGE_PROBE_REPORT.md §3.5.
SHM_SIZE="${SHM_SIZE:-4g}"
CONTAINER="${CONTAINER:-legba-kg3-probe}"
VOLUME="${VOLUME:-legba-kg3-probe-data}"
PORT="${PORT:-55433}"
DSN="postgresql://probe:probe@127.0.0.1:${PORT}/probe"
WORK="${WORK:-/tmp/legba_age_probe}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS="${RESULTS:-${WORK}/results.jsonl}"

# The live substrate's tuning profile (docker-compose.yml `command:` block).
# shared_buffers is the ONE deliberate deviation: live sets 8GB, the probe sets
# 2GB, because the probe's whole working set is ~1GB — both values cache it
# entirely, and 8GB would mean 8GB of RSS on the production host. Verified by
# checking the buffer hit ratio stays ~100% in the tuned arm.
TUNED_GUCS=(
  "shared_buffers=2GB"
  "effective_cache_size=24GB"
  "work_mem=32MB"
  "maintenance_work_mem=1GB"
  "max_wal_size=4GB"
  "wal_buffers=64MB"
  "random_page_cost=1.1"
  "effective_io_concurrency=200"
)

psql_probe() { docker exec -i "$CONTAINER" psql -U probe -d probe -X -P pager=off "$@"; }

wait_ready() {
  for _ in $(seq 1 60); do
    if docker exec "$CONTAINER" pg_isready -U probe >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "scratch instance never became ready" >&2; exit 1
}

apply_config() {
  # $1 = arm name; remaining args = extra `key=value` GUCs on top of TUNED.
  local arm="$1"; shift
  psql_probe -c "ALTER SYSTEM RESET ALL" >/dev/null
  if [[ "$arm" != "default" ]]; then
    for guc in "${TUNED_GUCS[@]}" "$@"; do
      psql_probe -c "ALTER SYSTEM SET ${guc%%=*} = '${guc#*=}'" >/dev/null
    done
  fi
  docker restart "$CONTAINER" >/dev/null
  wait_ready
}

cmd_up() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker volume create "$VOLUME" >/dev/null
  docker run -d --name "$CONTAINER" \
    -e POSTGRES_USER=probe -e POSTGRES_PASSWORD=probe -e POSTGRES_DB=probe \
    -e PGDATA=/var/lib/postgresql/data/pgdata \
    -p "127.0.0.1:${PORT}:5432" \
    -v "${VOLUME}:/var/lib/postgresql" \
    --shm-size="${SHM_SIZE}" --memory=6g \
    "apache/age@${AGE_DIGEST}" \
    postgres -c shared_preload_libraries=age >/dev/null
  wait_ready
  psql_probe -c "CREATE EXTENSION IF NOT EXISTS age" >/dev/null
  echo "scratch AGE instance up on ${PORT}"
  psql_probe -tAc "SELECT version()"
  psql_probe -tAc "SELECT 'AGE ' || extversion FROM pg_extension WHERE extname='age'"
}

cmd_claims() {
  python3 "${HERE}/age_claims.py" --dsn "$DSN" --json-out "${WORK}/claims.json"
}

cmd_bench() {
  local scale="${1:?scale: 100k|1m}"
  local edges repeats timeout
  case "$scale" in
    100k) edges=100000;  repeats=5; timeout=60000  ;;
    1m)   edges=1000000; repeats=3; timeout=60000  ;;
    *) echo "unknown scale $scale" >&2; exit 2 ;;
  esac
  local dir="${WORK}/g${scale}"
  mkdir -p "$WORK"
  [[ -f "${dir}/edges.csv" ]] || \
    python3 "${HERE}/world_graph_gen.py" --entities 50000 --edges "$edges" --out "$dir"

  # The in-process networkx arm needs no server, and doubles as the E4 gauge.
  # Skipped on a resume: rebuilding the snapshot at 1M takes minutes, and the
  # arm is deterministic, so re-running it would only append duplicates.
  if [[ -f "$RESULTS" ]] && grep -q "\"scale\": \"${scale}\", \"arm\": \"in_process\"" "$RESULTS"; then
    echo ">> nx arm for ${scale} already recorded; skipping"
  else
    python3 "${HERE}/age_probe_bench.py" nx --dir "$dir" --scale "$scale" \
        --out "$RESULTS" --repeats "$repeats"
  fi

  # Three FULL-matrix arms isolate the three candidate causes of any ceiling:
  #   default        stock apache/age config — what an untuned deploy gets
  #   tuned          the LIVE stack's GUCs   — is the ceiling our Postgres setup?
  #   tuned_propidx  + the AGE vertex property index — is it a missing index?
  # Anything still slow in tuned_propidx is the ENGINE, not the deployment.
  #
  # Then three single-knob ablations, run only on the queries that actually
  # hurt, so each knob's contribution is attributable rather than bundled.
  local abl="ego2,ego3,vlp6,triad"
  for arm in default tuned tuned_propidx jit_off parallel8 workmem256; do
    local only=""
    case "$arm" in
      default)       apply_config default;  idx=off ;;
      tuned)         apply_config tuned;    idx=off ;;
      tuned_propidx) apply_config tuned;    idx=on  ;;
      jit_off)       apply_config tuned "jit=off";                            idx=on; only="$abl" ;;
      parallel8)     apply_config tuned "max_parallel_workers_per_gather=8" \
                                        "max_parallel_workers=8" \
                                        "max_worker_processes=16";            idx=on; only="$abl" ;;
      workmem256)    apply_config tuned "work_mem=256MB";                     idx=on; only="$abl" ;;
    esac
    # The data survives restarts; load once, on the first arm only.
    if [[ "$arm" == "default" ]]; then
      python3 "${HERE}/age_probe_bench.py" load --dsn "$DSN" --dir "$dir" \
        | tee "${WORK}/load_${scale}.json"
    fi
    python3 "${HERE}/age_probe_bench.py" measure --dsn "$DSN" --dir "$dir" \
      --scale "$scale" --label "$arm" --out "$RESULTS" \
      --repeats "$repeats" --timeout-ms "$timeout" --prop-index "$idx" \
      --budget-ms "${BUDGET_MS:-45000}" ${only:+--only "$only"}
  done
}

cmd_teardown() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker volume rm "$VOLUME" >/dev/null 2>&1 || true
  echo "removed container ${CONTAINER} and volume ${VOLUME}"
  docker ps -a --filter "name=${CONTAINER}" --format '{{.Names}}'
  docker volume ls --filter "name=${VOLUME}" --format '{{.Name}}'
}

case "${1:?usage: up|claims|bench <scale>|teardown}" in
  up)       cmd_up ;;
  claims)   cmd_claims ;;
  bench)    shift; cmd_bench "$@" ;;
  teardown) cmd_teardown ;;
  *) echo "unknown command $1" >&2; exit 2 ;;
esac
