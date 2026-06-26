#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# deploy/deploy.sh — the ONE-COMMAND Legba deploy.
#
# Canonical, parameterized, idempotent, phased bring-up of a Legba stack from
# empty volumes through to a boot-verified instance. Replaces the ~16 hand-typed
# commands previously spread across README / docs/SETUP.md / docs/RUNBOOK.md.
#
# This wraps the SAME proven, load-bearing ordering those docs describe:
#   substrate+registry up → apply baseline schema → vault → stack → packs →
#   sources+catalog → targets → analysts → budget → [seeds] → runtime up → verify.
#
# It applies the single proven baseline (deploy/baseline/0001_baseline.sql), NOT
# the 23-file migration history, then runs `python -m legba.data.migrate` for any
# FUTURE (0054+) migrations (currently a no-op — the ledger is pre-seeded to 0053).
#
# ----------------------------------------------------------------------------
# USAGE
#   deploy/deploy.sh [--project NAME] [--env-file PATH] [--seed]
#                    [--no-caddy] [--teardown]
#
#   --project NAME     Compose project name. Default: legba (the REAL stack).
#                      Any other name triggers the DATA-ISOLATION GATE and the
#                      deploy/compose.isolation.yml override (project-scoped
#                      volumes + scheduler bind). Use a non-legba project for a
#                      throwaway clean-slate validation stack.
#   --env-file PATH    Env file passed to compose + bringup. Default: .env
#   --seed             Run the curated knowledge-seed sequence (world_baseline,
#                      sipri_arms_transfers; wikidata_leaders/acled need network/
#                      creds and are opt-in via env, see SEED_SOURCES below).
#                      Seeds run BEFORE the runtime boots (nlp_client footgun).
#   --no-caddy         Skip the caddy TLS edge (loopback-only stack). Recommended
#                      for any non-legba validation stack (no 443/ACME contention).
#   --teardown         Tear the stack down (see SAFETY below) instead of deploying.
#
# SAFETY (teardown)
#   * --project legba  → REFUSES `down -v`; only `docker compose -p legba stop`
#                        (preserves volumes — the only-instance rule).
#   * --project other  → `docker compose -p <name> down -v`, but ONLY after
#                        re-confirming (grep) the rendered volumes are the
#                        project-isolated ones, never the real legba_* names.
#
# This script AUTHORS no data and runs read-only `config` renders before any `up`.
# ----------------------------------------------------------------------------

set -Eeuo pipefail

# --- locate the repo root (this script lives in <root>/deploy/) --------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# --- defaults ----------------------------------------------------------------
PROJECT="legba"
ENV_FILE=".env"
DO_SEED=0
NO_CADDY=0
DO_TEARDOWN=0

COMPOSE_BASE="docker-compose.yml"
ISOLATION_OVERRIDE="deploy/compose.isolation.yml"
BASELINE_SQL="deploy/baseline/0001_baseline.sql"

# Registry health endpoint (served by legba-registry, proxied by caddy).
REGISTRY_HEALTH_PATH="/api/v1/registry/healthz"

# Curated seed sources run by --seed. Network/cred-dependent adapters
# (wikidata_leaders, acled_conflict) are opt-in: set LEGBA_SEED_SOURCES to
# override this list (space-separated). Default = offline curated YAML only.
SEED_SOURCES="${LEGBA_SEED_SOURCES:-world_baseline sipri_arms_transfers}"

# --- pretty logging ----------------------------------------------------------
_c() { printf '\033[%sm' "$1" 2>/dev/null || true; }
BOLD="$(_c '1')"; RED="$(_c '31')"; GRN="$(_c '32')"; YEL="$(_c '33')"; CYN="$(_c '36')"; RST="$(_c '0')"
phase() { printf '\n%s== %s ==%s\n' "${BOLD}${CYN}" "$*" "${RST}"; }
info()  { printf '%s   - %s%s\n' "${RST}" "$*" "${RST}"; }
ok()    { printf '%s   [OK] %s%s\n' "${GRN}" "$*" "${RST}"; }
warn()  { printf '%s   [!]  %s%s\n' "${YEL}" "$*" "${RST}"; }
die()   { printf '\n%s[ABORT] %s%s\n' "${BOLD}${RED}" "$*" "${RST}" >&2; exit 1; }

