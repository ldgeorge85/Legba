/**
 * A2. Analyst Outputs (`analyst.outputs`).
 *
 * Live tail of this analyst's emitted findings. Reads
 * `GET /api/v1/findings?analyst_id=&limit=` (substrate-reads; findings carry
 * `analyst_id`) and subscribes to the `analyst.*.finding` NATS subject via the
 * registry-events WS multiplexer — new rows for this analyst pop in live
 * (pausable, deduped, badged), matching the daily-driver Findings feed idiom.
 *
 * Header surfaces: total emitted, a severity histogram, distinct runs, and the
 * latest emission time. Rows deep-link to Lineage (`legba:open-lineage`).
 */

import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet } from '@/lib/api'
import { subscribeRegistryEvents } from '@/lib/ws'
import { FINDINGS_TAIL_FILTER, mapTailEnvelope } from '@/lib/findingsViews'
import type { PanelProps } from '@/types'
import { selectRow } from '@/state/selection'

interface OutputRow {
  id: string
  kind: string
  title: string
  body: string | null
  confidence: number | null
  severity: string | null
  target_id: string | null
  analyst_id: string | null
  run_id?: string | null
  produced_at: string
  derived_from: string[]
}

interface FindingsResponse {
  data: OutputRow[]
  next_cursor: string | null
}

const SEV_ORDER = ['critical', 'high', 'medium', 'low'] as const
const SEV_PILL: Record<string, string> = {
  critical: 'bg-rose-900 text-rose-200',
  high: 'bg-amber-900 text-amber-200',
  medium: 'bg-slate-700 text-slate-200',
  low: 'bg-slate-800 text-slate-300',
}

function openLineage(row: OutputRow) {
  // Redesign Move 2: unified selection store (opens the Inspector).
  selectRow(row.kind || 'finding', row.id, row.title ?? undefined, { origin: 'analyst-outputs' })
}

