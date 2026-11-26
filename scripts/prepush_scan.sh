#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# prepush_scan.sh — mechanical pre-push hygiene gate.
#
# The repo (github.com/ldgeorge85/legba) is PUBLIC. Before any push this
# scan exits NON-ZERO on anything that must not reach the public remote:
#
#   1. Internal TERMS in tracked file content: the generic patterns in the
#      tracked .prepush_terms plus the deployment's private terms (codenames,
#      private host names, vendor names) in the git-ignored .prepush_terms.local.
#   2. Operator DOMAIN in tracked file content (civislux).
#   3. A tracked .env / secrets file (must stay gitignored).
#   4. Private-key material (BEGIN ... PRIVATE KEY) in tracked content.
#   5. Bearer/API/secret tokens assigned a long literal value.
#   6. High-entropy strings (long base64/hex literals) — heuristic.
#   7. Non-neutral git author/committer identity on commits about to push.
#   8. A tracked planning/ file (internal tracking must stay ignored).
#   9. A tracked corpora/ file (operational corpus DATA must stay ignored).
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

# 1. Internal terms in tracked content. Two DATA files, same format (one
#    extended regex per line; '#'/blank lines ignored), joined into one
#    case-insensitive alternation:
#      .prepush_terms        TRACKED — generic patterns only (private-network
#                            name shapes); a public file must not list the
#                            deployment's own names, or it publishes them.
#      .prepush_terms.local  GIT-IGNORED — the deployment's private terms
#                            (prior-host codenames, private host names and
#                            host-name fragments, vendor and model codenames).
#                            Read when present; a checkout without it runs the
#                            generic patterns only and prints a NOTE.
#    Both files are EXCLUDED from the scan (they necessarily contain the
#    terms). A missing/empty TRACKED file is a hard FAIL — a silent empty regex
#    would neuter the whole gate.
TERMS_FILE="${REPO_ROOT}/.prepush_terms"
LOCAL_TERMS_FILE="${PREPUSH_LOCAL_TERMS:-${REPO_ROOT}/.prepush_terms.local}"
_terms() { grep -vE '^[[:space:]]*(#|$)' "$1" 2>/dev/null | sed -E 's/[[:space:]]+$//; s/^[[:space:]]+//' | grep -v '^$'; }
GENERIC_RE="$(_terms "${TERMS_FILE}" | paste -sd '|' -)"
LOCAL_RE=""
[[ -r "${LOCAL_TERMS_FILE}" ]] && LOCAL_RE="$(_terms "${LOCAL_TERMS_FILE}" | paste -sd '|' -)"
CODENAME_RE="${GENERIC_RE}${LOCAL_RE:+${GENERIC_RE:+|}${LOCAL_RE}}"
section "1. internal terms (.prepush_terms + .prepush_terms.local) in tracked content"
if [[ -z "${LOCAL_RE}" ]]; then
  echo "  NOTE  no private term list at ${LOCAL_TERMS_FILE} — generic patterns only"
fi
if [[ -z "${GENERIC_RE}" ]]; then
  report prepush-terms "missing/empty ${TERMS_FILE} — term scan cannot run"
elif git grep -nI -iE "${CODENAME_RE}" -- . \
     ':(exclude)scripts/prepush_scan.sh' ':(exclude)docs/RUNBOOK.md' \
     ':(exclude).prepush_terms' ':(exclude).prepush_terms.local' >/tmp/_ps_codename 2>/dev/null; then
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
  # Second EXACT-STRING carve-out (release prepush audit, 2026-11): five
  # self-describing fake test tokens (x4/b3 bearer-gate fixtures, the c1 usage
  # test token, the fictional series key) and two CODE expressions the keyword
  # pattern reads as a value (a function call and a class instantiation).
  # Fixed strings — any other value on the same line is still caught.
  # Narrow EXACT-STRING carve-out (release-gate prepush audit, 2026-08):
  # three self-describing FAKE test-fixture tokens, confirmed not real
  # credentials — none of the existing placeholder markers below
  # (example/dummy/fake/...) happen to appear in their names, so the pattern
  # filter alone doesn't clear them. Fixed-string match, not a regex, so any
  # OTHER token value stays caught even if it sat on the same line.
  while IFS= read -r line; do report token-literal "${line}"; done < <(
    grep -vE '\$\{|\$[A-Z_]|<[^>]+>|example|changeme|placeholder|your-|xxxx|REDACTED|\.invalid|getenv|os\.environ|environ\.get|process\.env|EXAMPLE|dummy|fake|TODO|"dev"|=dev$|:-dev|"LEGBA_[A-Z_]+"' /tmp/_ps_tok \
      | grep -vF -e "super-secret-registry-token" -e "disconnect-test-token" -e "ws-subproto-token" \
      | grep -vF -e "x4-gate-operator-" -e "x4-gate-service-" -e "b3-operator-token-" \
                 -e "c1-usage-judging-test-token" -e "k3y-fict1onal-0123456789" \
                 -e "passwordFromFile(" -e "LocalPasswordProvider()"
  )
fi

