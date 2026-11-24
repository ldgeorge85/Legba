/**
 * subjectReads — a producer's subject reads at VERDICT weight (U-4), the ONE
 * read behind the globe's blanks, the Wall's band grid, the rail's
 * confidence chips, the palette and the Findings verdicts.
 *
 * `GET /v3/map/subject_reads?analyst_id=` carries exactly what a verdict
 * needs — the subject, the instant, the stored and effective confidence, the
 * verification block's scalar leaves and the citation count — where the
 * default `/findings` weight carries every composition's quoted blocks (a
 * 200-row page of country compositions was ~23 MB live, 2026-11-14). A
 * registry that does not serve the route yet (404) is answered from the full
 * read, reduced to the same shape, so a UI deployed before its registry still
 * draws.
 */
import { ApiError, apiGet } from '@/lib/api'
import { subjectReadsKey } from '@/lib/queryKeys'

export interface SubjectRead {
  id: string
  title: string
  target_id: string | null
  analyst_id: string | null
  produced_at: string
  confidence: number
  effective_confidence: number | null
  verification: Record<string, unknown> | null
  citation_count: number
}

export interface SubjectReadsPage {
  available: boolean
  data: SubjectRead[]
}

interface FullRow {
  id: string
  title?: string | null
  target_id?: string | null
  analyst_id?: string | null
  produced_at: string
  confidence?: number | null
  effective_confidence?: number | null
  verification?: Record<string, unknown> | null
  data?: { citations?: unknown[]; data?: { citations?: unknown[] }; effective_confidence?: number | null; verification?: Record<string, unknown> | null } | null
}

/** A default-weight `/findings` row reduced to the verdict shape (the 404 fallback). */
export function fromFullRow(r: FullRow): SubjectRead {
  const cites = r.data?.data?.citations ?? r.data?.citations
  return {
    id: r.id,
    title: r.title ?? '',
    target_id: r.target_id ?? null,
    analyst_id: r.analyst_id ?? null,
    produced_at: r.produced_at,
    confidence: typeof r.confidence === 'number' ? r.confidence : 0,
    effective_confidence: r.effective_confidence ?? r.data?.effective_confidence ?? null,
    verification: r.verification ?? r.data?.verification ?? null,
    citation_count: Array.isArray(cites) ? cites.length : 0,
  }
}

export async function fetchSubjectReads(analystId: string, limit = 200): Promise<SubjectReadsPage> {
  const q = `analyst_id=${encodeURIComponent(analystId)}&limit=${limit}`
  try {
    return await apiGet<SubjectReadsPage>(`/v3/map/subject_reads?${q}`)
  } catch (e) {
    if (!(e instanceof ApiError) || e.status !== 404) throw e
    const page = await apiGet<{ data?: FullRow[] }>(`/findings?${q}&fields=full`)
    return { available: true, data: (page.data ?? []).map(fromFullRow) }
  }
}

/** The shared query options for one producer's reads (one key, one fetch, whoever asks). */
export function subjectReadsQuery(analystId: string) {
  return {
    queryKey: subjectReadsKey(analystId),
    queryFn: () => fetchSubjectReads(analystId),
    staleTime: 60_000,
    refetchInterval: 120_000,
  }
}
