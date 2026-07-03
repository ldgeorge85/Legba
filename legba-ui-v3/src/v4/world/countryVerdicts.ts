/**
 * Per-country verdict hook (S7-T5) — the data behind the World map's
 * banded-verdict choropleth.
 *
 * Fetches the verified `country_composition` findings (the per-country product —
 * see WorldAssessment desk card) and reduces them to ONE verdict per ISO-2
 * country, using the SAME `buildVerdict` the feed's VerdictBadge uses, so the
 * choropleth band and the desk card never disagree. Countries with no
 * composition are simply absent from the map (rendered as base land — honest
 * "unassessed", never a fabricated colour).
 *
 * The choropleth bands on the analytic CONFIDENCE axis (ICD-203) — how faithful
 * the verify pass judged the composition — because that is the platform's
 * selling point (measured faithfulness), and it is the axis every composition
 * carries. `probability`/`likelihood` ride along in the datum for tooltips.
 */
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import { buildVerdict, type ConfidenceLevel, type Verdict, type VerificationBlock } from '@/lib/verdictModel'

const COUNTRY_COMPOSITION_ID = 'country_composition'

/** `country_g20_us` / `country_watch_ir` → `US` / `IR`. */
const TARGET_ISO2_RE = /country_(?:g20|watch)_([a-z]{2})\b/i

interface CompositionRow {
  id: string
  target_id: string | null
  produced_at: string
  severity?: string | null
  confidence?: number | null
  effective_confidence?: number | null
  verification?: Record<string, unknown> | null
  title?: string | null
  data?: { citations?: unknown[]; effective_confidence?: number | null; verification?: Record<string, unknown> | null } | null
}

interface FindingsResponse {
  data: CompositionRow[]
  next_cursor: string | null
}

export interface CountryVerdict {
  iso2: string
  verdict: Verdict
  title: string
  producedAt: string
}

/** Fill colour per confidence band — the choropleth ramp. `unassessed` gets a
 *  muted slate so a verified-but-unfaithful read is visually distinct from a
 *  never-verified one. */
export const CONFIDENCE_FILL: Record<ConfidenceLevel, string> = {
  high: '#34d399', // emerald-400 — faithful, corroborated
  moderate: '#fbbf24', // amber-400
  low: '#fb7185', // rose-400 — verified but low faithfulness
  unassessed: '#64748b', // slate-500 — no verify pass
}

/** The choropleth legend, high→low (mirrors CONFIDENCE_FILL). */
export const CHOROPLETH_LEGEND: Array<{ level: ConfidenceLevel; label: string }> = [
  { level: 'high', label: 'High confidence' },
  { level: 'moderate', label: 'Moderate' },
  { level: 'low', label: 'Low' },
  { level: 'unassessed', label: 'Unverified' },
]

function citationCount(row: CompositionRow): number {
  const c = row.data?.citations
  return Array.isArray(c) ? c.length : 0
}

function toVerdict(row: CompositionRow): Verdict {
  const verification =
    (row.verification as VerificationBlock | null) ??
    (row.data?.verification as VerificationBlock | null) ??
    null
  return buildVerdict({
    confidence: row.confidence,
    effectiveConfidence: row.effective_confidence ?? row.data?.effective_confidence ?? null,
    verification,
    citationCount: citationCount(row),
  })
}

async function fetchCountryVerdicts(): Promise<Map<string, CountryVerdict>> {
  const page = await apiGet<FindingsResponse>(
    `/findings?analyst_id=${COUNTRY_COMPOSITION_ID}&limit=200`,
  )
  const byIso = new Map<string, CountryVerdict>()
  for (const row of page.data ?? []) {
    const m = TARGET_ISO2_RE.exec(row.target_id ?? '')
    if (!m) continue
    const iso2 = m[1].toUpperCase()
    // Keep the freshest composition per country (first-seen wins if the API
    // returns produced_at DESC; otherwise compare timestamps defensively).
    const prev = byIso.get(iso2)
    if (prev && Date.parse(prev.producedAt) >= Date.parse(row.produced_at)) continue
    byIso.set(iso2, {
      iso2,
      verdict: toVerdict(row),
      title: row.title ?? '(untitled composition)',
      producedAt: row.produced_at,
    })
  }
  return byIso
}

export interface UseCountryVerdictsResult {
  verdicts: Map<string, CountryVerdict>
  isLoading: boolean
}

export function useCountryVerdicts(): UseCountryVerdictsResult {
  const q = useQuery<Map<string, CountryVerdict>>({
    queryKey: ['world-country-verdicts'],
    queryFn: fetchCountryVerdicts,
    refetchInterval: 120_000,
    staleTime: 60_000,
  })
  return { verdicts: q.data ?? new Map(), isLoading: q.isLoading }
}
