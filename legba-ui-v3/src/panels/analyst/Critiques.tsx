/**
 * A5. Critic Scores (`analyst.critiques`) — L-175 critic outputs.
 *
 * Reads `GET /api/v1/analysts/{id}/critiques?limit=` (substrate-reads) → the
 * critique history *of* the scoped analyst's outputs (the scoped analyst is the
 * `analyzed_analyst_id`; `judge_analyst_id` is the critic that scored it). One
 * row per critiqued output, with the per-rubric-axis breakdown + an overall.
 *
 * Renders the rubric breakdown (per-axis bars), the overall trend across the
 * window, the revision delta (what the critic flagged for revision), and a
 * lineage deep-link to the critiqued trace.
 *
 * Row shape mirrors `AnalystCritiqueRow` (runtime_telemetry_api):
 *   { id, trace_id, analyzed_analyst_id, judge_analyst_id, overall_score,
 *     rubric_uri, scores, revision_delta, produced_at }
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, ApiError } from '@/lib/api'
import { critScoreTrend, scoreBand, type ScoreBand } from '@/lib/evalOps'
import type { PanelProps } from '@/types'
import { selectRow } from '@/state/selection'

interface CritiqueRow {
  id: string
  trace_id: string
  analyzed_analyst_id: string
  judge_analyst_id: string
  overall_score: number | null
  rubric_uri: string
  scores: Record<string, number>
  revision_delta: Record<string, unknown> | null
  produced_at: string
}

interface CritiquesResponse {
  items: CritiqueRow[]
  next_cursor: string | null
}

const BAND_PILL: Record<ScoreBand, string> = {
  good: 'bg-accent-ok/30 text-accent-ok',
  warn: 'bg-accent-warning/30 text-accent-warning',
  bad: 'bg-accent-critical/30 text-accent-critical',
}

function openLineage(traceId: string) {
  // Redesign Move 2: unified selection store (opens the Inspector). 'trace'
  // is not a first-class kind — it coerces to a finding-style Inspector path.
  selectRow('trace', traceId, undefined, { origin: 'analyst-critiques' })
}

export default function CritiquesPanel({ registration, scope }: PanelProps) {
  const analyst_id = scope.analyst_id ?? registration.analyst_id ?? '(unbound)'
  const bound = analyst_id !== '(unbound)'

  const { data, error, isLoading, refetch } = useQuery<CritiquesResponse>({
    queryKey: ['analyst-critiques-panel', analyst_id],
    enabled: bound,
    refetchInterval: 30_000,
    queryFn: async () => {
      try {
        return await apiGet<CritiquesResponse>(
          `/analysts/${encodeURIComponent(analyst_id)}/critiques?limit=50`,
        )
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { items: [], next_cursor: null }
        throw e
      }
    },
  })

  const rows = data?.items ?? []

  // mean overall + per-axis mean across the window — "is what this critic
  // judges trending up or down?"
  const summary = useMemo(() => {
    const scored = rows.filter((r) => typeof r.overall_score === 'number')
    const meanOverall = scored.length
      ? scored.reduce((a, r) => a + (r.overall_score ?? 0), 0) / scored.length
      : null
    const axisSum: Record<string, number> = {}
    const axisN: Record<string, number> = {}
    for (const r of rows) {
      for (const [axis, v] of Object.entries(r.scores ?? {})) {
        axisSum[axis] = (axisSum[axis] ?? 0) + v
        axisN[axis] = (axisN[axis] ?? 0) + 1
      }
    }
    const axisMeans = Object.fromEntries(
      Object.keys(axisSum).map((a) => [a, axisSum[a] / axisN[a]]),
    )
    return { meanOverall, axisMeans, judged: scored.length }
  }, [rows])

  // trend chart reuses the eval helper (it keys on produced_at + overall_score).
  const trend = useMemo(
    () =>
      critScoreTrend(
        rows
          .filter((r) => typeof r.overall_score === 'number')
          .map((r) => ({
            id: r.id,
            analyst_id: r.judge_analyst_id,
            analyst_version: null,
            scores: r.scores ?? {},
            overall_score: r.overall_score as number,
            produced_at: r.produced_at,
          })),
      ),
    [rows],
  )

  return (
    <PanelChrome
      registration={registration}
      subtitle={`critiques of ${analyst_id}`}
      onRefresh={() => refetch()}
    >
      {!bound && (
        <div className="text-xs text-slate-400">
          unbound — open this panel scoped to a critic analyst.
        </div>
      )}
      {bound && isLoading && <div className="text-xs text-slate-400">Loading critiques…</div>}
      {error && <div className="text-xs text-accent-critical">{(error as Error).message}</div>}
      {bound && !isLoading && rows.length === 0 && !error && (
        <div className="text-xs text-slate-400">No critiques recorded yet.</div>
      )}

      {rows.length > 0 && (
        <>
          {/* window summary — mean overall + per-axis mean bars */}
          <div className="bg-surface-50/40 rounded p-2 mb-2 text-[11px]">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-slate-400">window mean</span>
              {summary.meanOverall !== null ? (
                <span
                  className={`px-1.5 py-0.5 rounded font-mono ${BAND_PILL[scoreBand(summary.meanOverall)]}`}
                >
                  {(summary.meanOverall * 100).toFixed(0)}
                </span>
              ) : (
                <span className="text-slate-600">—</span>
              )}
              <span className="ml-auto text-slate-500">{summary.judged} judged</span>
            </div>
            {Object.entries(summary.axisMeans).map(([axis, v]) => (
              <div key={axis} className="flex items-center gap-2 mt-1">
                <span className="w-24 shrink-0 text-slate-400 truncate">{axis}</span>
                <div className="flex-1 h-1.5 bg-surface-200 rounded overflow-hidden">
                  <div
                    className={`h-full ${
                      scoreBand(v) === 'good'
                        ? 'bg-accent-ok'
                        : scoreBand(v) === 'warn'
                          ? 'bg-accent-warning'
                          : 'bg-accent-critical'
                    }`}
                    style={{ width: `${Math.round(v * 100)}%` }}
                  />
                </div>
                <span className="w-9 text-right text-slate-500 font-mono">
                  {(v * 100).toFixed(0)}
                </span>
              </div>
            ))}
          </div>

          {/* sparkline of the overall trend, oldest→newest */}
          {trend.length > 1 && (
            <div className="flex items-end gap-0.5 h-8 mb-2" data-testid="critiques-trend">
              {trend.map((p) => (
                <div
                  key={p.t}
                  className={`flex-1 ${
                    scoreBand(p.overall) === 'good'
                      ? 'bg-accent-ok/60'
                      : scoreBand(p.overall) === 'warn'
                        ? 'bg-accent-warning/60'
                        : 'bg-accent-critical/60'
                  }`}
                  style={{ height: `${Math.max(4, Math.round(p.overall * 100))}%` }}
                  title={`${p.label}: ${(p.overall * 100).toFixed(0)}`}
                />
              ))}
            </div>
          )}

          <ul className="space-y-2">
            {rows.map((c) => (
              <li key={c.id} className="bg-surface-50/40 rounded p-2 text-xs">
                <div className="flex items-center justify-between mb-1">
                  <span className="font-mono text-slate-300 truncate" title="critic (judge)">
                    judged by {c.judge_analyst_id}
                  </span>
                  {c.overall_score !== null && <ScoreBadge score={c.overall_score} />}
                </div>
                <div className="flex flex-wrap gap-x-3 gap-y-1 text-[10px] mb-1">
                  {Object.entries(c.scores ?? {}).map(([axis, score]) => (
                    <span key={axis}>
                      <span className="text-slate-400">{axis}:</span> {Number(score).toFixed(2)}
                    </span>
                  ))}
                </div>
                {c.revision_delta && Object.keys(c.revision_delta).length > 0 && (
                  <div className="text-[11px] text-slate-300 italic mb-1" title="critic revision notes">
                    {typeof c.revision_delta.summary === 'string'
                      ? c.revision_delta.summary
                      : JSON.stringify(c.revision_delta)}
                  </div>
                )}
                <div className="flex items-center gap-2 text-[10px] text-slate-500 mt-1">
                  <button
                    onClick={() => openLineage(c.trace_id)}
                    className="font-mono text-accent-info hover:text-blue-300 underline"
                    data-testid={`critique-trace-${c.id}`}
                    title="open the critiqued trace in Lineage"
                  >
                    trace {c.trace_id.slice(0, 8)} →
                  </button>
                  <span className="font-mono truncate">{c.rubric_uri}</span>
                  <span className="ml-auto">{new Date(c.produced_at).toLocaleString()}</span>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </PanelChrome>
  )
}

function ScoreBadge({ score }: { score: number }) {
  const pct = (score * 100).toFixed(0)
  return (
    <span className={`px-1.5 py-0.5 rounded text-[10px] font-mono ${BAND_PILL[scoreBand(score)]}`}>
      {pct}
    </span>
  )
}
