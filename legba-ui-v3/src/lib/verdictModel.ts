/**
 * verdictModel — the ONE verification vocabulary (S7-T3), ICD-203 aligned.
 *
 * The old UI mixed contradictory chips: a raw `faithfulness NN%`, a `judge_status`
 * word, a bare `confidence` number, and a severity token — four dialects for the
 * reader to reconcile. This module collapses them into the two axes ICD-203
 * (Analytic Standards) keeps SEPARATE and never conflates:
 *
 *   LIKELIHOOD  — how probable the judgment is (a claim ABOUT THE WORLD). We map
 *                 the finding's own probability (`confidence`, or the composition
 *                 `effective_confidence`) onto the ICD-203 seven-point verbal
 *                 scale ("likely", "very likely", …).
 *   CONFIDENCE  — how much the analyst trusts the judgment given the EVIDENCE
 *                 (source quality + corroboration). We derive Low / Moderate /
 *                 High from the mandatory faithfulness-verify pass + how the
 *                 judge ran + citation coverage.
 *
 * HONESTY: nothing is fabricated. No probability → likelihood is `undefined`
 * (the badge shows nothing for that axis). No verify block → confidence is
 * `unassessed` ("unverified"), never a number. Pure + DOM-free so it is unit
 * tested and reused by VerdictBadge everywhere (findings + compositions).
 */

/** ICD-203 seven-point likelihood scale (least → most probable). `unstated`
 *  means the finding carried no probability at all (honest absence). */
export type LikelihoodBand =
  | 'almost no chance'
  | 'very unlikely'
  | 'unlikely'
  | 'roughly even chance'
  | 'likely'
  | 'very likely'
  | 'almost certain'
  | 'unstated'

/** Analytic confidence in the judgment (ICD-203 confidence, kept separate from
 *  likelihood). `unassessed` = no verify pass ran → we do NOT invent a level. */
export type ConfidenceLevel = 'low' | 'moderate' | 'high' | 'unassessed'

/**
 * P0-4 — the verify-EXEMPT structural analysts. The mandatory faithfulness
 * verify pass covers the LLM read/composition kinds only; these deterministic
 * structural/mining analysts emit findings OUTSIDE it (no LLM prose to grade,
 * flat confidence). Their rows must never render indistinguishable from
 * verified ones, so the badge shows `unverified — structural`.
 *
 * Mirror of the ONE server registry
 * (`legba.data.provenance.kinds.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS`, which
 * stamps `/findings` rows with `verify_exempt: "structural"`). The client
 * mirror exists because live-tail rows (NATS envelopes) never pass through
 * the reads-API projection. Keep in sync with the server set — the server
 * side is drift-guarded in tests/data_pkg/test_trace_only_output_split.py.
 */
export const STRUCTURAL_VERIFY_EXEMPT_ANALYSTS: ReadonlySet<string> = new Set([
  'graph_mining',
  'anomaly_detection',
  'band_calibration_tracker',
  'calibration_tracking',
  'unit_correctness_scorer',
  'composition_lineage_sweep',
  'adversarial_signals',
  'situation_clustering',
  'thematic_proposal',
  'indicator_tracker',
  'collection_gap',
  'hypothesis_lifecycle',
  'signals_retention',
  'analyst_traces_retention',
  'geo_convergence_scan',
  'fact_decay_scan',
  'source_track_record',
  'narrative_mapper',
  'desk_baseline',
])

/** One-line explanation of the structural exemption, for tooltips/subtext. */
export const STRUCTURAL_EXEMPT_NOTE =
  'deterministic structural read — not routed through the faithfulness verify pass'

/**
 * True when a finding is verify-exempt STRUCTURAL: either the server stamped
 * it (`verify_exempt === 'structural'`, authoritative) or its analyst_id is in
 * the client mirror registry (live-tail rows carry no stamp). Never true for
 * an unknown analyst — the badge is classification, not fabrication.
 */
export function isStructuralExempt(
  analystId?: string | null,
  verifyExempt?: string | null,
): boolean {
  // C2b — 'structural-verified' is still a STRUCTURAL row (its claims were
  // deterministically re-derived); isStructuralVerified() below tells the two
  // apart for the badge label.
  if (verifyExempt === 'structural' || verifyExempt === 'structural-verified') return true
  return analystId != null && STRUCTURAL_VERIFY_EXEMPT_ANALYSTS.has(analystId)
}

/**
 * C2b (P4-6) — true when a structural finding's asserted quantities were
 * DETERMINISTICALLY re-derived and MATCHED (the server stamped
 * `verify_exempt === 'structural-verified'`). The badge then reads
 * `structural — verified` instead of the honest `unverified — structural`.
 * A structural row without a passing structural critique is not verified.
 */
export function isStructuralVerified(verifyExempt?: string | null): boolean {
  return verifyExempt === 'structural-verified'
}

/** One finding/composition verify block, read defensively (all fields optional
 *  — the lineage read path often carries none of them). */
export interface VerificationBlock {
  faithfulness_score?: number | null
  judge_status?: string | null
  confidence_ceiling?: number | null
  overall_score?: number | null
}

/** The two-axis verdict a badge renders. */
export interface Verdict {
  likelihood: LikelihoodBand
  /** The backing probability [0,1], when one existed (for the tooltip). */
  probability: number | null
  confidence: ConfidenceLevel
  /** Faithfulness fraction [0,1] that drove the confidence axis, when present. */
  faithfulness: number | null
  /** How the verify pass ran: 'llm' | 'deterministic' | 'judge-unavailable'. */
  judgeStatus: string | null
  /** Count of resolved citations backing the prose (corroboration breadth). */
  citationCount: number
  /** P0-4 — true for a verify-EXEMPT structural analyst's finding. When the
   *  confidence axis is `unassessed` the badge renders
   *  `unverified — structural` instead of the bare `unverified`. */
  structural: boolean
  /** C2b — true when a structural finding's asserted quantities were
   *  deterministically re-derived and matched (server stamp
   *  `structural-verified`). The badge then reads `structural — verified`. */
  structuralVerified: boolean
}