# --- arg parse ---------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --project)   PROJECT="${2:?--project needs a value}"; shift 2 ;;
    --project=*) PROJECT="${1#*=}"; shift ;;
    --env-file)  ENV_FILE="${2:?--env-file needs a value}"; shift 2 ;;
    --env-file=*) ENV_FILE="${1#*=}"; shift ;;
    --seed)      DO_SEED=1; shift ;;
    --no-caddy)  NO_CADDY=1; shift ;;
    --teardown)  DO_TEARDOWN=1; shift ;;
    -h|--help)   grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'; exit 0 ;;
    *)           die "unknown argument: $1 (try --help)" ;;
  esac
done

IS_REAL=0
[ "${PROJECT}" = "legba" ] && IS_REAL=1

# --- compose invocation builder ----------------------------------------------
# Real `legba` project → base file only (back-compat, unchanged volumes).
# Any other project    → base + isolation override (project-scoped volumes).
COMPOSE_FILES=( -f "${COMPOSE_BASE}" )
if [ "${IS_REAL}" -eq 0 ]; then
  COMPOSE_FILES+=( -f "${ISOLATION_OVERRIDE}" )
  # The isolation override reads ${LEGBA_ISO_PROJECT} for the project-scoped
  # volume + scheduler-bind names. Pin it to the chosen project.
  export LEGBA_ISO_PROJECT="${PROJECT}"
fi

# dc = docker compose, project + env-file + file list, for the chosen stack.
dc() {
  docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" "${COMPOSE_FILES[@]}" "$@"
}
# dc_runtime = dc with the canonical `runtime` profile (pulls dapr + ui).
dc_runtime() {
  dc --profile runtime "$@"
}

# Registrar runner: a one-off `compose run` against the registry image,
# project-aware (honors -p), with the app DB pinned + the repo mounted so the
# (un-baked) scripts/ are reachable. --entrypoint python overrides the image's
# server entrypoint. --no-deps so it does not (re)start the whole stack.
APP_DB="${LEGBA_DATA_PG_DB:-legba}"
run_registrar() {
  # usage: run_registrar <script-relative-path> [args...]
  local script="$1"; shift || true
  dc run --rm --no-deps \
    -v "${REPO_ROOT}:${REPO_ROOT}" -w "${REPO_ROOT}" \
    -e "LEGBA_DATA_PG_DB=${APP_DB}" \
    -e "LEGBA_REGISTRY_URL=http://legba-registry:8090/api/v1/registry" \
    --entrypoint python legba-registry "${script}" "$@"
}

# Wait until a one-off psql/health probe succeeds, or time out.
wait_for() {
  # usage: wait_for <label> <max_seconds> <cmd...>
  local label="$1" max="$2"; shift 2
  local i=0
  while [ "${i}" -lt "${max}" ]; do
    if "$@" >/dev/null 2>&1; then ok "${label} ready"; return 0; fi
    i=$((i+2)); sleep 2
  done
  return 1
}

