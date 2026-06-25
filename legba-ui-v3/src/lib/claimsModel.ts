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
}

function numOrNull(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return Number(v)
  return null
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
  }
}

export function toClaims(rows: FindingRow[]): Claim[] {
  return rows.map(toClaim)
}

/** Distinct severities present, for the facet dropdown. */
export function claimSeverities(claims: Claim[]): string[] {
  return [...new Set(claims.map((c) => c.severity))].sort()
}
