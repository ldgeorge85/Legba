/**
 * T3. Target Findings (`target.findings`) — UI-3 (Tier B) rebuilt.
 *
 * Reads the frozen substrate surface
 * `GET /api/v1/findings?target_id=…[&severity=]` → `{ data, next_cursor }`
 * (`FindingRow`, column-for-column with `substrate_reads_api.py`). Severity
 * is *nullable* (the analyst may not classify), so the panel sorts and
 * badges defensively.
 *
 * v2 parity (Findings feed):
 *   - title + body preview + emit time;
 *   - severity badge (nullable → "unrated") + severity / recency sort;
 *   - topic-tag chips from `data.tags` (bookkeeping `target:` / `analyst:`
 *     tags dropped);
 *   - row-level lineage affordance (`legba:open-lineage`, the cross-panel
 *     `{ row_kind, row_id }` contract every sibling panel uses).
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, ApiError } from '@/lib/api'
import { useLiveTail } from '@/lib/useLiveTail'
import type { PanelProps } from '@/types'
import { cn } from '@/lib/cn'
import { selectRow } from '@/state/selection'

interface FindingRow {
  id: string
  title: string
  body: string | null
  severity: string | null
  confidence: number
  target_id: string | null
  analyst_id: string | null
  derived_from: string[]
  produced_at: string
  data: Record<string, unknown> | null
}

interface Page<T> {
  data: T[]
  next_cursor: string | null
}

type SortMode = 'severity' | 'recency'

/** Severity → rank for sorting; null (unrated) sinks below `low`. */
const SEVERITY_RANK: Record<string, number> = {
  critical: 4,
  high: 3,
  medium: 2,
  low: 1,
}

function severityRank(sev: string | null): number {
  return sev ? (SEVERITY_RANK[sev] ?? 0) : 0
}

function openLineage(kind: string, id: string) {
  // Redesign Move 2: drive the unified selection store (opens the Inspector +
  // brushes every room) instead of firing a legacy window event into the void.
  selectRow(kind, id)
}

/** Drop the `target:` / `analyst:` bookkeeping tags the analyst stamps. */
function topicTags(data: Record<string, unknown> | null): string[] {
  const raw = data && Array.isArray(data.tags) ? (data.tags as unknown[]) : []
  return raw
    .filter((t): t is string => typeof t === 'string')
    .filter((t) => !t.startsWith('target:') && !t.startsWith('analyst:'))
}

