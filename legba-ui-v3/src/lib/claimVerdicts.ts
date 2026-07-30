/**
 * claimVerdicts — per-citation-chip verify verdicts (P1-8 / P2-4), pure + DOM-free.
 *
 * WHAT THE PAYLOAD ACTUALLY CARRIES (src/legba/data/provenance/verify.py,
 * `FaithfulnessReport.as_dict`): the finding-level `verification` block records
 * the POOLED score (`faithfulness_score`, `checkable_claims`,
 * `supported_claims`, `judge_status`) plus `unsupported_spans` — one entry per
 * FLAGGED claim: `{text, reason, markers}`, where `markers` lists the citation
 * ordinals the flagged claim carried (unit `[N]` signal indices, or
 * composition `[[ref:N]]` sub-claim ordinals).
 *
 * P2-4 additionally persists `claim_verdicts` — the FULL per-claim ledger
 * (`{text, markers, verdict: 'supported'|'hard_fail'|'soft_fail', reason}`),
 * one row per GRADED claim including supported ones (previously recorded
 * nowhere — the citation-hover finding this module now closes). Advisory-only
 * flags (`hedge_laundering` / `double_counted`) are deliberately NOT ledger
 * rows — they annotate a claim that is itself recorded (typically supported)
 * — so they still live ONLY in `unsupported_spans`.
 *
 * So the honest per-chip verdict, worst-to-best:
 *   * a span with this chip's ordinal in its `markers` → that claim's flag
 *     (contradicted / unsupported / hedge-laundering / …), with the flagged
 *     claim text to show — checked FIRST so an advisory flag is never
 *     shadowed by a ledger row that (correctly) also marks the same claim
 *     `supported`;
 *   * no flagged span names this chip, but the persisted `claim_verdicts`
 *     LEDGER names it `supported` → "supported" — a real, backed verdict,
 *     never fabricated (this is the case the ledger newly makes possible);
 *   * no flagged span, no ledger row (ledger absent — a pre-P2-4 critique —
 *     or present but silent on this ordinal) while the LLM judge RAN → "not
 *     flagged" (the legacy, vaguer fallback: every flagged checkable claim is
 *     named, so absence is meaningful, but we cannot say "supported" without
 *     the ledger);
 *   * the judge did not run (deterministic floor only) or there is no verify
 *     block → "claim-level verdict not recorded" — stated, never fabricated.
 */

// ---------------------------------------------------------------------------
// Reason vocabulary — superset of claimsModel's why-not labels, covering every
// reason `UnsupportedSpan.reason` can carry (verify.py docstring).
// ---------------------------------------------------------------------------

export const CLAIM_REASON_LABELS: Record<string, string> = {
  no_citation: 'no citation',
  unresolved_citation: 'citation resolves to nothing',
  judge_unsupported: 'unsupported by the verify judge',
  judge_contradicted: 'contradicted by the verify judge',
  double_counted: 'double-counted evidence (cited sub-claims share lineage)',
  hedge_laundering: 'asserts more confidence than the cited sub-claim',
  indicator_uncited_triggered: 'triggered indicator carries no citation',
  stale_leader: 'stale-cutoff officeholder reference',
  cross_target_leak: 'names only other countries than its desk target',
}

export function claimReasonLabel(reason: string): string {
  return CLAIM_REASON_LABELS[reason] ?? reason.replace(/_/g, ' ')
}

/**
 * U-5 — the plain-language gloss for the two honest-ABSENCE verdict kinds
 * ('not-recorded' / 'not-checked'). A hostile UX review found these strings
 * read like errors to a first-time reader ("verdict not recorded" sounds
 * broken); this says plainly that nothing was measured yet, never that
 * something failed.
 */
export const CLAIM_VERDICT_ABSENCE_EXPLAIN =
  'Nothing was checked at this per-claim granularity yet — an honest absence, ' +
  'not a hidden failure. (The pooled faithfulness score above still covers the ' +
  'whole read; this line is about THIS ONE citation specifically.)'

/** Extract the ordinal from a citation marker (`[8]` → 8, `[[ref:3]]` → 3).
 *  Null when the marker carries no digits (legacy uuid markers). */
