/**
 * AssessmentBanner — the compact, embeddable world_assessor strip (v4).
 *
 * A single-row banner meant to sit atop another room (World / Flow): a globe
 * icon, the label, the latest assessment title, a relative timestamp, and a
 * right chevron. The whole row is a button that calls `onOpen` (default no-op)
 * to deep-link into The Why's full {@link WorldAssessment} reading column.
 *
 * It reads the same `/findings` feed as WorldAssessment and filters client-side
 * to `analyst_id === 'world_assessor'`, but stays deliberately dependency-light
 * (no markdown) — it only needs the newest finding's title + time. When there is
 * no world_assessor finding (or the feed is still loading / errored), it renders
 * nothing.
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { formatDistanceToNow } from 'date-fns'
import { Globe, ChevronRight } from 'lucide-react'
import { apiGet } from '@/lib/api'

const ASSESSOR_ID = 'world_assessor'

interface FindingRow {
  id: string
  title?: string | null
  analyst_id?: string | null
  produced_at: string
  data?: unknown
  payload?: unknown
}

interface FindingsResponse {
  data: FindingRow[]
}

/** Pull a usable title from the row's payload or top-level `title`. */
function rowTitle(row: FindingRow): string {
  const raw = row.data ?? row.payload
  if (raw && typeof raw === 'object') {
    const t = (raw as Record<string, unknown>).title
    if (typeof t === 'string' && t.trim() !== '') return t
  }
  if (typeof row.title === 'string' && row.title.trim() !== '') return row.title
  return 'World Assessment'
}

export interface AssessmentBannerProps {
  /** Open the full WorldAssessment surface. Defaults to a no-op. */
  onOpen?: () => void
}

export default function AssessmentBanner({ onOpen }: AssessmentBannerProps) {
  const { data } = useQuery<FindingsResponse>({
    queryKey: ['world-assessment-findings'],
    refetchInterval: 5 * 60_000,
    // Server-side analyst_id filter — the world_assessor's 1-per-6h finding is
    // never in the recent global feed window (see WorldAssessment.tsx).
    queryFn: () => apiGet<FindingsResponse>('/findings?analyst_id=world_assessor&limit=5'),
  })

  const latest = useMemo<FindingRow | null>(() => {
    const rows = data?.data ?? []
    let newest: FindingRow | null = null
    for (const row of rows) {
      if (row.analyst_id !== ASSESSOR_ID) continue
      if (!newest || Date.parse(row.produced_at) > Date.parse(newest.produced_at)) {
        newest = row
      }
    }
    return newest
  }, [data])

  if (!latest) return null

  const title = rowTitle(latest)
  const producedAt = Date.parse(latest.produced_at)
  const hasTime = Number.isFinite(producedAt)

  return (
    <button
      type="button"
      onClick={() => onOpen?.()}
      title="Open the world assessment"
      data-testid="assessment-banner"
      className="flex h-9 w-full items-center gap-2 border-b border-slate-800 bg-surface-200 px-3 text-left text-xs transition-colors hover:bg-surface-100"
    >
      <Globe className="h-3.5 w-3.5 shrink-0 text-slate-500" aria-hidden />
      <span className="shrink-0 font-medium text-slate-300">World assessment</span>
      <span aria-hidden className="shrink-0 text-slate-700">·</span>
      <span className="min-w-0 flex-1 truncate text-slate-400">{title}</span>
      {hasTime && (
        <span className="shrink-0 whitespace-nowrap text-slate-500">
          · as of {formatDistanceToNow(producedAt)}
        </span>
      )}
      <ChevronRight className="h-4 w-4 shrink-0 text-slate-600" aria-hidden />
    </button>
  )
}