# =============================================================================
# TEARDOWN PATH (--teardown)
# =============================================================================
if [ "${DO_TEARDOWN}" -eq 1 ]; then
  phase "TEARDOWN (project=${PROJECT})"
  if [ "${IS_REAL}" -eq 1 ]; then
    warn "project is the REAL 'legba' stack — refusing 'down -v'."
    info "only-instance rule: the live volumes are NEVER destroyed by this tool."
    info "stopping containers (volumes preserved; scheduler 45s grace honored)..."
    dc_runtime stop
    ok "legba stopped. Volumes intact. Restart with: deploy/deploy.sh (no flags)."
    exit 0
  fi

  # Non-legba: a real teardown IS allowed, but only after RE-PROVING isolation.
  info "re-confirming isolation before destructive 'down -v'..."
  td_render="$(dc_runtime config 2>/dev/null || true)"
  [ -n "${td_render}" ] || die "could not render compose config for ${PROJECT}"
  if printf '%s' "${td_render}" \
       | grep -E 'legba_(postgres|nats|qdrant|redis|caddy|ui|media)_(data|dist|config)' \
       | grep -vqE "${PROJECT}_"; then
    die "isolation re-check FAILED: render references REAL legba_* volumes. Refusing 'down -v'."
  fi
  ok "isolation re-confirmed (only ${PROJECT}_* volumes present)."
  info "destroying the throwaway stack ${PROJECT} (down -v)..."
  dc_runtime down -v
  # Remove the project-scoped scheduler host-bind too.
  if [ -d "deploy/${PROJECT}-scheduler-data" ]; then
    rm -rf "deploy/${PROJECT}-scheduler-data"
    info "removed deploy/${PROJECT}-scheduler-data"
  fi
  ok "teardown complete for ${PROJECT}."
  exit 0
fi

# =============================================================================
# PHASE 1 — PREFLIGHT
# =============================================================================
phase "PHASE 1 — PREFLIGHT (project=${PROJECT}, env=${ENV_FILE})"

command -v docker >/dev/null 2>&1 || die "docker not found on PATH"
docker compose version >/dev/null 2>&1 || die "docker compose v2 not available"
[ -f "${COMPOSE_BASE}" ]   || die "missing ${COMPOSE_BASE} (run from repo root: ${REPO_ROOT})"
[ -f "${BASELINE_SQL}" ]   || die "missing baseline schema ${BASELINE_SQL}"
[ -f "${ENV_FILE}" ]       || die "env file '${ENV_FILE}' not found"
if [ "${IS_REAL}" -eq 0 ]; then
  [ -f "${ISOLATION_OVERRIDE}" ] || die "missing isolation override ${ISOLATION_OVERRIDE}"
fi
ok "docker + compose + compose-files + baseline + env file present"

# Required boot env keys (from the audit's required-env inventory). These must
# be PRESENT (non-empty) in the env file or the stack fails closed at boot.
REQUIRED_KEYS=(
  LEGBA_DATA_REGISTRY_DSN
  LEGBA_DATA_MASTER_KEY
  LEGBA_DATA_PG_HOST LEGBA_DATA_PG_PORT LEGBA_DATA_PG_USER
  LEGBA_DATA_PG_PASSWORD LEGBA_DATA_PG_DB
  LEGBA_DATA_QDRANT_HOST LEGBA_DATA_QDRANT_PORT
  LEGBA_DATA_REDIS_HOST LEGBA_DATA_REDIS_PORT
  LEGBA_DATA_NATS_URL
  LEGBA_DAPR_PG_CONNSTRING
  LEGBA_REGISTRY_API_TOKEN
  LEGBA_GEOCODER_CONTACT_EMAIL
  LEGBA_PUBLIC_DOMAIN
  LEGBA_BASIC_AUTH_HASH
)
# Read keys present (with a non-empty value) in the env file. We match KEY=...
# where the value side is not blank; comments / blank lines are ignored.
missing=()
for k in "${REQUIRED_KEYS[@]}"; do
  if ! grep -Eq "^[[:space:]]*${k}=[[:space:]]*[^[:space:]#]" "${ENV_FILE}"; then
    missing+=( "${k}" )
  fi
done
if [ "${#missing[@]}" -gt 0 ]; then
  warn "env file '${ENV_FILE}' is missing/blank for required boot keys:"
  for k in "${missing[@]}"; do printf '        - %s\n' "${k}"; done
  die "fill these in (.env.example documents each) before deploying."
fi
ok "all ${#REQUIRED_KEYS[@]} required boot keys present in ${ENV_FILE}"

# Pin the app DB from the env file if set there (else default legba).
env_db="$(grep -E '^[[:space:]]*LEGBA_DATA_PG_DB=' "${ENV_FILE}" | tail -1 | cut -d= -f2- | tr -d ' "'"'"'' || true)"
[ -n "${env_db}" ] && APP_DB="${env_db}"
info "app DB pinned to: ${APP_DB}"

