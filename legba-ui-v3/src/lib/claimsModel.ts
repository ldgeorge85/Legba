/**
 * Claims data layer (UI-3 / Tier B — Claims panel, ex-Facts).
 *
 * Post-L-090 the standalone facts table collapsed into the universal
 * analyst-output substrate. There is no `/claims` or `/facts` REST
 * endpoint (frozen surface) — the honest source for claim-like rows that
 * carry a confidence AND corroboration is the findings read
 * (`GET /api/v1/findings` → `FindingRow`): the deterministic
 * `corroboration_scoring` handler writes `corroboration_score` (a [0,1]
 * score) and `corroboration_sources` (independent-source count) into the
 * row's `data` jsonb.
 *
 * This module normalizes a `FindingRow` into a `Claim` view-model, reading
 * those corroboration fields defensively (they're absent until the
 * corroboration pass has run), so the panel can render confidence +
 * corroboration without a DOM.
 */

export interface FindingRow {
  id: string
  kind: string
  title: string
  body: string
  confidence: number
  severity: string | null
  data: Record<string, unknown>
  target_id: string | null
  analyst_id: string | null
  produced_at: string
  derived_from: string[]
  /** P0-T3 faithfulness-verify detail block — a TOP-LEVEL sibling of `data`
   *  returned by `/findings` (NULL on a legacy / unverified finding). Read
   *  defensively for the why-not derive. */
  verification?: Record<string, unknown> | null
}

export interface Claim {
  id: string
  /** The claim statement (finding title). */
  statement: string
  body: string
  confidence: number
  severity: string
  /** Corroboration score in [0,1], or null when not yet scored. */
  corroborationScore: number | null
  /** Independent-source count, or null when not yet scored. */
  corroborationSources: number | null
  analyst_id: string | null
  produced_at: string
  derived_from: string[]
  /** P1-T6 freshness/decay surface, derived from the row `data` (never
   *  fabricated — a row with no decay fields is `fresh` at full opacity). */
  decay: DecayInfo
  /** P1-T6 why-NOT note: spans the faithfulness verify pass flagged as
   *  unsupported, or null when the claim is supported / unverified. The OTHER
   *  why-not path — a LIVE dispute — is `<ContestedBadge>` (#101). */
  whyNot: WhyNot | null
}

function numOrNull(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return Number(v)
  return null
}

