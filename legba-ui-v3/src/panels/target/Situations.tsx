/**
 * T4. Target Situations (`target.situations`) — UI-3 (Tier B) rebuilt.
 *
 * Reads the frozen substrate surface
 * `GET /api/v1/situations?target_id=…[&state=]` → `{ data, next_cursor }`
 * (column-for-column `SituationRow` from `substrate_reads_api.py`).
 *
 * v2 parity (Situations scored keep-4):
 *   - status buckets (escalating / active / resolved) with counts;
 *   - status filter dropdown (server-side via `?state=`);
 *   - intensity-derived severity dot + event-count badge;
 *   - expand-to-detail: lifecycle timestamps, category, intensity,
 *     contributing findings (`derived_from`) each linking → lineage.
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, ApiError } from '@/lib/api'
import type { PanelProps } from '@/types'
import { cn } from '@/lib/cn'
import { selectRow } from '@/state/selection'

type SituationStatus = 'active' | 'resolved' | 'escalating'

interface SituationRow {
  id: string
  name: string
  status: string
  category: string
  last_event_at: string | null
  event_count: number
  intensity_score: number
  target_id: string | null
  analyst_id: string | null
  produced_at: string
  derived_from: string[]
  created_at: string
  updated_at: string
  data: Record<string, unknown>
}
interface Page<T> {
  data: T[]
  next_cursor: string | null
}

const STATUS_BUCKETS: SituationStatus[] = ['escalating', 'active', 'resolved']
const STATUS_FILTERS: Array<{ value: '' | SituationStatus; label: string }> = [
  { value: '', label: 'all' },
  { value: 'escalating', label: 'escalating' },
  { value: 'active', label: 'active' },
  { value: 'resolved', label: 'resolved' },
]

/** Map intensity [0,1] → a severity bucket + dot color (v2 severity badges). */
function intensityTier(score: number): { label: string; dot: string } {
  if (score >= 0.75) return { label: 'critical', dot: 'bg-accent-critical' }
  if (score >= 0.5) return { label: 'high', dot: 'bg-accent-warning' }
  if (score >= 0.25) return { label: 'medium', dot: 'bg-accent-info' }
  return { label: 'low', dot: 'bg-accent-ok' }
}

function openLineage(kind: string, id: string) {
  // Redesign Move 2: drive the unified selection store (opens the Inspector +
  // brushes every room) instead of firing a legacy window event into the void.
  selectRow(kind, id)
}

