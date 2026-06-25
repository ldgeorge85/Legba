/**
 * UI-2 / Tier C — fan-out / provenance explorer.
 *
 * Walks the source-first fan-out:  source → signal → finding.
 *
 *   hop 1  source → signals    GET /signals?source_id= (this source's stream)
 *   hop 2  signal → findings    GET /findings, join on finding.derived_from
 *                               ⊇ {signal_id}  (NOT /lineage — see below)
 *
 * One source emits many signals (the "fan-out"); each signal fans out again
 * into the findings derived from it. This panel makes that visible end-to-end:
 * pick a source, see its signals, pick a signal, see the findings that cite it
 * in their `derived_from`. Any node hands off to the Provenance Lineage panel
 * (`legba:open-lineage`) for a full bidirectional walk.
 *
 * Provenance is reconstructed from `/signals` + `/findings.derived_from`
 * (per the source-first read model) rather than the dedicated `/lineage`
 * walk endpoint, which is not yet load-bearing. `derived_from` is the
 * authoritative provenance edge: a finding lists the signal ids it was
 * synthesised from, so `derived_from ⊇ {signal_id}` is the source→signal→
 * finding join.
 *
 * Source selection comes from scope / the `legba:open-source-detail` event
 * (shared with source.detail) or a manual id field.
 */

import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { ScopePicker } from '@/components/ScopePicker'
import { apiGet } from '@/lib/api'
import type { PanelProps } from '@/types'
import { selectRow, useSelection } from '@/state/selection'
import type { FindingRow, FindingsPage, SignalRow, SignalsPage } from './sourceTypes'

const KIND_COLOR: Record<string, string> = {
  signal: 'text-sky-300',
  finding: 'text-emerald-300',
  situation: 'text-amber-300',
  prediction: 'text-violet-300',
  hypothesis: 'text-fuchsia-300',
  critique: 'text-rose-300',
}

