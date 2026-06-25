/**
 * T2. Target Signals (`target.signals`) — UI-3 (Tier B) rebuilt.
 *
 * Reads the frozen substrate surface
 * `GET /api/v1/signals?target_id=…[&source_id=][&language=]` →
 * `{ data, next_cursor }` (`SignalRow`). Signals are TARGET-AGNOSTIC — the
 * `target_id` filter resolves the target's `scope.geo` and returns the
 * signals geocoded into those countries (per the substrate-reads contract).
 *
 * v2 parity (Signals scored a raw-data table, personal-mode):
 *   - paged table: title, source, language, geo country, produced_at;
 *   - server-side `source_id` + `language` filters;
 *   - client-side free-text filter over title/source/url;
 *   - geo country chip (from `data.geo.country` when geocoded, else `geo[]`);
 *   - entity-class + tag chips on expand;
 *   - source out-link (`source_url`) + row-level lineage affordance.
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, ApiError } from '@/lib/api'
import { useLiveTail } from '@/lib/useLiveTail'
import type { PanelProps } from '@/types'
import { cn } from '@/lib/cn'
import { selectRow } from '@/state/selection'

interface SignalGeo {
  lat?: number
  lon?: number
  country?: string
  country_iso2?: string
}

interface SignalRow {
  id: string
  title: string
  source_id: string
  source_url: string | null
  language: string
  produced_at: string
  geo: string[]
  tags: string[]
  entity_classes: string[]
  derived_from: string[]
  data: { geo?: SignalGeo } & Record<string, unknown>
}

interface Page<T> {
  data: T[]
  next_cursor: string | null
}

function openLineage(kind: string, id: string) {
  // Redesign Move 2: drive the unified selection store (opens the Inspector +
  // brushes every room) instead of firing a legacy window event into the void.
  selectRow(kind, id)
}

/** Country label: geocoded `data.geo.country` first, else the `geo[]` ISO list. */
function countryLabel(row: SignalRow): string {
  const g = row.data?.geo
  if (g?.country) return g.country
  if (g?.country_iso2) return g.country_iso2
  if (row.geo.length > 0) return row.geo.join(', ')
  return '—'
}