/** ICD-203 probability bands, as [low, high] inclusive-low/exclusive-high cuts
 *  over [0,1]. The published percentage ranges, collapsed to contiguous cuts. */
const LIKELIHOOD_CUTS: Array<{ max: number; band: LikelihoodBand }> = [
  { max: 0.05, band: 'almost no chance' },
  { max: 0.2, band: 'very unlikely' },
  { max: 0.45, band: 'unlikely' },
  { max: 0.55, band: 'roughly even chance' },
  { max: 0.8, band: 'likely' },
  { max: 0.95, band: 'very likely' },
  { max: 1.01, band: 'almost certain' },
]

/** Map a probability [0,1] onto the ICD-203 verbal likelihood band. A value
 *  outside [0,1], NaN, or null/undefined → `unstated` (never a fabricated band). */
export function probabilityToLikelihood(p: number | null | undefined): LikelihoodBand {
  if (p == null || !Number.isFinite(p) || p < 0 || p > 1) return 'unstated'
  for (const cut of LIKELIHOOD_CUTS) {
    if (p < cut.max) return cut.band
  }
  return 'almost certain'
}

/**
 * Derive the analytic CONFIDENCE level from the verify pass. The faithfulness
 * fraction is the primary signal; the judge status gates the top band (only an
 * actual LLM judge earns High); citation breadth can only ever LOWER, never
 * raise. No verify block at all → `unassessed` (honest "unverified").
 */
export function deriveConfidence(
  verification: VerificationBlock | null | undefined,
  citationCount: number,
): { level: ConfidenceLevel; faithfulness: number | null; judgeStatus: string | null } {
  const faithfulness =
    verification && typeof verification.faithfulness_score === 'number'
      ? verification.faithfulness_score
      : null
  const judgeStatus =
    verification && typeof verification.judge_status === 'string' ? verification.judge_status : null

  if (faithfulness == null) {
    // No measured faithfulness → we never invent a level.
    return { level: 'unassessed', faithfulness: null, judgeStatus }
  }

  let level: ConfidenceLevel
  if (faithfulness >= 0.8 && judgeStatus === 'llm') level = 'high'
  else if (faithfulness >= 0.6) level = 'moderate'
  else level = 'low'

  // A single uncorroborated citation cannot read as High confidence.
  if (level === 'high' && citationCount < 2) level = 'moderate'

  return { level, faithfulness, judgeStatus }
}

/** Inputs a finding/composition surfaces for its verdict. */
export interface VerdictInput {
  /** The finding's own probability/confidence (the likelihood driver). */
  confidence?: number | null
  /** A composition's effective_confidence, used when `confidence` is absent. */
  effectiveConfidence?: number | null
  verification?: VerificationBlock | null
  citationCount?: number
  /** The emitting analyst id — classifies verify-exempt structural rows (P0-4). */
  analystId?: string | null
  /** The server's `verify_exempt` stamp from `/findings`, when present. */
  verifyExempt?: string | null
}

/** Assemble the two-axis {@link Verdict} from a finding's fields. */
export function buildVerdict(input: VerdictInput): Verdict {
  const citationCount = input.citationCount ?? 0
  const probability =
    typeof input.confidence === 'number' && Number.isFinite(input.confidence)
      ? input.confidence
      : typeof input.effectiveConfidence === 'number' && Number.isFinite(input.effectiveConfidence)
        ? input.effectiveConfidence
        : null
  const conf = deriveConfidence(input.verification, citationCount)
  return {
    likelihood: probabilityToLikelihood(probability),
    probability,
    confidence: conf.level,
    faithfulness: conf.faithfulness,
    judgeStatus: conf.judgeStatus,
    citationCount,
    structural: isStructuralExempt(input.analystId, input.verifyExempt),
    structuralVerified: isStructuralVerified(input.verifyExempt),
  }
}

/** A short human label for a judge status, for the tooltip. */
export function judgeStatusLabel(status: string | null): string {
  switch (status) {
    case 'llm':
      return 'LLM judge'
    case 'deterministic':
      return 'deterministic floor'
    case 'judge-unavailable':
      return 'judge unavailable'
    default:
      return status ?? 'not verified'
  }
}

/** The ICD-203 likelihood legend: band → its published probability range. */
export const LIKELIHOOD_LEGEND: Array<{ band: LikelihoodBand; range: string }> = [
  { band: 'almost no chance', range: '01–05%' },
  { band: 'very unlikely', range: '05–20%' },
  { band: 'unlikely', range: '20–45%' },
  { band: 'roughly even chance', range: '45–55%' },
  { band: 'likely', range: '55–80%' },
  { band: 'very likely', range: '80–95%' },
  { band: 'almost certain', range: '95–99%' },
]

/** The confidence legend: level → what earns it (evidence quality, ICD-203). */
export const CONFIDENCE_LEGEND: Array<{ level: ConfidenceLevel; meaning: string }> = [
  { level: 'high', meaning: 'LLM-judged faithful ≥80% over ≥2 corroborating citations' },
  { level: 'moderate', meaning: 'verified faithful ≥60% (or judged high on a single citation)' },
  { level: 'low', meaning: 'verified but faithfulness below 60% — read with caution' },
  {
    level: 'unassessed',
    meaning:
      'no faithfulness-verify pass on this read — unverified; "— structural" marks a ' +
      'deterministic structural/mining read that is verify-exempt by design',
  },
]