export default function FanoutExplorerPanel({ registration, scope }: PanelProps) {
  const initial =
    (registration.data_query?.source_id as string | undefined) ??
    (scope as { source_id?: string }).source_id ??
    ''
  const [sourceId, setSourceId] = useState(initial)
  const [selectedSignal, setSelectedSignal] = useState<string | null>(null)

  // Redesign Move 2: follow the shared selection when it's a source (replaces
  // the legacy `legba:open-source-detail` window listener).
  const selection = useSelection((s) => s.selection)
  useEffect(() => {
    if (selection?.kind === 'source') {
      setSourceId(selection.id)
      setSelectedSignal(null)
    }
  }, [selection])

  const enabled = sourceId.trim().length > 0

  // hop 1 — the source's published signals (server-side source_id filter)
  const signals = useQuery<SignalsPage>({
    enabled,
    queryKey: ['fanout-signals', sourceId],
    queryFn: () =>
      apiGet<SignalsPage>(`/signals?source_id=${encodeURIComponent(sourceId)}&limit=100`),
    refetchInterval: 30_000,
  })

  const sourceSignals: SignalRow[] = useMemo(() => {
    const all = signals.data?.data ?? []
    return all.filter((s) => s.descriptor_source_id === sourceId || s.source_id === sourceId)
  }, [signals.data, sourceId])

  // hop 2 — findings, joined on derived_from (recent window). Loaded for the
  // whole window once and joined client-side so picking any hop-1 signal is
  // instant and we can badge which signals actually fanned out.
  const findings = useQuery<FindingsPage>({
    enabled,
    queryKey: ['fanout-findings'],
    queryFn: () => apiGet<FindingsPage>('/findings?limit=200'),
    refetchInterval: 30_000,
  })

  // signal_id → findings derived from it (provenance edge: derived_from ⊇ id)
  const findingsBySignal = useMemo(() => {
    const m = new Map<string, FindingRow[]>()
    for (const f of findings.data?.data ?? []) {
      for (const src of f.derived_from ?? []) {
        const arr = m.get(src) ?? []
        arr.push(f)
        m.set(src, arr)
      }
    }
    return m
  }, [findings.data])

  // how many of this source's signals fed at least one finding
  const fanoutCounts = useMemo(() => {
    let withFindings = 0
    for (const s of sourceSignals) if ((findingsBySignal.get(s.id)?.length ?? 0) > 0) withFindings += 1
    return { signals: sourceSignals.length, withFindings }
  }, [sourceSignals, findingsBySignal])

  const downstream: FindingRow[] = useMemo(() => {
    if (selectedSignal == null) return []
    const list = findingsBySignal.get(selectedSignal) ?? []
    return [...list].sort((a, b) => Date.parse(b.produced_at) - Date.parse(a.produced_at))
  }, [selectedSignal, findingsBySignal])

  // fan-out summary: downstream rows grouped by the target they were emitted for
  const fanoutByTarget = useMemo(() => {
    const m = new Map<string, number>()
    for (const f of downstream) {
      const t = f.target_id ?? '(no target)'
      m.set(t, (m.get(t) ?? 0) + 1)
    }
    return Array.from(m.entries()).sort((a, b) => b[1] - a[1])
  }, [downstream])

  function openLineage(kind: string, id: string, title?: string | null) {
    // Redesign Move 2: unified selection store (opens the Inspector).
    selectRow(kind, id, title ?? undefined, { origin: 'fanout' })
  }

  return (
    <PanelChrome
      registration={registration}
      subtitle={
        enabled
          ? `${fanoutCounts.signals} signals · ${fanoutCounts.withFindings} fanned to findings`
          : 'select a source'
      }
      onRefresh={
        enabled
          ? () => {
              signals.refetch()
              findings.refetch()
            }
          : undefined
      }
    >
      <div className="flex items-center gap-2 mb-2 text-xs">
        <ScopePicker
          family="source"
          value={sourceId}
          onChange={(v) => {
            setSourceId(v)
            setSelectedSignal(null)
          }}
          placeholder="select a source…"
          className="flex-1 bg-surface-200 border border-slate-700 rounded p-1 px-2 font-mono text-slate-200"
          testId="fanout-source-id"
        />
      </div>

      {!enabled && (
        <div className="text-slate-500 text-sm py-4 text-center" data-testid="fanout-empty">
          enter a source id, or open a source from the registry / detail panel
        </div>
      )}

      {enabled && (
        <div className="flex-1 overflow-auto text-xs grid grid-cols-2 gap-2">
          {/* hop 1 — signals */}
          <div className="space-y-1" data-testid="fanout-signals">
            <div className="text-slate-400 text-[10px] uppercase tracking-wide sticky top-0 bg-surface-100 py-1">
              hop 1 — signals ({sourceSignals.length})
            </div>
            {signals.isLoading && <div className="text-slate-500">loading signals…</div>}
            {!signals.isLoading && sourceSignals.length === 0 && (
              <div className="text-slate-500">no signals from this source in the loaded window</div>
            )}
            {sourceSignals.map((s) => {
              const n = findingsBySignal.get(s.id)?.length ?? 0
              return (
                <button
                  key={s.id}
                  onClick={() => setSelectedSignal(s.id)}
                  className={`w-full text-left border rounded p-2 ${
                    selectedSignal === s.id
                      ? 'bg-sky-950 border-sky-700'
                      : 'bg-surface-100 hover:bg-surface-200 border-slate-800'
                  }`}
                  data-testid={`fanout-signal-${s.id}`}
                >
                  <div className="flex items-baseline gap-2">
                    <span className={`shrink-0 ${KIND_COLOR.signal}`}>signal</span>
                    <span className="text-slate-200 truncate flex-1">{s.title}</span>
                    {n > 0 && (
                      <span
                        className={`shrink-0 rounded px-1 text-[10px] bg-emerald-950 ${KIND_COLOR.finding}`}
                        title="findings derived from this signal"
                        data-testid={`fanout-signal-count-${s.id}`}
                      >
                        → {n}
                      </span>
                    )}
                  </div>
                  <div className="text-slate-600 mt-0.5 flex gap-2">
                    {s.geo.length > 0 && <span className="uppercase">{s.geo.join(' ')}</span>}
                    {s.language && <span>{s.language}</span>}
                    <span>{new Date(s.produced_at).toLocaleTimeString()}</span>
                  </div>
                </button>
              )
            })}
          </div>

          {/* hop 2 — findings derived from the selected signal */}
          <div className="space-y-1" data-testid="fanout-downstream">
            <div className="text-slate-400 text-[10px] uppercase tracking-wide sticky top-0 bg-surface-100 py-1">
              hop 2 — findings citing this signal
            </div>
            {selectedSignal == null && (
              <div className="text-slate-500">← pick a signal to see the findings derived from it</div>
            )}
            {selectedSignal != null && findings.isLoading && (
              <div className="text-slate-500">joining findings…</div>
            )}
            {findings.error instanceof Error && (
              <div className="text-rose-400">error: {findings.error.message}</div>
            )}

            {selectedSignal != null && !findings.isLoading && (
              <>
                {fanoutByTarget.length > 0 && (
                  <div className="flex gap-2 flex-wrap mb-1" data-testid="fanout-summary">
                    {fanoutByTarget.map(([t, n]) => (
                      <span
                        key={t}
                        className={`rounded px-1 text-[10px] bg-surface-200 ${KIND_COLOR.finding}`}
                        title="findings emitted for this target"
                      >
                        {n} finding{n === 1 ? '' : 's'} → {t}
                      </span>
                    ))}
                  </div>
                )}

                <div className="space-y-1">
                  {downstream.map((f) => (
                    <button
                      key={f.id}
                      onClick={() => openLineage('finding', f.id, f.title)}
                      className="w-full text-left bg-surface-100 hover:bg-surface-200 border border-slate-800 rounded p-2"
                      data-testid={`fanout-node-${f.id}`}
                    >
                      <div className="flex items-baseline gap-2">
                        <span className={`shrink-0 ${KIND_COLOR.finding}`}>finding</span>
                        <span className="text-slate-200 truncate flex-1">
                          {f.title || '(untitled)'}
                        </span>
                        {f.severity && (
                          <span className="shrink-0 rounded px-1 text-[10px] bg-amber-900 text-amber-200">
                            {f.severity}
                          </span>
                        )}
                        {f.confidence != null && (
                          <span className="shrink-0 text-slate-500 text-[10px]">
                            c={f.confidence.toFixed(2)}
                          </span>
                        )}
                      </div>
                      <div className="text-slate-600 mt-0.5 flex gap-2">
                        {f.target_id && <span>target: {f.target_id}</span>}
                        {f.analyst_id && <span>analyst: {f.analyst_id}</span>}
                        <span className="ml-auto">
                          ← {f.derived_from.length} input{f.derived_from.length === 1 ? '' : 's'}
                        </span>
                      </div>
                    </button>
                  ))}
                </div>

                {downstream.length === 0 && (
                  <div className="text-slate-500">
                    no findings cite this signal yet (the signal may be too fresh, or it informed
                    findings outside the loaded window)
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </PanelChrome>
  )
}