# Render the compose config once now (also a syntactic gate on the files).
dc_runtime config >/dev/null 2>&1 || die "compose config failed to render — check ${COMPOSE_BASE} / override / env"
ok "compose config renders cleanly"

# =============================================================================
# PHASE 2 — ISOLATION GATE (non-legba only) — the data-bleed firewall
# =============================================================================
if [ "${IS_REAL}" -eq 0 ]; then
  phase "PHASE 2 — ISOLATION GATE (THE DATA-BLEED FIREWALL)"
  info "rendering '${PROJECT}' and proving ZERO references to the REAL legba_* state..."
  render="$(dc_runtime config 2>/dev/null || true)"
  [ -n "${render}" ] || die "isolation render produced no output"

  # (a) No REAL named-volume references.
  if printf '%s' "${render}" \
       | grep -E 'legba_(postgres|nats|qdrant|redis|caddy|ui|media)_(data|dist|config)' \
       | grep -vqE "${PROJECT}_"; then
    printf '%s' "${render}" \
      | grep -E 'legba_(postgres|nats|qdrant|redis|caddy|ui|media)_(data|dist|config)' \
      | grep -vE "${PROJECT}_" | sed 's/^/        LEAK> /' >&2
    die "ISOLATION GATE FAILED: render references REAL legba_* volume names."
  fi

  # (b) No REAL scheduler host-bind (./deploy/dapr-scheduler-data).
  if printf '%s' "${render}" | grep -q '/deploy/dapr-scheduler-data'; then
    die "ISOLATION GATE FAILED: render still binds the REAL ./deploy/dapr-scheduler-data."
  fi

  ok "ISOLATION GATE PASSED — zero real-volume hits, zero real scheduler-bind hits."
  info "project-scoped volumes confirmed: ${PROJECT}_postgres_data, _nats_data, _qdrant_data, ..."
  info "scheduler bind confirmed: ./deploy/${PROJECT}-scheduler-data"
else
  phase "PHASE 2 — ISOLATION GATE (skipped: real 'legba' project uses real volumes by design)"
  warn "deploying the REAL 'legba' stack onto its production volumes."
fi

# =============================================================================
# PHASE 3 — SUBSTRATE + DAPR CONTROL PLANE
# =============================================================================
phase "PHASE 3 — SUBSTRATE + DAPR CONTROL PLANE"
info "bringing up substrate (postgres/age, nats, qdrant, redis) + dapr control plane..."
# `--profile runtime` pulls dapr (placement, scheduler-init, scheduler, init-db,
# sidecar). We bring everything up EXCEPT the runtime app for now — the registry
# is needed for HTTP registrars; the runtime boots LAST (nlp_client footgun).
dc_runtime up -d --no-build \
  redis postgres qdrant nats \
  dapr-placement dapr-scheduler-init dapr-scheduler dapr-init-db
ok "substrate + dapr control plane started"

info "waiting for postgres to accept connections..."
wait_for "postgres" 120 \
  dc exec -T postgres pg_isready -U "${LEGBA_DATA_PG_USER:-legba}" \
  || die "postgres did not become ready"

info "waiting for nats / qdrant / redis health..."
wait_for "nats"   60 dc exec -T nats   wget -q -O- http://localhost:8222/healthz || warn "nats health probe inconclusive (continuing)"
ok "substrate healthy"

# =============================================================================
# PHASE 4 — SCHEMA (apply the single baseline, then future migrations)
# =============================================================================
phase "PHASE 4 — SCHEMA"
info "ensuring app DB '${APP_DB}' exists..."
dc exec -T postgres psql -U "${LEGBA_DATA_PG_USER:-legba}" -d postgres -tc \
  "SELECT 1 FROM pg_database WHERE datname='${APP_DB}'" 2>/dev/null | grep -q 1 \
  || dc exec -T postgres psql -U "${LEGBA_DATA_PG_USER:-legba}" -d postgres -c "CREATE DATABASE ${APP_DB}"
