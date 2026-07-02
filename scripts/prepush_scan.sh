#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# prepush_scan.sh — mechanical pre-push hygiene gate.
#
# The repo (github.com/ldgeorge85/legba) is PUBLIC. Before any push this
# scan exits NON-ZERO on anything that must not reach the public remote:
#
#   1. Prior-host CODENAME in tracked file content (terms in .prepush_terms).
#   2. Operator DOMAIN in tracked file content (civislux).
#   3. A tracked .env / secrets file (must stay gitignored).
#   4. Private-key material (BEGIN ... PRIVATE KEY) in tracked content.
#   5. Bearer/API/secret tokens assigned a long literal value.
#   6. High-entropy strings (long base64/hex literals) — heuristic.
#   7. Non-neutral git author/committer identity on commits about to push.
#   8. A tracked planning/ file (internal tracking must stay ignored).
#
# It scans TRACKED CONTENT (`git grep`) + the about-to-push commit range —
# never the untracked vault/.env (those are correctly ignored). Allowlisted
# legitimate hits (e.g. the public GitHub URL, the AGPL header) are filtered.
#
# NOTE on `mnemosyne`: that is an INTENDED federation component name (the
# A2A trust-query peer service), NOT a stray codename — so it is deliberately
# NOT scanned for. See docs/RUNBOOK.md §19 (codename scan findings).
#
# USAGE:
#   bash scripts/prepush_scan.sh                 # scan vs origin/main..HEAD
#   BASE=origin/main bash scripts/prepush_scan.sh
#   gitleaks detect ... && bash scripts/prepush_scan.sh   # optional gitleaks first
#
# Exit 0 = clean. Exit 1 = at least one finding (printed). Optional gitleaks:
# if `gitleaks` is on PATH it is run too (best-effort; its findings also fail).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"
BASE="${BASE:-origin/main}"

FOUND=0
report() { echo "  HIT [$1] $2"; FOUND=1; }
section() { echo ">> ${1}"; }

# 1. Prior-host codename(s) in tracked content. The term list lives in the
#    .prepush_terms DATA file (one term per line; '#'/blank lines ignored) —
#    extracted from the old inline list so the terms are maintained as data, not
#    code. All terms are distinctive with zero false-positive risk. The regex
#    alternation is built from that file; the file itself is EXCLUDED from the
#    scan (it necessarily contains the terms). An empty/missing terms file is a
#    hard FAIL — a silent empty regex would neuter the whole codename gate.
TERMS_FILE="${REPO_ROOT}/.prepush_terms"
CODENAME_RE="$(grep -vE '^[[:space:]]*(#|$)' "${TERMS_FILE}" 2>/dev/null \
  | sed -E 's/[[:space:]]//g' | grep -v '^$' | paste -sd '|' -)"
section "1. codename (from .prepush_terms) in tracked content"
if [[ -z "${CODENAME_RE}" ]]; then
  report prepush-terms "missing/empty ${TERMS_FILE} — codename scan cannot run"
elif git grep -nI -iE "${CODENAME_RE}" -- . \
     ':(exclude)scripts/prepush_scan.sh' ':(exclude)docs/RUNBOOK.md' \
     ':(exclude).prepush_terms' >/tmp/_ps_codename 2>/dev/null; then
  while IFS= read -r line; do report codename "${line}"; done < /tmp/_ps_codename
fi

# 2. Operator domain in tracked content.
section "2. operator domain (civislux) in tracked content"
# Allowlist: legba@civislux.us is the operator's INTENTIONAL public contact
# address (the README "Contact" section) — permitted. Every OTHER civislux
# usage (e.g. a <codename>.civislux.us infra hostname) is still reported, even on
# the same line: the contact address is stripped first, then the residual is
# re-checked for any remaining civislux occurrence.
if git grep -nI -i 'civislux' -- . ':(exclude)scripts/prepush_scan.sh' ':(exclude)docs/RUNBOOK.md' >/tmp/_ps_domain 2>/dev/null; then
  while IFS= read -r line; do
    if printf '%s' "${line}" | sed -E 's/legba@civislux\.us//Ig' | grep -qi 'civislux'; then
      report domain "${line}"
    fi
  done < /tmp/_ps_domain
fi

# 3. Tracked .env / secrets files.
section "3. tracked .env / secret files"
while IFS= read -r f; do report tracked-secret-file "${f}"; done < <(git ls-files \
  | grep -E '(^|/)\.env$|(^|/)\.env\.[^e].*|\.legba_signing_key$|\.legba_bearer_token$|(^|/)vault(/|$)|master_key.*\.txt$|secrets?\.(ya?ml|json|txt)$' \
  | grep -vE '\.env\.example$|\.env\.claude\.example$')