function strOrNull(v: unknown): string | null {
  return typeof v === 'string' && v.length > 0 ? v : null
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

/** Read the corroboration count from any of the keys the backend may use. */
export function corroborationSources(data: Record<string, unknown>): number | null {
  return (
    numOrNull(data['corroboration_sources']) ??
    numOrNull(data['independent_sources']) ??
    numOrNull(data['corroboration_count'])
  )
}

/** Read the corroboration score ([0,1]) the corroboration pass writes. */
export function corroborationScore(data: Record<string, unknown>): number | null {
  return numOrNull(data['corroboration_score'])
}

// ---------------------------------------------------------------------------
// Freshness / decay (P1-T6).
//
// A claim fades as its evidence ages. Two honest, defensively-read signals on
// the row `data`, both written by the deterministic temporal passes:
//   * `confidence_components.decay` — a CUMULATIVE NEGATIVE adjustment the
//     `fact_decay` pass subtracts (−0.05 per stale tick); the more negative,
//     the more the claim has decayed.
//   * `valid_until` / `expired` — an explicit temporal bound; once `valid_until`
//     is in the past (or `data.expired` is set) the claim is STALE → faded.
// Absent fields → a `fresh` claim at full opacity (no fabricated decay).
// ---------------------------------------------------------------------------

export type DecayLabel = 'fresh' | 'decaying' | 'stale' | 'expired'

export interface DecayInfo {
  /** Cumulative confidence decay (≤ 0), or null when never decayed. */
  decay: number | null
  /** `valid_until` ISO string, or null. */
  validUntil: string | null
  /** Explicit `data.expired` flag. */
  expired: boolean
  /** `data.last_confidence_decay` ISO string, or null. */
  lastDecayAt: string | null
  /** Stale = expired, or `valid_until` now in the past. */
  stale: boolean
  /** Render opacity in [floor, 1] — faded as the claim decays / goes stale. */
  opacity: number
  /** Freshness bucket for the indicator chip. */
  label: DecayLabel
}

const _DECAY_OPACITY_FLOOR = 0.4

/** Derive the freshness/decay surface for a finding's `data`. `now` is
 *  injectable so the staleness boundary is deterministic in tests. */
export function decayInfo(
  data: Record<string, unknown> | null | undefined,
  now: number = Date.now(),
): DecayInfo {
  const d = data ?? {}
  const cc = asRecord(d['confidence_components'])
  const decay = cc ? numOrNull(cc['decay']) : null
  const validUntil = strOrNull(d['valid_until'])
  const lastDecayAt = strOrNull(d['last_confidence_decay'])
  const expiredFlag = d['expired']
  const expired = expiredFlag === true || expiredFlag === 'true'

  let validUntilPast = false
  if (validUntil) {
    const t = Date.parse(validUntil)
    if (Number.isFinite(t) && t < now) validUntilPast = true
  }
  const stale = expired || validUntilPast

  // Mirror the confidence the decay shaved off onto the render opacity, floored
  // so the row stays legible (degrade, never vanish). A stale/expired claim
  // fades further.
  let opacity = 1
  if (decay !== null && decay < 0) opacity = Math.max(_DECAY_OPACITY_FLOOR, 1 + decay)
  if (stale) opacity = Math.min(opacity, 0.5)
  opacity = Math.round(opacity * 100) / 100

  let label: DecayLabel
  if (expired) label = 'expired'
  else if (validUntilPast) label = 'stale'
  else if (decay !== null && decay < 0) label = 'decaying'
  else label = 'fresh'

  return { decay, validUntil, expired, lastDecayAt, stale, opacity, label }
}

// ---------------------------------------------------------------------------
// Why-NOT (P1-T6).
//
// When the P0-T2/T3 faithfulness verify pass flags a claim's prose, the row
// carries a top-level `verification` block NAMING the unsupported spans (a
// fact-asserting claim with no resolving citation, or one the judge could not
// support / found contradicted). Surfacing them is the "why not" for an
// unsupported claim — never a silent omission. A clean verify block (no spans)
// is NOT a why-not and derives `null` (no noise).
// ---------------------------------------------------------------------------

export interface UnsupportedSpanView {
  /** The fact-asserting claim text the verify pass could not support. */
  text: string
  /** Raw reason code. */
  reason: string
  /** Human-readable reason. */
  reasonLabel: string
  /** Citation markers the claim DID carry (empty for an uncited claim). */
  markers: number[]
}

export interface WhyNot {
  unsupportedSpans: UnsupportedSpanView[]
  /** Faithfulness score [0,1] when present. */
  faithfulnessScore: number | null
  /** `'deterministic' | 'llm'` verify-judge label. */
  judgeStatus: string | null
}

const _REASON_LABELS: Record<string, string> = {
  no_citation: 'no citation',
  unresolved_citation: 'citation resolves to nothing',
  judge_unsupported: 'unsupported by the verify judge',
  judge_contradicted: 'contradicted by the verify judge',
}

/** Derive the why-not note from a finding's faithfulness `verification` block.
 *  Returns null when the claim is supported / unverified (no spans). */
export function whyNot(
  verification: Record<string, unknown> | null | undefined,
): WhyNot | null {
  const v = asRecord(verification)
  if (!v) return null
  const rawSpans = Array.isArray(v['unsupported_spans'])
    ? (v['unsupported_spans'] as unknown[])
    : []
  const spans: UnsupportedSpanView[] = []
  for (const item of rawSpans) {
    const o = asRecord(item)
    const text = o ? strOrNull(o['text']) : null
    if (!o || !text) continue
    const reason = strOrNull(o['reason']) ?? 'unsupported'
    const markers = Array.isArray(o['markers'])
      ? (o['markers'] as unknown[])
          .map((m) => numOrNull(m))
          .filter((n): n is number => n !== null)
      : []
    spans.push({
      text,
      reason,
      reasonLabel: _REASON_LABELS[reason] ?? reason.replace(/_/g, ' '),
      markers,
    })
  }
  if (spans.length === 0) return null
  return {
    unsupportedSpans: spans,
    faithfulnessScore: numOrNull(v['faithfulness_score']),
    judgeStatus: strOrNull(v['judge_status']),
  }
}

export function toClaim(row: FindingRow): Claim {
  const data = row.data ?? {}
  return {
    id: row.id,
    statement: row.title,
    body: row.body,
    confidence: row.confidence,
    severity: row.severity ?? 'unknown',
    corroborationScore: corroborationScore(data),
    corroborationSources: corroborationSources(data),
    analyst_id: row.analyst_id,
    produced_at: row.produced_at,
    derived_from: row.derived_from ?? [],
    decay: decayInfo(data),
    whyNot: whyNot(row.verification ?? null),
  }
}

export function toClaims(rows: FindingRow[]): Claim[] {
  return rows.map(toClaim)
}

/** Distinct severities present, for the facet dropdown. */
export function claimSeverities(claims: Claim[]): string[] {
  return [...new Set(claims.map((c) => c.severity))].sort()
}