# 6. High-entropy literals (long base64/hex runs) — heuristic, allowlist-filtered.
section "6. high-entropy literals (heuristic)"
if git grep -nI -E -- "['\"][A-Za-z0-9+/]{40,}={0,2}['\"]|['\"][0-9a-fA-F]{48,}['\"]" \
   . ':(exclude)scripts/prepush_scan.sh' ':(exclude)legba-ui-v3/package-lock.json' \
   ':(exclude)deploy/baseline/0001_baseline.sql' \
   ':(exclude)*.svg' ':(exclude)*.png' >/tmp/_ps_ent 2>/dev/null; then
  # Narrow PATH+CONTEXT carve-out (release-gate prepush audit, 2026-08):
  # scripts/voice4_flip/_flip_common.py's INTENDED_SHA256 / HELD_SHA256
  # pinned-digest block (see the file's own "WHY PIN THEM HERE" docstring) is
  # a content-integrity check, not a secret. Scoped to that exact file AND
  # the exact dict-entry / bare-hex-literal shape those two constants use —
  # nothing else in the file, or anywhere else, is exempted by it.
  FLIP_DIGEST_RE='^scripts/voice4_flip/_flip_common\.py:[0-9]+: *("[a-z_]+": *)?"[0-9a-f]{64}",?$'
  # Second narrow PATH+CONTEXT carve-out (release-gate prepush audit, 2026-09):
  # scripts/load_unit_reference.py's V4_PACKET_SHA is the sha256 of the PROGRAM
  # 1 v4 graded packet and half the uniqueness key the calibration row is
  # written under (see that constant's own "WHY THIS DIGEST IS PINNED HERE"
  # comment) — a provenance record of a measurement, not a credential. Scoped to
  # that exact file AND that exact variable's assignment line; no other literal
  # in the file, or anywhere else, is exempted by it.
  UNITREF_PACKET_RE='^scripts/load_unit_reference\.py:[0-9]+:V4_PACKET_SHA = "[0-9a-f]{64}"$'
  # Third narrow PATH+CONTEXT carve-out (release prepush audit, 2026-11): the
  # bake-off scripts pin the sha256 of the prompt and of the design file they
  # were measured with — content-integrity pins, not credentials. Scoped to
  # those two files AND those two variables' assignment lines.
  BAKEOFF_PIN_RE='^scripts/bakeoff/(arm_open|design)\.py:[0-9]+:(RAW_PROMPT_SHA256|PINNED_SHA256) = "[0-9a-f]{64}"$'
  while IFS= read -r line; do report high-entropy "${line}"; done < <(
    grep -vE 'sha256-|sha512-|integrity|test|fixture|mock|example|sample|hash|digest|did:key:|base64|encode|decode|ALPHABET|alphabet|charset|/[a-z]+/[a-z]+/' /tmp/_ps_ent \
      | grep -vE "${FLIP_DIGEST_RE}" \
      | grep -vE "${UNITREF_PACKET_RE}" \
      | grep -vE "${BAKEOFF_PIN_RE}"
  )
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

# 7b. Internal terms in the ABOUT-TO-PUSH commit MESSAGES.
#     `git grep` (section 1) only sees the tree; commit bodies ship in
#     history too. This is a NON-FATAL WARN, not a hard fail: the release path
#     is a clean-slate squash-before-push (see RUNBOOK §20) which discards every
#     range message and writes ONE fresh commit — so these resolve at push time.
#     It is surfaced so a NON-squash push is never made blind to a message leak.
section "7b. internal terms in commit messages (${RANGE}) — WARN"
if [[ -n "${CODENAME_RE}" ]]; then
  MSG_HITS="$(git log "${RANGE}" --format='%H %s%n%b' 2>/dev/null \
    | grep -inE "${CODENAME_RE}" || true)"
  if [[ -n "${MSG_HITS}" ]]; then
    echo "  WARN  commit messages name internal terms — squash-before-push resolves; reword first if NOT squashing:"
    printf '%s\n' "${MSG_HITS}" | sed 's/^/    /'
  else
    echo "  clean"
  fi
fi

# 8. Tracked planning/ files (internal tracking must stay ignored).
section "8. tracked planning/ files"
while IFS= read -r f; do report tracked-planning "${f}"; done < <(git ls-files planning/ 2>/dev/null)

# 9. Tracked corpora/ files. The operational corpus DATA (world_context /
#    Factbook / license-bearing chunks) is the #1 "never ship" directory
#    (.gitignore), but had no gate backstop — a `git add -f` could sneak it in.
section "9. tracked corpora/ files"
while IFS= read -r f; do report tracked-corpora "${f}"; done < <(git ls-files corpora/ 2>/dev/null)

# 10. The README front door. The centred logo block at the top of README.md is
#     the project's front page; a rewrite of the file once
#     dropped it, and the squash carried that to the public main. It never ships
#     without the logo again: the check reads the file as it is TRACKED (what a
#     push would carry), not the working copy.
section "10. README logo block present"
if ! git show HEAD:README.md 2>/dev/null | grep -q 'src="logo_small.png"'; then
  report readme-logo-missing "README.md at HEAD has no <img src=\"logo_small.png\"> block"
fi
for f in logo_small.png logo.png; do
  git ls-files --error-unmatch "${f}" >/dev/null 2>&1 || report readme-logo-asset-untracked "${f}"
done

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