# 4. Private-key material in tracked content (real keys only — test fixtures
#    that embed FAKE/EXAMPLE/DUMMY placeholder bodies are allowlisted).
section "4. private-key blocks in tracked content"
if git grep -nI -E -- '-----BEGIN ([A-Z ]+ )?PRIVATE KEY-----' . ':(exclude)scripts/prepush_scan.sh' >/tmp/_ps_pk 2>/dev/null; then
  while IFS= read -r line; do report private-key "${line}"; done < <(grep -viE 'FAKE|EXAMPLE|DUMMY|PLACEHOLDER|REDACTED|test|fixture|mock' /tmp/_ps_pk)
fi

# 5. Token assignments with a long literal value (PASS/SECRET/TOKEN/API_KEY).
#    Flag a credential keyword assigned a >=16-char value that is NOT an env
#    interpolation (${...} / $VAR), a placeholder, or a known-safe default.
section "5. PASS|SECRET|TOKEN|API_KEY assigned a long literal"
if git grep -nI -E -- \
   '(PASS(WORD)?|SECRET|TOKEN|API_?KEY)["'"'"']?\s*[:=]\s*["'"'"']?[A-Za-z0-9/+_.-]{16,}' \
   . ':(exclude)scripts/prepush_scan.sh' >/tmp/_ps_tok 2>/dev/null; then
  while IFS= read -r line; do report token-literal "${line}"; done < <(grep -vE '\$\{|\$[A-Z_]|<[^>]+>|example|changeme|placeholder|your-|xxxx|REDACTED|\.invalid|getenv|os\.environ|environ\.get|process\.env|EXAMPLE|dummy|fake|TODO|"dev"|=dev$|:-dev' /tmp/_ps_tok)
fi

# 6. High-entropy literals (long base64/hex runs) — heuristic, allowlist-filtered.
section "6. high-entropy literals (heuristic)"
if git grep -nI -E -- "['\"][A-Za-z0-9+/]{40,}={0,2}['\"]|['\"][0-9a-fA-F]{48,}['\"]" \
   . ':(exclude)scripts/prepush_scan.sh' ':(exclude)legba-ui-v3/package-lock.json' \
   ':(exclude)deploy/baseline/0001_baseline.sql' \
   ':(exclude)*.svg' ':(exclude)*.png' >/tmp/_ps_ent 2>/dev/null; then
  while IFS= read -r line; do report high-entropy "${line}"; done < <(grep -vE 'sha256-|sha512-|integrity|test|fixture|mock|example|sample|hash|digest|did:key:|base64|encode|decode|ALPHABET|alphabet|charset|/[a-z]+/[a-z]+/' /tmp/_ps_ent)
fi

# 7. Non-neutral commit identity on the about-to-push range.
section "7. commit identity on ${BASE}..HEAD"
if git rev-parse --verify "${BASE}" >/dev/null 2>&1; then
  RANGE="${BASE}..HEAD"
else
  echo "  NOTE  base ref '${BASE}' not found — scanning all of HEAD's identities"
  RANGE="HEAD"
fi
# Allow only the neutral release identities.
BAD_IDENT="$(git log "${RANGE}" --format='%ae%n%ce' 2>/dev/null \
  | sort -u \
  | grep -viE '^(dev@legba\.(invalid|local)|noreply@|.*@users\.noreply\.github\.com)$' || true)"
if [[ -n "${BAD_IDENT}" ]]; then
  while IFS= read -r ident; do
    [[ -n "${ident}" ]] && report commit-identity "${ident} (non-neutral author/committer in ${RANGE})"
  done <<< "${BAD_IDENT}"
fi

# 8. Tracked planning/ files (internal tracking must stay ignored).
section "8. tracked planning/ files"
while IFS= read -r f; do report tracked-planning "${f}"; done < <(git ls-files planning/ 2>/dev/null)

# Optional gitleaks pass.
section "gitleaks (optional)"
if command -v gitleaks >/dev/null 2>&1; then
  if ! gitleaks detect --no-banner --redact --source "${REPO_ROOT}" >/tmp/_ps_gl 2>&1; then
    echo "  gitleaks reported findings:"; sed 's/^/    /' /tmp/_ps_gl; FOUND=1
  else
    echo "  gitleaks clean"
  fi
else
  echo "  gitleaks not installed — skipping (install for deep entropy/regex scan)"
fi

echo
if [[ "${FOUND}" -ne 0 ]]; then
  echo ">> PREPUSH SCAN: FINDINGS — do NOT push. Resolve the hits above."
  exit 1
fi
echo ">> PREPUSH SCAN: CLEAN"
