/**
 * P-9/10. Analyst Detail (`analyst.runs`).
 *
 * Consolidated three-tab view: Runs / Outputs / Critiques against
 * the W2 substrate-read endpoints:
 *  - GET /api/v1/analysts/{id}/runs       (analyst_traces rows)
 *  - GET /api/v1/analysts/{id}/outputs    (analyst_outputs rows)
 *  - GET /api/v1/analysts/{id}/critiques  (analyst_critiques rows)
 *
 * Each tab cursor-paginates independently. Output / critique row clicks
 * dispatch `legba:open-lineage` so the Lineage panel picks up the deep
 * link.
 *
 * Replaces the prior separate Runs / Outputs / CrossTarget / Forecasts /
 * Critiques split — one analyst panel that branches on tab.
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, ApiError } from '@/lib/api'
import type { PanelProps } from '@/types'
import { selectRow } from '@/state/selection'

interface AnalystRun {
  run_id: string
  analyst_id: string
  analyst_version: string
  target_id: string | null
  cadence_trigger: string
  status: string
  started_at: string
  ended_at: string | null
  duration_ms: number
  token_count: number
  output_count: number
}

interface AnalystOutput {
  id: string
  kind: string
  title: string
  body: string | null
  confidence: number | null
  severity: string | null
  target_id: string | null
  produced_at: string
}

interface AnalystCritique {
  id: string
  trace_id: string
  judge_analyst_id: string
  overall_score: number | null
  rubric_uri: string
  produced_at: string
  scores: Record<string, number>
}

type Tab = 'runs' | 'outputs' | 'critiques'
const TABS: Array<{ key: Tab; label: string }> = [
  { key: 'runs', label: 'Runs' },
  { key: 'outputs', label: 'Outputs' },
  { key: 'critiques', label: 'Critiques' },
]

function statusColor(status: string): string {
  if (status === 'success' || status === 'ok') return 'text-emerald-400'
  if (status === 'fail' || status === 'error') return 'text-rose-400'
  if (status === 'running') return 'text-sky-400'
  if (status === 'budget_paused' || status === 'noop') return 'text-amber-400'
  return 'text-slate-400'
}

function openLineage(rowKind: string, rowId: string) {
  // Redesign Move 2: unified selection store (opens the Inspector).
  selectRow(rowKind, rowId, undefined, { origin: 'analyst-runs' })
}

export default function AnalystDetailPanel({ registration, scope }: PanelProps) {
  const analyst_id = scope.analyst_id ?? registration.analyst_id ?? registration.descriptor_id
  const [tab, setTab] = useState<Tab>('runs')

  const runsQ = useQuery<{ items: AnalystRun[]; next_cursor: string | null }>({
    enabled: tab === 'runs' && !!analyst_id,
    queryKey: ['analyst-runs', analyst_id],
    queryFn: async () => {
      try {
        return await apiGet<{ items: AnalystRun[]; next_cursor: string | null }>(
          `/analysts/${encodeURIComponent(analyst_id)}/runs?limit=50`,
        )
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { items: [], next_cursor: null }
        throw e
      }
    },
    refetchInterval: 30_000,
  })

  const outputsQ = useQuery<{ items: AnalystOutput[]; next_cursor: string | null }>({
    enabled: tab === 'outputs' && !!analyst_id,
    queryKey: ['analyst-outputs', analyst_id],
    queryFn: async () => {
      try {
        return await apiGet<{ items: AnalystOutput[]; next_cursor: string | null }>(
          `/analysts/${encodeURIComponent(analyst_id)}/outputs?limit=50`,
        )
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { items: [], next_cursor: null }
        throw e
      }
    },
    refetchInterval: 30_000,
  })

  const critiquesQ = useQuery<{ items: AnalystCritique[]; next_cursor: string | null }>({
    enabled: tab === 'critiques' && !!analyst_id,
    queryKey: ['analyst-critiques', analyst_id],
    queryFn: async () => {
      try {
        return await apiGet<{ items: AnalystCritique[]; next_cursor: string | null }>(
          `/analysts/${encodeURIComponent(analyst_id)}/critiques?limit=50`,
        )
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { items: [], next_cursor: null }
        throw e
      }
    },
    refetchInterval: 30_000,
  })

  const runs = runsQ.data?.items ?? []
  const outputs = outputsQ.data?.items ?? []
  const critiques = critiquesQ.data?.items ?? []

  const aggregate =
    tab === 'runs'
      ? {
          total: runs.length,
          ok: runs.filter((r) => r.status === 'success' || r.status === 'ok').length,
          fail: runs.filter((r) => r.status === 'fail' || r.status === 'error').length,
          tokens: runs.reduce((acc, r) => acc + r.token_count, 0),
        }
      : null

  return (
    <PanelChrome
      registration={registration}
      subtitle={`analyst ${analyst_id}`}
      onRefresh={() => {
        if (tab === 'runs') runsQ.refetch()
        if (tab === 'outputs') outputsQ.refetch()
        if (tab === 'critiques') critiquesQ.refetch()
      }}
    >
      <div className="flex items-center gap-1 mb-2 text-xs border-b border-slate-800">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`px-3 py-1 ${
              tab === t.key
                ? 'border-b-2 border-amber-500 text-amber-300'
                : 'text-slate-500 hover:text-slate-300'
            }`}
            data-testid={`analyst-tab-${t.key}`}
          >
            {t.label}
          </button>
        ))}
        {aggregate && tab === 'runs' && (
          <div className="ml-auto flex gap-3 text-slate-500 text-[11px]">
            <span>ok: {aggregate.ok}</span>
            {aggregate.fail > 0 && <span className="text-rose-400">fail: {aggregate.fail}</span>}
            <span>tokens: {aggregate.tokens.toLocaleString()}</span>
          </div>
        )}
      </div>

      {tab === 'runs' && (
        <div className="flex-1 overflow-auto text-xs">
          {runsQ.isLoading && <div className="text-slate-500">loading runs…</div>}
          {runs.length === 0 && !runsQ.isLoading && (
            <div className="text-slate-500 text-center py-4">no runs</div>
          )}
          <table className="w-full">
            <thead className="text-slate-500 text-left text-[10px] uppercase tracking-wide border-b border-slate-800">
              <tr>
                <th className="py-1 px-1">when</th>
                <th className="py-1 px-1">target</th>
                <th className="py-1 px-1">trigger</th>
                <th className="py-1 px-1">status</th>
                <th className="py-1 px-1 text-right">dur</th>
                <th className="py-1 px-1 text-right">tokens</th>
                <th className="py-1 px-1 text-right">outs</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.run_id} className="border-b border-slate-800/40 hover:bg-surface-100">
                  <td className="py-1 px-1 font-mono text-slate-500">
                    {new Date(r.started_at).toLocaleTimeString()}
                  </td>
                  <td className="py-1 px-1 text-slate-400 truncate max-w-[120px]">
                    {r.target_id ?? '—'}
                  </td>
                  <td className="py-1 px-1 text-slate-500">{r.cadence_trigger}</td>
                  <td className={`py-1 px-1 font-mono ${statusColor(r.status)}`}>{r.status}</td>
                  <td className="py-1 px-1 font-mono text-right">
                    {(r.duration_ms / 1000).toFixed(1)}s
                  </td>
                  <td className="py-1 px-1 font-mono text-right">{r.token_count.toLocaleString()}</td>
                  <td className="py-1 px-1 text-right">{r.output_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {tab === 'outputs' && (
        <div className="flex-1 overflow-auto text-xs space-y-1">
          {outputsQ.isLoading && <div className="text-slate-500">loading outputs…</div>}
          {outputs.length === 0 && !outputsQ.isLoading && (
            <div className="text-slate-500 text-center py-4">no outputs</div>
          )}
          {outputs.map((o) => (
            <button
              key={o.id}
              onClick={() => openLineage(o.kind, o.id)}
              className="w-full text-left bg-surface-100 hover:bg-surface-200 border border-slate-800 rounded p-2"
            >
              <div className="flex items-baseline gap-2">
                <span className="text-slate-500 shrink-0 w-20">{o.kind}</span>
                {o.severity && (
                  <span
                    className={`shrink-0 rounded px-1 text-[10px] ${
                      o.severity === 'critical'
                        ? 'bg-rose-900 text-rose-200'
                        : o.severity === 'high'
                          ? 'bg-amber-900 text-amber-200'
                          : 'bg-slate-700 text-slate-200'
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
              {o.target_id && (
                <div className="text-slate-600 mt-1">target: {o.target_id}</div>
              )}
            </button>
          ))}
        </div>
      )}

      {tab === 'critiques' && (
        <div className="flex-1 overflow-auto text-xs space-y-1">
          {critiquesQ.isLoading && <div className="text-slate-500">loading critiques…</div>}
          {critiques.length === 0 && !critiquesQ.isLoading && (
            <div className="text-slate-500 text-center py-4">no critiques</div>
          )}
          {critiques.map((c) => (
            <button
              key={c.id}
              onClick={() => openLineage('critique', c.id)}
              className="w-full text-left bg-surface-100 hover:bg-surface-200 border border-slate-800 rounded p-2"
            >
              <div className="flex items-baseline gap-2">
                <span className="text-slate-500 shrink-0 w-32 truncate">{c.judge_analyst_id}</span>
                {c.overall_score !== null && (
                  <span
                    className={`shrink-0 rounded px-1 text-[10px] ${
                      c.overall_score >= 0.8
                        ? 'bg-emerald-900 text-emerald-200'
                        : c.overall_score >= 0.5
                          ? 'bg-amber-900 text-amber-200'
                          : 'bg-rose-900 text-rose-200'
                    }`}
                  >
                    score: {c.overall_score.toFixed(2)}
                  </span>
                )}
                <span className="text-slate-600 shrink-0 ml-auto">
                  {new Date(c.produced_at).toLocaleString()}
                </span>
              </div>
              <div className="text-slate-500 mt-1 truncate">rubric: {c.rubric_uri}</div>
              {c.scores && Object.keys(c.scores).length > 0 && (
                <div className="flex gap-3 mt-1 text-slate-600 text-[10px]">
                  {Object.entries(c.scores)
                    .slice(0, 5)
                    .map(([k, v]) => (
                      <span key={k}>
                        {k}: {Number(v).toFixed(2)}
                      </span>
                    ))}
                </div>
              )}
            </button>
          ))}
        </div>
      )}
    </PanelChrome>
  )
}
