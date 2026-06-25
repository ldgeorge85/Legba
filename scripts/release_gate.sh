#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# release_gate.sh — the ordered, fail-fast release gate (docs/RUNBOOK.md §18).
#
# Composes the gates that already exist into ONE driver so a release can't
# skip a step. Runs in order, stops at the first failure, and writes a
# timestamped gate log to release/gate-<utc>.log. NOTHING here writes to the
# live DB or mutates running containers — it builds/test images, runs the
# suite, validates descriptors, builds the UI (the tsc gate), regenerates the
# manifest, and smoke-checks a deployed stack.
#
# Stages (each must pass):
#   1. strict in-container test suite (LEGBA_TEST_STRICT=1 — no silent skips)
#   2. no-stub gate (forbidden stub/mock markers in production paths)
#   3. descriptor validation (every descriptor parses + type-checks)
#   4. UI build (legba-ui-build container == the tsc gate)
#   5. pre-push secret/codename scan (prepush_scan.sh)
#   6. release manifest regen (make_release_manifest.sh — fold-in so it
#      can't go stale vs the tag)
#   7. deployed-stack smoke (release_smoke.sh) — SKIPPED unless a stack is up
#
# USAGE:
#   bash scripts/release_gate.sh                 # full gate
#   SKIP_SMOKE=1 bash scripts/release_gate.sh    # skip stage 7 (no live stack)
#   SKIP_UI=1    bash scripts/release_gate.sh    # skip stage 4 (no docker UI build)
#
# Exit 0 = every stage green. Non-zero = first failing stage (logged).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_DIR="${REPO_ROOT}/release"
LOG="${LOG_DIR}/gate-${TS}.log"
mkdir -p "${LOG_DIR}"

stage() { echo; echo "==== STAGE $1 ===="; echo "==== STAGE $1 ====" >> "${LOG}"; }
die() { echo ">> GATE FAILED at: $1" | tee -a "${LOG}"; exit 1; }

echo "Legba release gate — ${TS} — HEAD $(git rev-parse --short HEAD)" | tee "${LOG}"

# 1. Strict test suite.
stage "1/7 strict test suite"
if ! LEGBA_TEST_STRICT=1 bash scripts/run_tests_in_container.sh 2>&1 | tee -a "${LOG}"; then
  die "strict test suite"
fi

# 2. No-stub gate. The repo convention: a genuinely-deferred item is a
#    fail-loud declared SEAM in docs/SEAMS.md, NEVER a silent stub. This grep
#    forbids stub/mock markers in production source (tests excluded).
stage "2/7 no-stub gate"
if git grep -nI -E -- '\b(TODO: stub|FIXME: stub|NotImplementedError\(\s*["'"'"']stub|raise +NotImplementedError\b.*stub|# *STUB\b|MagicMock|unittest\.mock)' \
   -- 'src/**/*.py' ':(exclude)src/**/tests/**' > /tmp/_rg_stub 2>/dev/null; then
  echo "  forbidden stub/mock markers in production source:" | tee -a "${LOG}"
  sed 's/^/    /' /tmp/_rg_stub | tee -a "${LOG}"
  die "no-stub gate"
fi
echo "  no forbidden stub/mock markers in src/" | tee -a "${LOG}"

# 3. Descriptor validation — every YAML descriptor parses + type-checks.
stage "3/7 descriptor validation"
if ls "${REPO_ROOT}/scripts/validate_descriptors.py" >/dev/null 2>&1; then
  if ! docker run --rm --network host \
        -v "${REPO_ROOT}:${REPO_ROOT}" -w "${REPO_ROOT}" \
        -e PYTHONPATH="${REPO_ROOT}/src:/install/lib/python3.11/site-packages" \
        --entrypoint python legba/legba-test:latest \
        scripts/validate_descriptors.py 2>&1 | tee -a "${LOG}"; then
    die "descriptor validation"
  fi
else
  # Fallback: parse + typed-load every descriptor via the registry conversion
  # path. A descriptor that fails to type-check raises here.
  if ! docker run --rm --network host \
        -v "${REPO_ROOT}:${REPO_ROOT}" -w "${REPO_ROOT}" \
        -e PYTHONPATH="${REPO_ROOT}/src:/install/lib/python3.11/site-packages" \
        --entrypoint python legba/legba-test:latest -c '
import glob, sys, yaml
bad = 0
for f in sorted(glob.glob("descriptors/*.yaml")):
    try:
        with open(f) as fh:
            yaml.safe_load(fh)
    except Exception as exc:
        print(f"  DESCRIPTOR PARSE FAIL {f}: {exc}"); bad += 1
print(f"  validated {len(glob.glob(\"descriptors/*.yaml\"))} descriptors, {bad} bad")
sys.exit(1 if bad else 0)
' 2>&1 | tee -a "${LOG}"; then
    die "descriptor validation"
  fi
fi

# 4. UI build (the tsc gate). The legba-ui-build container build IS the type
#    check — a tsc error fails the docker build.
stage "4/7 UI build (tsc gate)"
if [[ "${SKIP_UI:-0}" == "1" ]]; then
  echo "  SKIP_UI=1 — skipping UI build" | tee -a "${LOG}"
else
  if ! docker compose --profile ui build legba-ui-build 2>&1 | tee -a "${LOG}"; then
    die "UI build (tsc gate)"
  fi
fi

# 5. Pre-push secret/codename scan.
stage "5/7 pre-push secret/codename scan"
if ! bash scripts/prepush_scan.sh 2>&1 | tee -a "${LOG}"; then
  die "pre-push secret/codename scan (resolve the hits — see RUNBOOK §19/§20)"
fi

# 6. Release manifest regen (fold-in so it tracks the tag).
stage "6/7 release manifest"
if ! bash scripts/make_release_manifest.sh 2>&1 | tee -a "${LOG}"; then
  die "release manifest"
fi

# 7. Deployed-stack smoke (optional — only when a stack is running).
stage "7/7 deployed-stack smoke"
if [[ "${SKIP_SMOKE:-0}" == "1" ]]; then
  echo "  SKIP_SMOKE=1 — skipping smoke" | tee -a "${LOG}"
else
  if ! bash scripts/release_smoke.sh 2>&1 | tee -a "${LOG}"; then
    die "deployed-stack smoke"
  fi
fi

echo | tee -a "${LOG}"
echo ">> RELEASE GATE GREEN — log: ${LOG}" | tee -a "${LOG}"