export default function TargetSituationsPanel({ registration, scope }: PanelProps) {
  const target_id = scope.target_id ?? registration.descriptor_id
  const [open, setOpen] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<'' | SituationStatus>('')

  const { data, error, isLoading, refetch, isFetching } = useQuery<Page<SituationRow>>({
    enabled: !!target_id,
    queryKey: ['target-situations', target_id, statusFilter],
    queryFn: async () => {
      const qs = new URLSearchParams({ target_id, limit: '100' })
      if (statusFilter) qs.set('state', statusFilter)
      try {
        return await apiGet<Page<SituationRow>>(`/situations?${qs.toString()}`)
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { data: [], next_cursor: null }
        throw e
      }
    },
    refetchInterval: 60_000,
  })

  const rows = data?.data ?? []

  const actions = (
    <select
      className="bg-surface-200 border border-slate-700 rounded px-2 py-0.5 text-xs"
      value={statusFilter}
      onChange={(e) => setStatusFilter(e.target.value as '' | SituationStatus)}
      data-testid="target-situations-status"
    >
      {STATUS_FILTERS.map((f) => (
        <option key={f.value} value={f.value}>
          {f.label}
        </option>
      ))}
    </select>
  )

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${rows.length} situation${rows.length === 1 ? '' : 's'} · target ${target_id}`}
      actions={actions}
      onRefresh={() => refetch()}
    >
      {isLoading && <div className="text-xs text-slate-400">Loading situations…</div>}
      {error && (
        <div className="text-xs text-accent-critical">
          Failed to load: {(error as Error).message}
        </div>
      )}
      {!isLoading && !error && rows.length === 0 && (
        <div className="text-xs text-slate-400" data-testid="target-situations-empty">
          {isFetching ? 'Loading…' : 'No situations for this target yet.'}
        </div>
      )}
      {rows.length > 0 && (
        <div className="space-y-4">
          {STATUS_BUCKETS.map((bucket) => {
            const bucketRows = rows.filter((s) => s.status === bucket)
            if (bucketRows.length === 0) return null
            return (
              <section key={bucket} data-testid={`target-situations-bucket-${bucket}`}>
                <h3 className="text-[11px] uppercase tracking-wider text-slate-400 mb-1">
                  {bucket} ({bucketRows.length})
                </h3>
                <ul className="space-y-1">
                  {bucketRows.map((s) => {
                    const tier = intensityTier(s.intensity_score)
                    const expanded = open === s.id
                    return (
                      <li key={s.id}>
                        <button
                          onClick={() => setOpen(expanded ? null : s.id)}
                          className={cn(
                            'w-full text-left px-2 py-1.5 rounded text-xs flex items-center gap-2',
                            'hover:bg-surface-50/40',
                            expanded && 'bg-surface-50/60',
                          )}
                          data-testid={`target-situation-row-${s.id}`}
                        >
                          <span
                            className={cn('inline-block w-2 h-2 rounded-full shrink-0', tier.dot)}
                            title={`intensity ${tier.label} (${s.intensity_score.toFixed(2)})`}
                          />
                          <span className="flex-1 truncate">{s.name}</span>
                          <span className="text-[10px] text-slate-500 shrink-0">
                            {s.category}
                          </span>
                          <span className="text-[10px] font-mono text-slate-400 shrink-0">
                            {s.event_count} ev
                          </span>
                        </button>
                        {expanded && (
                          <div className="ml-4 mt-1 p-2 bg-surface-50/40 rounded text-xs space-y-1">
                            <IntensityMeter score={s.intensity_score} tier={tier} />
                            <Row label="intensity">
                              {tier.label} · {s.intensity_score.toFixed(2)}
                            </Row>
                            <Row label="events">{s.event_count}</Row>
                            <Row label="opened">
                              {new Date(s.produced_at).toLocaleString()}
                            </Row>
                            {s.last_event_at && (
                              <Row label="last event">
                                {new Date(s.last_event_at).toLocaleString()}
                              </Row>
                            )}
                            <Row label="updated">
                              {new Date(s.updated_at).toLocaleString()}
                            </Row>
                            <div className="pt-1">
                              <span className="text-slate-400">
                                contributing findings ({s.derived_from.length}):{' '}
                              </span>
                              {s.derived_from.length === 0 ? (
                                <span className="text-slate-500">none recorded</span>
                              ) : (
                                <span className="inline-flex flex-wrap gap-1 align-top">
                                  {s.derived_from.map((fid) => (
                                    <button
                                      key={fid}
                                      title={`open lineage for ${fid}`}
                                      onClick={(e) => {
                                        e.stopPropagation()
                                        openLineage('finding', fid)
                                      }}
                                      className="font-mono text-[10px] underline text-accent-info"
                                    >
                                      {fid.slice(0, 8)}…
                                    </button>
                                  ))}
                                </span>
                              )}
                            </div>
                            <div className="pt-1">
                              <button
                                onClick={(e) => {
                                  e.stopPropagation()
                                  openLineage('situation', s.id)
                                }}
                                className="text-[10px] underline text-accent-info"
                              >
                                trace this situation →
                              </button>
                            </div>
                          </div>
                        )}
                      </li>
                    )
                  })}
                </ul>
              </section>
            )
          })}
        </div>
      )}
    </PanelChrome>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <span className="text-slate-400">{label}: </span>
      <span className="font-mono">{children}</span>
    </div>
  )
}

/** Intensity meter — fills [0,1] in the tier color (reuses the dot's bg class). */
function IntensityMeter({
  score,
  tier,
}: {
  score: number
  tier: { label: string; dot: string }
}) {
  const pct = Math.max(0, Math.min(1, score)) * 100
  return (
    <div className="flex items-center gap-2 pb-1" data-testid="target-situation-intensity">
      <span className="text-[10px] text-slate-400 w-16 shrink-0">intensity</span>
      <div className="flex-1 h-1.5 bg-surface-200 rounded overflow-hidden">
        <div className={cn('h-full rounded', tier.dot)} style={{ width: `${pct}%` }} />
      </div>
      <span className="font-mono text-[10px] text-slate-300 w-9 text-right">{pct.toFixed(0)}%</span>
    </div>
  )
}
