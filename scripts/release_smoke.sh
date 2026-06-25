#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# release_smoke.sh — minimal deployed-stack smoke set for the release gate.
#
# Verifies a freshly-deployed stack is actually serving + fail-closed. Each
# check is a single curl/psql assertion; the script exits non-zero on the
# first failure so it composes into release_gate.sh. It is READ-ONLY: no
# writes to the DB or any container state.
#
# Checks:
#   1. registry /stack with NO bearer            → 401 (auth required)
#   2. registry /stack with a WRONG bearer       → 403 (fail-closed)
#   3. registry /stack with the configured token → 200 (serving)
#   4. Postgres reachable + migration ledger non-empty
#   5. caddy edge serves the public domain (401 basic_auth at the door)
#
# USAGE:
#   bash scripts/release_smoke.sh
#   LEGBA_REGISTRY_API_TOKEN=... LEGBA_PUBLIC_DOMAIN=legba.example.com \
#     bash scripts/release_smoke.sh
#
# The token + domain are read from the env, falling back to the gitignored
# .env at the repo root (same resolution the bringup scripts use).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY_URL="${LEGBA_REGISTRY_INTERNAL_URL:-http://127.0.0.1:8090}"
STACK_PATH="/api/v1/registry/stack"

# Resolve the bearer token: env first, then .env.
TOKEN="${LEGBA_REGISTRY_API_TOKEN:-}"
if [[ -z "${TOKEN}" && -f "${REPO_ROOT}/.env" ]]; then
  TOKEN="$(grep -E '^LEGBA_REGISTRY_API_TOKEN=' "${REPO_ROOT}/.env" | head -1 | cut -d= -f2- || true)"
fi
DOMAIN="${LEGBA_PUBLIC_DOMAIN:-}"
if [[ -z "${DOMAIN}" && -f "${REPO_ROOT}/.env" ]]; then
  DOMAIN="$(grep -E '^LEGBA_PUBLIC_DOMAIN=' "${REPO_ROOT}/.env" | head -1 | cut -d= -f2- || true)"
fi

FAILED=0
pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; FAILED=1; }

code() { curl -s -o /dev/null -w '%{http_code}' "$@" 2>/dev/null || echo "000"; }

echo ">> Legba release smoke"

# 1. No bearer → 401.
c="$(code "${REGISTRY_URL}${STACK_PATH}")"
[[ "${c}" == "401" ]] && pass "registry no-bearer → 401" || fail "registry no-bearer → ${c} (want 401)"

# 2. Wrong bearer → 403 (fail-closed; B-2).
c="$(code -H 'Authorization: Bearer definitely-wrong-token' "${REGISTRY_URL}${STACK_PATH}")"
[[ "${c}" == "403" ]] && pass "registry wrong-bearer → 403" || fail "registry wrong-bearer → ${c} (want 403)"

# 3. Correct bearer → 200.
if [[ -n "${TOKEN}" ]]; then
  c="$(code -H "Authorization: Bearer ${TOKEN}" "${REGISTRY_URL}${STACK_PATH}")"
  [[ "${c}" == "200" ]] && pass "registry valid-bearer → 200" || fail "registry valid-bearer → ${c} (want 200)"
else
  fail "registry valid-bearer: no LEGBA_REGISTRY_API_TOKEN resolved (set env or .env)"
fi

# 4. Postgres + migration ledger.
if command -v docker >/dev/null 2>&1; then
  n="$(docker exec legba-postgres-1 psql -U legba -d legba -tAc \
        "SELECT count(*) FROM legba_data_migrations" 2>/dev/null || echo "")"
  if [[ "${n}" =~ ^[0-9]+$ && "${n}" -gt 0 ]]; then
    pass "migration ledger applied (${n} migrations)"
  else
    fail "migration ledger empty/unreachable (got '${n}')"
  fi
else
  echo "  SKIP  migration ledger (docker not available)"
fi

# 5. Caddy edge (public domain at the door — 401 basic_auth is the healthy
#    'serving' signal; the cert + vhost resolve).
if [[ -n "${DOMAIN}" ]]; then
  c="$(curl -sk -o /dev/null -w '%{http_code}' --resolve "${DOMAIN}:443:127.0.0.1" \
        "https://${DOMAIN}/" 2>/dev/null || echo "000")"
  if [[ "${c}" == "401" || "${c}" == "200" ]]; then
    pass "caddy edge ${DOMAIN} → ${c}"
  else
    fail "caddy edge ${DOMAIN} → ${c} (want 401/200)"
  fi
else
  echo "  SKIP  caddy edge (no LEGBA_PUBLIC_DOMAIN)"
fi

if [[ "${FAILED}" -ne 0 ]]; then
  echo ">> SMOKE FAILED"
  exit 1
fi
echo ">> SMOKE OK"