export default function TargetSignalsPanel({ registration, scope }: PanelProps) {
  const target_id = scope.target_id ?? registration.descriptor_id
  const [open, setOpen] = useState<string | null>(null)
  const [text, setText] = useState('')
  const [sourceId, setSourceId] = useState('')
  const [language, setLanguage] = useState('')
  const [live, setLive] = useState(true)

  const { data, error, isLoading, refetch, isFetching } = useQuery<Page<SignalRow>>({
    enabled: !!target_id,
    queryKey: ['target-signals', target_id, sourceId, language],
    queryFn: async () => {
      const qs = new URLSearchParams({ target_id, limit: '200' })
      if (sourceId) qs.set('source_id', sourceId)
      if (language) qs.set('language', language)
      try {
        return await apiGet<Page<SignalRow>>(`/signals?${qs.toString()}`)
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { data: [], next_cursor: null }
        throw e
      }
    },
    refetchInterval: 60_000,
  })

  // Live-tail: refetch the signals page when a new signal is published. Gated
  // by the `live` toggle (and inert under test — the stub WS never fires).
  const { connected } = useLiveTail(
    'legba.signals.>',
    () => {
      if (target_id) refetch()
    },
    live && !!target_id,
  )

  const rows = data?.data ?? []

  // Distinct facet values for the dropdowns — derived from the loaded page.
  const sourceOptions = useMemo(
    () => Array.from(new Set(rows.map((r) => r.source_id))).sort(),
    [rows],
  )
  const languageOptions = useMemo(
    () => Array.from(new Set(rows.map((r) => r.language).filter(Boolean))).sort(),
    [rows],
  )

  // Client-side free-text filter over title / source / url.
  const visible = useMemo(() => {
    const q = text.trim().toLowerCase()
    if (!q) return rows
    return rows.filter(
      (r) =>
        r.title.toLowerCase().includes(q) ||
        r.source_id.toLowerCase().includes(q) ||
        (r.source_url ?? '').toLowerCase().includes(q),
    )
  }, [rows, text])

  const actions = (
    <div className="flex items-center gap-1">
      <button
        onClick={() => setLive((v) => !v)}
        className={cn(
          'text-[10px] px-2 py-0.5 rounded border shrink-0',
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
        data-testid="target-signals-live"
      >
        {live ? '● live' : '○ paused'}
      </button>
      <input
        type="text"
        placeholder="filter…"
        value={text}
        onChange={(e) => setText(e.target.value)}
        className="bg-surface-200 border border-slate-700 rounded px-2 py-0.5 text-xs w-28"
        data-testid="target-signals-text"
      />
      <select
        className="bg-surface-200 border border-slate-700 rounded px-2 py-0.5 text-xs max-w-[10rem]"
        value={sourceId}
        onChange={(e) => setSourceId(e.target.value)}
        data-testid="target-signals-source"
      >
        <option value="">all sources</option>
        {/* Union of loaded sources plus the active filter (so it survives a refetch). */}
        {Array.from(new Set([...sourceOptions, ...(sourceId ? [sourceId] : [])])).map((s) => (
          <option key={s} value={s}>
            {s}
          </option>
        ))}
      </select>
      <select
        className="bg-surface-200 border border-slate-700 rounded px-2 py-0.5 text-xs"
        value={language}
        onChange={(e) => setLanguage(e.target.value)}
        data-testid="target-signals-language"
      >
        <option value="">all langs</option>
        {Array.from(new Set([...languageOptions, ...(language ? [language] : [])])).map((l) => (
          <option key={l} value={l}>
            {l}
          </option>
        ))}
      </select>
    </div>
  )

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${visible.length}${
        visible.length !== rows.length ? `/${rows.length}` : ''
      } signal${rows.length === 1 ? '' : 's'} · target ${target_id}`}
      actions={actions}
      onRefresh={() => refetch()}
    >
      {isLoading && <div className="text-xs text-slate-400">Loading signals…</div>}
      {error && (
        <div className="text-xs text-accent-critical">
          Failed to load: {(error as Error).message}
        </div>
      )}
      {!isLoading && !error && visible.length === 0 && (
        <div className="text-xs text-slate-400" data-testid="target-signals-empty">
          {isFetching
            ? 'Loading…'
            : rows.length > 0
              ? 'No signals match the current filter.'
              : 'No signals for this target yet.'}
        </div>
      )}
      {visible.length > 0 && (
        <table className="w-full text-xs">
          <thead className="text-slate-400 text-left border-b border-slate-700/60">
            <tr>
              <th className="py-1 pr-2">Title</th>
              <th className="py-1 pr-2">Source</th>
              <th className="py-1 pr-2">Lang</th>
              <th className="py-1 pr-2">Geo</th>
              <th className="py-1 pr-2">When</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((s) => (
              <SignalRowView
                key={s.id}
                row={s}
                expanded={open === s.id}
                onToggle={() => setOpen(open === s.id ? null : s.id)}
              />
            ))}
          </tbody>
        </table>
      )}
    </PanelChrome>
  )
}

function SignalRowView({
  row: s,
  expanded,
  onToggle,
}: {
  row: SignalRow
  expanded: boolean
  onToggle: () => void
}) {
  return (
    <>
      <tr
        className={cn(
          'border-b border-slate-800/40 cursor-pointer hover:bg-surface-50/40',
          expanded && 'bg-surface-50/60',
        )}
        onClick={onToggle}
        data-testid={`target-signal-row-${s.id}`}
      >
        <td className="py-1 pr-2 max-w-md truncate">{s.title}</td>
        <td className="py-1 pr-2 font-mono text-slate-400 truncate max-w-[10rem]">{s.source_id}</td>
        <td className="py-1 pr-2 font-mono text-slate-500">{s.language || '—'}</td>
        <td className="py-1 pr-2">
          <span className="px-1.5 py-0.5 rounded bg-surface-200 text-[10px] text-slate-300">
            {countryLabel(s)}
          </span>
        </td>
        <td className="py-1 pr-2 font-mono text-slate-500 whitespace-nowrap">
          {new Date(s.produced_at).toLocaleString()}
        </td>
      </tr>
      {expanded && (
        <tr className="border-b border-slate-800/40">
          <td colSpan={5} className="px-2 py-2 bg-surface-50/40">
            <div className="space-y-2 text-xs">
              {(s.entity_classes.length > 0 || s.tags.length > 0) && (
                <div className="flex flex-wrap gap-1">
                  {s.entity_classes.map((c) => (
                    <span
                      key={`ec-${c}`}
                      className="px-1.5 py-0.5 rounded bg-accent-info/20 text-[10px] text-accent-info"
                      title="entity class"
                    >
                      {c}
                    </span>
                  ))}
                  {s.tags.map((t) => (
                    <span
                      key={`tag-${t}`}
                      className="px-1.5 py-0.5 rounded bg-surface-200 text-[10px] text-slate-300"
                      title="tag"
                    >
                      {t}
                    </span>
                  ))}
                </div>
              )}
              <div className="flex items-center gap-3">
                {s.source_url && (
                  <a
                    href={s.source_url}
                    target="_blank"
                    rel="noreferrer noopener"
                    onClick={(e) => e.stopPropagation()}
                    className="text-[10px] underline text-accent-info"
                    data-testid={`target-signal-out-${s.id}`}
                  >
                    open source ↗
                  </a>
                )}
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    openLineage('signal', s.id)
                  }}
                  className="text-[10px] underline text-accent-info"
                >
                  trace this signal →
                </button>
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}