export function markerOrdinal(marker: string): number | null {
  const m = /(\d+)/.exec(marker)
  return m ? Number(m[1]) : null
}

// ---------------------------------------------------------------------------
// Verdict derivation
// ---------------------------------------------------------------------------

export type ClaimVerdictKind =
  /** A judge_contradicted span names this chip's ordinal. */
  | 'contradicted'
  /** A judge_unsupported span names this chip's ordinal. */
  | 'unsupported'
  /** A non-judge flag (hedge_laundering, double_counted, …) names it. */
  | 'flagged'
  /** P2-4 — the persisted claim_verdicts LEDGER names this chip `supported`
   *  (no flag names it, and the ledger recorded a real per-claim verdict). */
  | 'supported'
  /** LLM judge ran; no flagged claim names this chip, and either no ledger
   *  was persisted for this critique or it says nothing about this ordinal. */
  | 'not-flagged'
  /** Only the deterministic floor ran — no per-claim judge verdict exists. */
  | 'not-checked'
  /** No verification block at all. */
  | 'not-recorded'

export interface ClaimVerdictSpan {
  /** The flagged claim's text (the judge's per-claim subject). */
  text: string
  reason: string
  reasonLabel: string
}

export interface ClaimVerdict {
  kind: ClaimVerdictKind
  /** The one-line verdict for the hover card. */
  label: string
  /** Flagged claim spans naming this chip's ordinal (empty unless flagged). */
  spans: ClaimVerdictSpan[]
  judgeStatus: string | null
  /** Pooled context when recorded (`checkable_claims` / `supported_claims`). */
  checkable: number | null
  supported: number | null
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function intOrNull(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** Severity order within a chip's matched flags — worst first. */
const REASON_SEVERITY: Record<string, number> = {
  judge_contradicted: 0,
  judge_unsupported: 1,
}

function spanNamesOrdinal(rawMarkers: unknown, ordinal: number): boolean {
  if (!Array.isArray(rawMarkers)) return false
  return rawMarkers.some((m) => {
    if (typeof m === 'number') return m === ordinal
    if (typeof m === 'string') return Number(m) === ordinal
    return false
  })
}

// ---------------------------------------------------------------------------
// P2-4 — the persisted per-claim `claim_verdicts` LEDGER (supported +
// hard_fail + soft_fail rows; verify.py `ClaimVerdict.as_dict`). Consulted
// ONLY when no span already flags this chip's ordinal — the ledger never
// overrides a real flag (including the advisory ones it deliberately excludes
// from its own rows), it only upgrades the previously-vague "not flagged"
// silence into a backed "supported" when the ledger actually says so.
// ---------------------------------------------------------------------------

type LedgerVerdict = 'supported' | 'hard_fail' | 'soft_fail'

function isLedgerVerdict(v: unknown): v is LedgerVerdict {
  return v === 'supported' || v === 'hard_fail' || v === 'soft_fail'
}

interface LedgerRowMatch {
  text: string
  verdict: LedgerVerdict
  reason: string | null
}

/** Worst-first ordering for ledger rows that (defensively) both name the same
 *  ordinal — mirrors REASON_SEVERITY's "hard beats soft" precedent. */
const LEDGER_SEVERITY: Record<LedgerVerdict, number> = {
  hard_fail: 0,
  soft_fail: 1,
  supported: 2,
}

function ledgerRowsNamingOrdinal(
  v: Record<string, unknown>,
  ordinal: number,
): LedgerRowMatch[] {
  const raw = v['claim_verdicts']
  if (!Array.isArray(raw)) return []
  const out: LedgerRowMatch[] = []
  for (const item of raw) {
    const o = asRecord(item)
    if (!o) continue
    const verdict = o['verdict']
    if (!isLedgerVerdict(verdict)) continue
    if (!spanNamesOrdinal(o['markers'], ordinal)) continue
    const text = typeof o['text'] === 'string' ? o['text'] : ''
    const reason = typeof o['reason'] === 'string' ? o['reason'] : null
    out.push({ text, verdict, reason })
  }
  return out
}

/**
 * Derive the per-chip verdict for one citation `marker` from the finding's
 * `verification` block. Never fabricates: absence of data yields the explicit
 * not-checked / not-recorded verdicts with honest labels.
 */
export function claimVerdictForMarker(
  verification: Record<string, unknown> | null | undefined,
  marker: string,
): ClaimVerdict {
  const v = asRecord(verification)
  if (!v) {
    return {
      kind: 'not-recorded',
      label: 'claim-level verdict not recorded',
      spans: [],
      judgeStatus: null,
      checkable: null,
      supported: null,
    }
  }

  const judgeStatus = typeof v['judge_status'] === 'string' ? (v['judge_status'] as string) : null
  const checkable = intOrNull(v['checkable_claims'])
  const supported = intOrNull(v['supported_claims'])
  const ordinal = markerOrdinal(marker)

  const spans: ClaimVerdictSpan[] = []
  if (ordinal !== null && Array.isArray(v['unsupported_spans'])) {
    for (const item of v['unsupported_spans'] as unknown[]) {
      const o = asRecord(item)
      if (!o) continue
      const text = typeof o['text'] === 'string' ? o['text'] : ''
      const reason = typeof o['reason'] === 'string' ? o['reason'] : 'unsupported'
      if (!text) continue
      if (!spanNamesOrdinal(o['markers'], ordinal)) continue
      spans.push({ text, reason, reasonLabel: claimReasonLabel(reason) })
    }
    spans.sort((a, b) => (REASON_SEVERITY[a.reason] ?? 2) - (REASON_SEVERITY[b.reason] ?? 2))
  }

  if (spans.length > 0) {
    const worst = spans[0]
    const kind: ClaimVerdictKind =
      worst.reason === 'judge_contradicted'
        ? 'contradicted'
        : worst.reason === 'judge_unsupported'
          ? 'unsupported'
          : 'flagged'
    return { kind, label: worst.reasonLabel, spans, judgeStatus, checkable, supported }
  }

  // P2-4 — no flag names this ordinal. Prefer the persisted claim_verdicts
  // LEDGER (records EVERY graded claim, supported included) over the vaguer
  // legacy "not flagged" read, when the ledger actually names this ordinal.
  if (ordinal !== null) {
    const ledgerRows = ledgerRowsNamingOrdinal(v, ordinal)
    if (ledgerRows.length > 0) {
      // A defensive worst-first pick: spans and the ledger are written in
      // parallel server-side, so a failing ledger row should already have
      // been caught by the spans branch above — this only guards against a
      // ledger-only failure ever being silently swallowed.
      const worst = [...ledgerRows].sort(
        (a, b) => LEDGER_SEVERITY[a.verdict] - LEDGER_SEVERITY[b.verdict],
      )[0]
      if (worst.verdict === 'supported') {
        return {
          kind: 'supported',
          label: 'supported by the verify judge',
          spans: [],
          judgeStatus,
          checkable,
          supported,
        }
      }
      const reasonLabel = worst.reason
        ? claimReasonLabel(worst.reason)
        : worst.verdict === 'hard_fail'
          ? 'flagged (hard fail)'
          : 'flagged (soft fail)'
      const kind: ClaimVerdictKind =
        worst.reason === 'judge_contradicted'
          ? 'contradicted'
          : worst.reason === 'judge_unsupported'
            ? 'unsupported'
            : 'flagged'
      return {
        kind,
        label: reasonLabel,
        spans: worst.text
          ? [{ text: worst.text, reason: worst.reason ?? worst.verdict, reasonLabel }]
          : [],
        judgeStatus,
        checkable,
        supported,
      }
    }
  }

  if (judgeStatus === 'llm') {
    return {
      kind: 'not-flagged',
      label: 'not flagged by the verify judge',
      spans: [],
      judgeStatus,
      checkable,
      supported,
    }
  }

  return {
    kind: 'not-checked',
    label: 'claim-level verdict not recorded — LLM judge did not run',
    spans: [],
    judgeStatus,
    checkable,
    supported,
  }
}
