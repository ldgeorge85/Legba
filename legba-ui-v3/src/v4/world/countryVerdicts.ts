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
  /** The desk target id the composition was produced for (`country_g20_us`) —
   *  lets a consumer select the desk (P1-7 Wall band grid). */
  targetId: string
  verdict: Verdict
  title: string
  producedAt: string
}

/** Fill colour per confidence band — the choropleth ramp. `unassessed` gets a
 *  muted slate so a verified-but-unfaithful read is visually distinct from a
 *  never-verified one. */
/**
 * CHANNEL B · CONFIDENCE (UI_HOLISTIC_DESIGN_2026-08-24 §5.2/§5.3) — one hue,
 * sequential, plus a cartographic "no data" neutral. Mirrors the `--conf-*`
 * tokens; kept as literals because the choropleth feeds them straight into a
 * MapLibre paint expression, which cannot read a CSS variable.
 *
 * THE RE-KEY: this ramp used to be emerald / amber / ROSE / slate, which put a
 * confidence band on the same red-amber-green hues severity already owned — so
 * red meant "critical severity" AND "low confidence", and green meant "LOW
 * severity" (nothing wrong) AND "high confidence". Three questions, one set of
 * hues, three mappings. Confidence is now deliberately desaturated blue: it
 * never competes with a severity mark and never borrows its meaning. The
 * choropleth legend, the Wall's band grid and the sidebar's desk chips all read
 * this same map, so no two surfaces can disagree.
 */
export const CONFIDENCE_FILL: Record<ConfidenceLevel, string> = {
  high: '#79c0ff', // faithful, corroborated
  moderate: '#4493f8',
  low: '#1f6feb', // verified but low faithfulness
  unassessed: '#30363d', // no verify pass — cartographic "no data"
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
      targetId: row.target_id ?? '',
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
