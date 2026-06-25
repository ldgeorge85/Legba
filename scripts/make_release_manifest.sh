#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# make_release_manifest.sh — emit a reproducible release manifest.
#
# WHY: docker-compose pins the third-party substrate images to floating tags
# (apache/age:latest, qdrant:latest, busybox:latest)
# and pyproject/package.json use range constraints. That keeps dev velocity
# but means "what exactly is running" is not captured anywhere. This script
# freezes the answer AT RELEASE-TAG TIME into a single committed artifact
# (RELEASE.md is the human doc; this emits the machine-checkable manifest):
#
#   * resolved image digests (sha256) for every compose image actually present
#   * pip freeze from the built runtime image (the real installed set)
#   * the UI npm lockfile hash (package-lock.json)
#   * the applied migration baseline (the data-migration ledger HEAD)
#   * the git commit + tag the manifest was cut from
#   * the smoke-command set (release_smoke.sh) to re-verify the stack
#
# USAGE:
#   bash scripts/make_release_manifest.sh                 # → release/manifest-<gitsha>.txt
#   OUT=/tmp/m.txt bash scripts/make_release_manifest.sh  # custom output path
#
# It is READ-ONLY against the live stack (docker images / inspect, git, a
# pip freeze inside a throwaway container). It never touches the DB or any
# running container's state. Fold a regen into the release gate so the
# manifest can't go stale relative to the tag (see docs/RUNBOOK.md §18).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
GIT_TAG="$(git -C "${REPO_ROOT}" describe --tags --exact-match 2>/dev/null || echo '(untagged)')"
OUT="${OUT:-${REPO_ROOT}/release/manifest-${GIT_SHA}.txt}"
RUNTIME_IMAGE="${RUNTIME_IMAGE:-legba/legba-runtime-dapr:latest}"

mkdir -p "$(dirname "${OUT}")"

# Images referenced by the compose file (dedup, ignore comments).
mapfile -t IMAGES < <(
  grep -hE '^\s*image:' "${REPO_ROOT}/docker-compose.yml" \
    | sed -E 's/^\s*image:\s*//; s/\s*#.*$//' | sort -u
)

{
  echo "# Legba release manifest"
  echo "# Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# Git commit: ${GIT_SHA}"
  echo "# Git tag:    ${GIT_TAG}"
  echo
  echo "## image digests (resolved sha256; '<not-pulled>' = image absent locally)"
  for img in "${IMAGES[@]}"; do
    digest="$(docker image inspect "${img}" --format '{{join .RepoDigests "\n"}}' 2>/dev/null | head -1 || true)"
    id="$(docker image inspect "${img}" --format '{{.Id}}' 2>/dev/null || true)"
    if [[ -n "${digest}" ]]; then
      echo "${img}  ${digest}"
    elif [[ -n "${id}" ]]; then
      echo "${img}  (no RepoDigest; local id ${id})"
    else
      echo "${img}  <not-pulled>"
    fi
  done

  echo
  echo "## pip freeze (from ${RUNTIME_IMAGE})"
  if docker image inspect "${RUNTIME_IMAGE}" >/dev/null 2>&1; then
    docker run --rm --entrypoint python "${RUNTIME_IMAGE}" -m pip freeze 2>/dev/null \
      | sort || echo "<pip freeze failed>"
  else
    echo "<runtime image ${RUNTIME_IMAGE} not present — build it first>"
  fi

  echo
  echo "## UI lockfile baseline (package-lock.json sha256)"
  LOCK="${REPO_ROOT}/legba-ui-v3/package-lock.json"
  if [[ -f "${LOCK}" ]]; then
    sha256sum "${LOCK}" | sed "s#${REPO_ROOT}/##"
  else
    echo "<legba-ui-v3/package-lock.json not found>"
  fi

  echo
  echo "## migration baseline (data-migration files; ledger HEAD is verified at deploy)"
  ls -1 "${REPO_ROOT}/src/legba/data/migrations/"*.sql 2>/dev/null \
    | xargs -r -n1 basename | sort || echo "<no migration files found>"
  echo "# Ledger table: legba_data_migrations  (verify applied set per RUNBOOK §3)"

  echo
  echo "## smoke verification"
  echo "# Re-verify a deployed stack against this manifest with:"
  echo "#   bash scripts/release_smoke.sh"
} > "${OUT}"

echo ">> Release manifest written to ${OUT}" >&2
echo "${OUT}"