export default function TargetFindingsPanel({ registration, scope }: PanelProps) {
  const target_id = scope.target_id ?? registration.descriptor_id
  const [open, setOpen] = useState<string | null>(null)
  const [sort, setSort] = useState<SortMode>('severity')
  const [live, setLive] = useState(true)

  const { data, error, isLoading, refetch, isFetching } = useQuery<Page<FindingRow>>({
    enabled: !!target_id,
    queryKey: ['target-findings', target_id],
    queryFn: async () => {
      const qs = new URLSearchParams({ target_id, limit: '200' })
      try {
        return await apiGet<Page<FindingRow>>(`/findings?${qs.toString()}`)
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { data: [], next_cursor: null }
        throw e
      }
    },
    refetchInterval: 60_000,
  })

  // Live-tail: a new signal or finding for this target invalidates the page so
  // the freshest findings surface without waiting on the 60s poll. We refetch
  // (rather than prepend) so clustering/sort/dedup all flow through one path.
  // Gated by the `live` toggle (and inert under test — the stub WS never fires).
  const { connected } = useLiveTail(
    'legba.signals.>',
    () => {
      if (target_id) refetch()
    },
    live && !!target_id,
  )

  const rows = data?.data ?? []
  const sorted = useMemo(() => sortFindings(rows, sort), [rows, sort])

  const actions = (
    <div className="flex items-center gap-1">
      <button
        onClick={() => setLive((v) => !v)}
        className={cn(
          'text-[10px] px-2 py-0.5 rounded border',
          live
            ? connected
              ? 'border-accent-ok text-accent-ok'
              : 'border-accent-warning text-accent-warning'
            : 'border-slate-700 text-slate-500',
        )}
        title={
          live
            ? connected
              ? 'Live — refetching on new signals; click to pause'
              : 'Live — connecting…'
            : 'Paused — click to resume live refetch'
        }
        data-testid="target-findings-live"
      >
        {live ? '● live' : '○ paused'}
      </button>
      <select
        className="bg-surface-200 border border-slate-700 rounded px-2 py-0.5 text-xs"
        value={sort}
        onChange={(e) => setSort(e.target.value as SortMode)}
        data-testid="target-findings-sort"
      >
        <option value="severity">severity</option>
        <option value="recency">recency</option>
      </select>
    </div>
  )

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${rows.length} finding${rows.length === 1 ? '' : 's'} · target ${target_id}`}
      actions={actions}
      onRefresh={() => refetch()}
    >
      {isLoading && <div className="text-xs text-slate-400">Loading findings…</div>}
      {error && (
        <div className="text-xs text-accent-critical">
          Failed to load: {(error as Error).message}
        </div>
      )}
      {!isLoading && !error && sorted.length === 0 && (
        <div className="text-xs text-slate-400" data-testid="target-findings-empty">
          {isFetching ? 'Loading…' : 'No findings for this target yet.'}
        </div>
      )}
      {sorted.length > 0 && (
        <ul className="space-y-1">
          {sorted.map((f) => (
            <FindingItem
              key={f.id}
              finding={f}
              expanded={open === f.id}
              onToggle={() => setOpen(open === f.id ? null : f.id)}
            />
          ))}
        </ul>
      )}
    </PanelChrome>
  )
}

function FindingItem({
  finding: f,
  expanded,
  onToggle,
}: {
  finding: FindingRow
  expanded: boolean
  onToggle: () => void
}) {
  const tags = topicTags(f.data)
  return (
    <li data-testid="target-finding-item">
      <button
        onClick={onToggle}
        className={cn(
          'w-full text-left px-2 py-1.5 rounded text-xs flex items-center gap-2',
          'hover:bg-surface-50/40',
          expanded && 'bg-surface-50/60',
        )}
        data-testid={`target-finding-row-${f.id}`}
      >
        <SeverityBadge severity={f.severity} />
        <span className="flex-1 truncate">{f.title}</span>
        <span className="font-mono text-[11px] text-slate-300 shrink-0" title="confidence">
          {(f.confidence * 100).toFixed(0)}%
        </span>
      </button>
      {expanded && (
        <div className="ml-4 mt-1 p-2 bg-surface-50/40 rounded text-xs space-y-2">
          {f.body && <div className="text-slate-300 whitespace-pre-wrap">{f.body}</div>}

          {tags.length > 0 && (
            <div className="flex flex-wrap gap-1" data-testid={`target-finding-tags-${f.id}`}>
              {tags.map((t) => (
                <span
                  key={t}
                  className="px-1.5 py-0.5 rounded bg-surface-200 text-[10px] text-slate-300"
                >
                  {t}
                </span>
              ))}
            </div>
          )}

          <div className="text-[10px] text-slate-500">
            {f.analyst_id ?? 'unknown analyst'} · {new Date(f.produced_at).toLocaleString()}
          </div>

          <div>
            <span className="text-[10px] uppercase tracking-wide text-slate-400">
              evidence chain ({f.derived_from.length})
            </span>
            {f.derived_from.length > 0 && (
              <span className="ml-2 inline-flex flex-wrap gap-1 align-top">
                {f.derived_from.map((id) => (
                  <button
                    key={id}
                    title={`open lineage for ${id}`}
                    onClick={(e) => {
                      e.stopPropagation()
                      openLineage('signal', id)
                    }}
                    className="font-mono text-[10px] underline text-accent-info"
                  >
                    {id.slice(0, 8)}…
                  </button>
                ))}
              </span>
            )}
          </div>

          <button
            onClick={(e) => {
              e.stopPropagation()
              openLineage('finding', f.id)
            }}
            className="text-[10px] underline text-accent-info"
          >
            trace this finding →
          </button>
        </div>
      )}
    </li>
  )
}

function SeverityBadge({ severity }: { severity: string | null }) {
  const label = severity ?? 'unrated'
  const color =
    severity === 'critical'
      ? 'bg-accent-critical/30 text-accent-critical'
      : severity === 'high'
        ? 'bg-accent-warning/30 text-accent-warning'
        : severity === 'medium'
          ? 'bg-accent-info/30 text-accent-info'
          : severity === 'low'
            ? 'bg-accent-ok/30 text-accent-ok'
            : 'bg-surface-50 text-slate-400'
  return (
    <span
      className={cn(
        'px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wider shrink-0',
        color,
      )}
    >
      {label}
    </span>
  )
}

function sortFindings(rows: FindingRow[], mode: SortMode): FindingRow[] {
  const xs = [...rows]
  switch (mode) {
    case 'severity':
      return xs.sort((a, b) => {
        const d = severityRank(b.severity) - severityRank(a.severity)
        return d !== 0 ? d : b.produced_at.localeCompare(a.produced_at)
      })
    case 'recency':
      return xs.sort((a, b) => b.produced_at.localeCompare(a.produced_at))
  }
}
