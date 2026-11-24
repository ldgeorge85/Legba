#!/usr/bin/env bash
# wave_sync_check.sh — the orchestrator's check after a remote wave is rsynced back.
#
# A remote agent works on a synced copy of this tree (planning/WAVES_REMOTE_2026-09-29.md)
# and the operator rsyncs it back with --delete. Before anything is reviewed, merged or
# rolled, this refuses on the ways a round trip can hurt the live host:
#   1. a dirty tree (the agent must end clean on its wave branch);
#   2. a commit in the wave range touching deploy/ (rsync-excluded; shows deleted remotely)
#      or README.md (the logo block — the operator's standing order);
#   3. the logo block missing at HEAD;
#   4. .env keys that differ from the RUNNING runtime container's environment
#      (the 09-23 reversed-sync incident dropped two flags silently);
#   5. a deploy/ tracked file missing on disk;
#   6. a prepush-scan hit.
# Then it prints the wave branch, its commits and the diff stat, and leaves the suite to you.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
BASE="${WAVE_BASE:-portfolio-ingestion-v2}"
RUNTIME="${WAVE_RUNTIME_CONTAINER:-legba-legba-runtime-dapr-1}"
fail=0
say() { printf '%s\n' "$*"; }
bad() { say "  FAIL: $*"; fail=1; }

say ">> 1. tree"
branch=$(git rev-parse --abbrev-ref HEAD)
if [[ -n "$(git status --porcelain | grep -vE '^ D deploy/')" ]]; then
  git status --short | grep -vE '^ D deploy/' | head -20
  bad "tree is not clean (the agent must commit everything on its wave branch)"
else
  say "  clean; branch=${branch}"
fi

say ">> 2. the wave range against ${BASE}"
if ! git rev-parse --verify -q "${BASE}" >/dev/null; then bad "base branch ${BASE} not found"; fi
range="${BASE}..HEAD"
n=$(git rev-list --count "${range}" 2>/dev/null || echo 0)
say "  ${n} commit(s) in ${range}"
git log --format='  %h %an %s' "${range}" | head -40
if git diff --name-only "${range}" | grep -qE '^deploy/'; then bad "a commit in the range touches deploy/ (rsync-excluded live state)"; fi
if git diff --name-only "${range}" | grep -qxE 'README.md'; then bad "a commit in the range touches README.md"; fi
if git log --format='%b' "${range}" | grep -qiE 'co-authored-by|generated with|claude\.ai/code'; then bad "a commit in the range carries a trailer"; fi

say ">> 3. README logo block at HEAD"
git show HEAD:README.md | grep -q 'src="logo_small.png"' && say "  present" || bad "logo block missing at HEAD"

say ">> 4. .env keys vs the running runtime container"
if [[ -f .env ]] && docker inspect "${RUNTIME}" >/dev/null 2>&1; then
  miss=0
  while IFS= read -r line; do
    key=${line%%=*}; val=${line#*=}; val=${val%\"}; val=${val#\"}
    val=${val//\$\$/\$}   # compose interpolates a doubled dollar to one (the bcrypt hash; the 06-04 push incident)
    live=$(docker exec "${RUNTIME}" printenv "${key}" 2>/dev/null || true)
    if [[ -n "${live}" && "${live}" != "${val}" ]]; then say "  differs: ${key}"; miss=1; fi
  done < <(grep -E '^[A-Z_][A-Z0-9_]*=' .env)
  [[ ${miss} -eq 0 ]] && say "  every .env key present in the container matches" || bad ".env differs from the running container (see keys above)"
else
  say "  skipped (.env or the runtime container not found)"
fi

say ">> 5. deploy/ tracked files on disk"
gone=0
while IFS= read -r f; do [[ -e "$f" ]] || { say "  missing: $f"; gone=1; }; done < <(git ls-files deploy)
[[ ${gone} -eq 0 ]] && say "  all present" || bad "deploy/ tracked files missing on disk — the sync deleted excluded paths?"

say ">> 6. prepush scan"
if bash scripts/prepush_scan.sh >/tmp/_wave_scan.log 2>&1; then say "  clean"; else
  grep -E 'HIT' /tmp/_wave_scan.log | grep -vE 'commit-identity' | head -10
  if grep -E 'HIT' /tmp/_wave_scan.log | grep -vqE 'commit-identity'; then bad "prepush scan hits (identity hits are expected on the working line)"; else say "  only the expected commit-identity hits"; fi
fi

say ">> diff stat"
git diff --stat "${range}" | tail -25

if [[ ${fail} -ne 0 ]]; then say ">> WAVE SYNC CHECK: REFUSED — fix the FAIL lines before review"; exit 1; fi
say ">> WAVE SYNC CHECK: OK — next: LEGBA_REPO_ROOT=\$PWD bash scripts/run_tests_in_container.sh (the whole suite), then review the report"
