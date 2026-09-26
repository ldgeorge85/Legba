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
import { ScaleStamp } from '@/components/ScaleStamp'
import { requestCrossFraming } from '@/lib/crossFramingLink'

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
                            {/* K3 — WHICH intensity scale this reading is on.
                                Migration 0188 re-based every stored score in
                                August 2026, so a 59 here and a 59 from July
                                are not the same measurement; the chip says so
                                instead of leaving the reader to assume. The
                                "opened" row below is this reading's as-of. */}
                            <Row label="scale">
                              <ScaleStamp
                                row={s.data}
                                testId={`situation-scale-stamp-${s.id}`}
                              />
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
                            {/* V3/P6 — the bounded occurrences this frame
                                tracks (situation_event_links), fetched
                                lazily on expand. */}
                            <TrackedEvents situationId={s.id} />
                            <div className="flex flex-wrap items-center gap-3 pt-1">
                              <button
                                onClick={(e) => {
                                  e.stopPropagation()
                                  openLineage('situation', s.id)
                                }}
                                className="text-[10px] underline text-accent-info"
                              >
                                trace this situation →
                              </button>
                              {/* Wave P lane B — CROSS-FRAMING from a situation
                                  row. "Trace" walks DOWN into this frame's own
                                  lineage; this walks ACROSS, putting the desk's
                                  most contested claim against every bounded
                                  unit and naming the ones that are silent. The
                                  situation's name rides along as the claim hint
                                  so the panel lands on the matter the operator
                                  was reading, falling back to the desk's most
                                  contested claim when no span matches it. */}
                              {s.target_id && (
                                <button
                                  onClick={(e) => {
                                    e.stopPropagation()
                                    requestCrossFraming({
                                      targetId: s.target_id!,
                                      claimText: s.name,
                                      origin: 'target.situations',
                                    })
                                  }}
                                  className="text-[10px] underline text-accent-info"
                                  data-testid={`situation-cross-framing-${s.id}`}
                                  title="put this desk's claim against its other bounded units"
                                >
                                  cross-framing ▸
                                </button>
                              )}
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

/** The `/v3/events` row shape used by the tracked-events list (V3/P6). */
interface EventRow {
  id: string
  title: string
  lifecycle_state: string
  severity?: string | null
  time_start?: string | null
  time_end?: string | null
}

/** V3/P6 — the events a situation tracks (`situation_event_links`), fetched
 *  lazily when the row expands (the 404 degrade covers a substrate that
 *  predates the events route). Clicking a row opens the event's lineage. */
function TrackedEvents({ situationId }: { situationId: string }) {
  const { data, isLoading, error } = useQuery<Page<EventRow>>({
    queryKey: ['situation-events', situationId],
    queryFn: async () => {
      try {
        return await apiGet<Page<EventRow>>(
          `/v3/events?situation_id=${encodeURIComponent(situationId)}&limit=50`,
        )
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { data: [], next_cursor: null }
        throw e
      }
    },
    staleTime: 60_000,
  })
  const rows = data?.data ?? []
  return (
    <div className="pt-1" data-testid="target-situation-events">
      <span className="text-slate-400">tracked events: </span>
      {isLoading ? (
        <span className="text-slate-500">loading…</span>
      ) : error ? (
        <span className="text-accent-critical">{(error as Error).message}</span>
      ) : rows.length === 0 ? (
        <span className="text-slate-500">none linked yet</span>
      ) : (
        <span className="inline-flex flex-wrap gap-1 align-top">
          {rows.map((ev) => (
            <button
              key={ev.id}
              title={`${ev.lifecycle_state} · ${ev.time_start ?? 'unstamped'}`}
              onClick={(e) => {
                e.stopPropagation()
                openLineage('event', ev.id)
              }}
              className="font-mono text-[10px] underline text-accent-info"
            >
              {ev.title.length > 40 ? `${ev.title.slice(0, 40)}…` : ev.title}
            </button>
          ))}
        </span>
      )}
    </div>
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