ok "app DB '${APP_DB}' present"

# Has the baseline already been applied? (ledger has the 23 pre-seeded rows)
already="$(dc exec -T postgres psql -U "${LEGBA_DATA_PG_USER:-legba}" -d "${APP_DB}" -tAc \
  "SELECT count(*) FROM legba_data_migrations" 2>/dev/null | tr -d '[:space:]' || echo 0)"
if [ "${already:-0}" -ge 1 ]; then
  ok "schema already present (ledger has ${already} rows) — skipping baseline apply (idempotent)"
else
  info "applying single baseline ${BASELINE_SQL} (NOT the 23-file history)..."
  dc exec -T postgres psql -U "${LEGBA_DATA_PG_USER:-legba}" -d "${APP_DB}" -v ON_ERROR_STOP=1 \
    < "${BASELINE_SQL}"
  ok "baseline applied"
fi

info "running future migrations (legba.data.migrate) — no-op while ledger head = 0053..."
run_registrar -m legba.data.migrate || die "migrate failed"
ok "schema at head (no pending future migrations)"

# =============================================================================
# PHASE 5 — REGISTRARS (audit's ordered sequence)
# =============================================================================
phase "PHASE 5 — REGISTRARS"
info "starting the registry (HTTP registrars need it up)..."
dc_runtime up -d --no-build legba-registry
wait_for "registry-healthz" 120 \
  dc exec -T legba-registry curl -fsS "http://localhost:8090${REGISTRY_HEALTH_PATH}" \
  || warn "registry healthz not confirmed via exec (continuing; registrars will surface errors)"

# Ordered registrar sequence — VERBATIM from the audit (§1.3):
#   1 vault → 2 stack → 3 packs(HTTP) → 4 sources → 4b catalog →
#   5 G20 targets → 6 analyst set → 7 deterministic-6 → 8 budget.
info "[1] vault secrets (HTTP)";            run_registrar scripts/bringup_vault_load.py
info "[2] stack components (HTTP)";          run_registrar scripts/bringup_register_stack.py
info "[3] action packs (HTTP, 8 packs)";     run_registrar scripts/bringup_register_action_packs.py
info "[4] shared RSS sources (3, direct)";   run_registrar scripts/bringup_register_sources.py
info "[4b] full no-auth catalog (~46)";      run_registrar scripts/bringup_register_source_catalog.py
info "[5] G20 country targets (x19)";        run_registrar scripts/bringup_register_g20_country_targets.py
info "[6] analyst working set (~21-23)";     run_registrar scripts/bringup_register_analysts.py
info "[7] deterministic analysts (x6, HTTP)"
for det in cross_source_dedup cross_source_coalesce entity_resolution \
           finding_supersession integrity_sweep situation_clustering; do
  run_registrar "scripts/bringup_register_${det}.py"
done
info "[8] budget envelope (direct)";         run_registrar scripts/bringup_set_budget_envelope.py
ok "all registrars complete (idempotent — re-run safe)"

# =============================================================================
# PHASE 6 — SEEDS (optional; BEFORE the runtime boots)
# =============================================================================
if [ "${DO_SEED}" -eq 1 ]; then
  phase "PHASE 6 — SEEDS (knowledge layer, before runtime boot)"
  info "nlp_client footgun: seeds run NOW, before the runtime builds its clients."
  for src in ${SEED_SOURCES}; do
    info "seed: ${src}"
    run_registrar scripts/seed.py --source "${src}"
  done
  ok "seed sequence complete (${SEED_SOURCES})"
else
  phase "PHASE 6 — SEEDS (skipped: pass --seed to load the knowledge layer)"
fi

# =============================================================================
# PHASE 7 — APP UP (registry already up → runtime → ui [→ caddy])
# =============================================================================
phase "PHASE 7 — APP UP"
# --force-recreate the runtime is MANDATORY: if it ever booted before seeding,
# nlp_client is pinned None for the process lifetime. Recreating rebuilds it
# against the now-seeded stack.
if [ "${NO_CADDY}" -eq 1 ]; then
  info "bringing up runtime + ui (caddy SKIPPED: --no-caddy)..."
  dc_runtime up -d --no-build --force-recreate \
    legba-runtime-dapr legba-ui-build
