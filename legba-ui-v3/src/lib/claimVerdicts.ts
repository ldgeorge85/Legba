/**
 * claimVerdicts — per-citation-chip verify verdicts (P1-8), pure + DOM-free.
 *
 * WHAT THE PAYLOAD ACTUALLY CARRIES (src/legba/data/provenance/verify.py,
 * `FaithfulnessReport.as_dict`): the finding-level `verification` block records
 * the POOLED score (`faithfulness_score`, `checkable_claims`,
 * `supported_claims`, `judge_status`) plus `unsupported_spans` — one entry per
 * FLAGGED claim: `{text, reason, markers}`, where `markers` lists the citation
 * ordinals the flagged claim carried (unit `[N]` signal indices, or
 * composition `[[ref:N]]` sub-claim ordinals). Per-claim SUPPORTED verdicts
 * are NOT persisted — only the failures are named, plus the pooled counts.
 *
 * So the honest per-chip verdict is:
 *   * a span with this chip's ordinal in its `markers` → that claim's flag
 *     (contradicted / unsupported / hedge-laundering / …), with the flagged
 *     claim text to show;
 *   * no flagged span naming this chip while the LLM judge RAN → "not
 *     flagged" (every flagged checkable claim is named, so absence is
 *     meaningful — but a positive per-claim "supported" is not recorded, and
 *     we say so via the pooled context);
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
  /** LLM judge ran; no flagged claim names this chip. */
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