export default function OutputsPanel({ registration, scope }: PanelProps) {
  const analyst_id = scope.analyst_id ?? registration.analyst_id ?? '(unbound)'
  const bound = analyst_id !== '(unbound)'
  const [live, setLive] = useState<OutputRow[]>([])
  const [tailOn, setTailOn] = useState(true)

  const { data, isLoading, error, refetch } = useQuery<FindingsResponse>({
    queryKey: ['analyst-outputs-feed', analyst_id],
    enabled: bound,
    refetchInterval: 30_000, // poll backstop; live-tail handles real-time
    queryFn: async () => {
      const r = await apiGet<FindingsResponse>(
        `/findings?analyst_id=${encodeURIComponent(analyst_id)}&limit=50`,
      )
      setLive([])
      return r
    },
  })

  // -------- NATS live-tail (this analyst only) --------
  const analystRef = useRef(analyst_id)
  analystRef.current = analyst_id
  useEffect(() => {
    if (!bound || !tailOn) return
    const sub = subscribeRegistryEvents(FINDINGS_TAIL_FILTER, (ev) => {
      if (ev.type !== 'event') return
      const row = mapTailEnvelope(ev.payload)
      if (!row || row.analyst_id !== analystRef.current) return
      setLive((prev) => {
        if (prev.some((r) => r.id === row.id)) return prev
        return [row as OutputRow, ...prev].slice(0, 200)
      })
    })
    return () => sub.close()
  }, [bound, tailOn])

  const rows = useMemo(() => {
    const seen = new Set<string>()
    const merged: OutputRow[] = []
    for (const r of [...live, ...(data?.data ?? [])]) {
      if (seen.has(r.id)) continue
      seen.add(r.id)
      merged.push(r)
    }
    return merged.sort((a, b) => Date.parse(b.produced_at) - Date.parse(a.produced_at))
  }, [data, live])

  const stats = useMemo(() => {
    const histo: Record<string, number> = {}
    const runs = new Set<string>()
    for (const r of rows) {
      const sev = r.severity ?? 'unrated'
      histo[sev] = (histo[sev] ?? 0) + 1
      if (r.run_id) runs.add(r.run_id)
    }
    return { histo, runs: runs.size, latest: rows[0]?.produced_at ?? null }
  }, [rows])

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${rows.length} outputs by ${analyst_id}${live.length ? ` · ${live.length} live` : ''}${
        stats.runs ? ` · ${stats.runs} run${stats.runs === 1 ? '' : 's'}` : ''
      }`}
      actions={
        <button
          onClick={() => setTailOn((v) => !v)}
          className={`text-[10px] px-2 py-0.5 rounded border ${
            tailOn ? 'border-accent-ok text-accent-ok' : 'border-slate-700 text-slate-500'
          }`}
          title="Toggle NATS live-tail"
          data-testid="outputs-tail-toggle"
        >
          {tailOn ? '● live' : '○ paused'}
        </button>
      }
      onRefresh={() => {
        setLive([])
        refetch()
      }}
    >
      {!bound && (
        <div className="text-xs text-slate-400">
          unbound — open this panel scoped to an analyst.
        </div>
      )}

      {bound && (
        <>
          {/* severity histogram */}
          <div className="flex items-center gap-2 mb-2 text-[11px] flex-wrap" data-testid="outputs-histogram">
            {SEV_ORDER.map((sev) =>
              stats.histo[sev] ? (
                <span key={sev} className={`rounded px-1.5 py-0.5 ${SEV_PILL[sev]}`}>
                  {sev}: {stats.histo[sev]}
                </span>
              ) : null,
            )}
            {stats.histo.unrated ? (
              <span className="rounded px-1.5 py-0.5 bg-slate-800 text-slate-400">
                unrated: {stats.histo.unrated}
              </span>
            ) : null}
            {rows.length === 0 && !isLoading && (
              <span className="text-slate-500">no outputs yet</span>
            )}
          </div>

          {isLoading && <div className="text-slate-500 text-sm">loading outputs…</div>}
          {error instanceof Error && (
            <div className="text-rose-400 text-sm">error: {error.message}</div>
          )}

          <div className="flex-1 overflow-auto space-y-1 text-xs" data-testid="outputs-list">
            {rows.map((o) => (
              <button
                key={o.id}
                onClick={() => openLineage(o)}
                className="w-full text-left bg-surface-100 hover:bg-surface-200 border border-slate-800 rounded p-2 block"
                data-testid={`output-${o.id}`}
              >
                <div className="flex items-baseline gap-2">
                  {(o as { live?: boolean }).live && (
                    <span className="shrink-0 rounded px-1 bg-emerald-900 text-emerald-200">
                      live
                    </span>
                  )}
                  <span className="text-slate-500 shrink-0 w-16 truncate">{o.kind}</span>
                  {o.severity && (
                    <span
                      className={`shrink-0 rounded px-1 text-[10px] ${
                        SEV_PILL[o.severity] ?? 'bg-slate-700 text-slate-200'
                      }`}
                    >
                      {o.severity}
                    </span>
                  )}
                  {o.confidence !== null && (
                    <span className="shrink-0 text-slate-500">c={o.confidence.toFixed(2)}</span>
                  )}
                  <span className="text-slate-200 truncate flex-1">{o.title}</span>
                  <span className="text-slate-600 shrink-0">
                    {new Date(o.produced_at).toLocaleString()}
                  </span>
                </div>
                {o.body && <div className="text-slate-400 line-clamp-2 mt-1">{o.body}</div>}
                <div className="flex gap-3 text-slate-600 mt-1 text-[10px]">
                  {o.target_id && <span>target: {o.target_id}</span>}
                  {o.derived_from.length > 0 && (
                    <span>
                      ← {o.derived_from.length} input{o.derived_from.length === 1 ? '' : 's'}
                    </span>
                  )}
                </div>
              </button>
            ))}
          </div>
        </>
      )}
    </PanelChrome>
  )
}