else
  info "bringing up runtime + ui + caddy..."
  dc_runtime up -d --no-build --force-recreate \
    legba-runtime-dapr legba-ui-build legba-caddy
fi
ok "app services started (runtime force-recreated → fresh nlp_client)"

info "waiting for runtime health..."
wait_for "runtime" 120 \
  dc exec -T legba-runtime-dapr curl -fsS "http://localhost:6090/healthz" \
  || warn "runtime health not confirmed (continuing to verify)"

# =============================================================================
# PHASE 8 — BOOT-VERIFY
# =============================================================================
phase "PHASE 8 — BOOT-VERIFY"
VERIFY_FAIL=0

# (a) registry healthz 200
if dc exec -T legba-registry curl -fsS "http://localhost:8090${REGISTRY_HEALTH_PATH}" >/dev/null 2>&1; then
  ok "registry healthz 200"
else
  warn "registry healthz did NOT return 200"; VERIFY_FAIL=1
fi

# (b) nlp_client built (the enrichment-live signal in the boot log)
if dc logs legba-runtime-dapr 2>/dev/null | grep -q 'nlp_client.built'; then
  ok "runtime nlp_client.built (enrichment live)"
else
  warn "runtime boot log shows no nlp_client.built line — enrichment may be off"; VERIFY_FAIL=1
fi
if dc logs legba-runtime-dapr 2>/dev/null | grep -qi 'enrichment_build_failed'; then
  warn "runtime boot log shows enrichment_build_failed"; VERIFY_FAIL=1
fi

# (c) sources registered (the actor roster / a source can poll)
src_count="$(dc exec -T postgres psql -U "${LEGBA_DATA_PG_USER:-legba}" -d "${APP_DB}" -tAc \
  "SELECT count(*) FROM source_descriptors" 2>/dev/null | tr -d '[:space:]' || echo 0)"
if [ "${src_count:-0}" -ge 3 ]; then
  ok "source_descriptors registered: ${src_count} (>= 3 cold-start feeds)"
else
  warn "only ${src_count} source_descriptors registered (expected dozens after catalog)"; VERIFY_FAIL=1
fi

# (d) no pending future migrations
if run_registrar -m legba.data.migrate --dry-run 2>&1 | grep -qiE 'primary: \[\]|0 pending|nothing'; then
  ok "migrate --dry-run reports nothing pending"
else
  info "migrate --dry-run output (review):"
  run_registrar -m legba.data.migrate --dry-run 2>&1 | sed 's/^/        /' || true
fi

# --- summary -----------------------------------------------------------------
phase "SUMMARY"
printf '   project       : %s\n' "${PROJECT}"
printf '   env file      : %s\n' "${ENV_FILE}"
printf '   app DB        : %s\n' "${APP_DB}"
printf '   isolation     : %s\n' "$([ "${IS_REAL}" -eq 1 ] && echo 'real legba volumes' || echo "ISOLATED (${PROJECT}_*)")"
printf '   caddy edge    : %s\n' "$([ "${NO_CADDY}" -eq 1 ] && echo 'skipped (--no-caddy)' || echo 'up')"
printf '   seeds         : %s\n' "$([ "${DO_SEED}" -eq 1 ] && echo "${SEED_SOURCES}" || echo 'skipped')"
printf '   sources       : %s registered\n' "${src_count}"
if [ "${VERIFY_FAIL}" -eq 0 ]; then
  printf '\n%s[PASS] deploy verified — %s is up.%s\n' "${BOLD}${GRN}" "${PROJECT}" "${RST}"
  exit 0
else
  printf '\n%s[FAIL] deploy completed but BOOT-VERIFY found issues (see [!] above).%s\n' "${BOLD}${RED}" "${RST}"
  exit 1
fi
