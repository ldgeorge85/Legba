/**
 * S3 / UI-5 (Tier E). Eval Scorecard (`system.eval`) — analyst-quality surface.
 *
 * "Is this analyst getting better?" — per-analyst rubric scores over time,
 * critic-judge overall trend, and ground-truth backtest accuracy where present.
 *
 * Reads `GET /api/v1/v3/eval/scorecard?analyst_id=&since=&limit=` — one row per
 * critic judgement (per-axis rubric breakdown + overall + optional backtest
 * accuracy). All grouping / trend / axis-mean logic lives in `@/lib/evalOps`
 * so it is unit-tested without a DOM.
 *
 * Worst-scoring analysts surface first (they need attention). Selecting an
 * analyst expands its critic-score trend chart + per-axis rubric bars.
 *
 * NOTE: the cross-analyst `/v3/eval/scorecard` rollup endpoint is not yet wired
 * in the registry API (404 today). Until it lands, this singleton shows an
 * empty state pointing operators at the per-analyst Critiques panel (A5,
 * `/analysts/{id}/critiques`), which carries the same judge-score data scoped
 * to one analyst. A 404 is treated as "endpoint pending", not an error.
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, ApiError } from '@/lib/api'
import {
  buildScorecards,
  critScoreTrend,
  scoreBand,
  type ScorecardRow,
  type ScoreBand,
} from '@/lib/evalOps'
import type { PanelProps } from '@/types'
import { RecordLink } from '@/components/inspector/RecordLink'

const BAND_PILL: Record<ScoreBand, string> = {
  good: 'bg-emerald-900 text-emerald-200',
  warn: 'bg-amber-900 text-amber-200',
  bad: 'bg-rose-900 text-rose-200',
}
const BAND_BAR: Record<ScoreBand, string> = {
  good: 'bg-emerald-500',
  warn: 'bg-amber-500',
  bad: 'bg-rose-500',
}

export default function EvalScorecardPanel({ registration }: PanelProps) {
  const [expanded, setExpanded] = useState<string | null>(null)
  const [endpointPending, setEndpointPending] = useState(false)

  const { data, isLoading, error, refetch } = useQuery<ScorecardRow[]>({
    queryKey: ['eval-scorecard'],
    queryFn: async () => {
      try {
        const rows = await apiGet<ScorecardRow[]>('/v3/eval/scorecard?limit=500')
        setEndpointPending(false)
        return rows
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) {
          // cross-analyst rollup endpoint not wired yet — empty, not an error.
          setEndpointPending(true)
          return []
        }
        throw e
      }
    },
    refetchInterval: 60_000,
  })

  const cards = useMemo(() => buildScorecards(data ?? []), [data])

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${cards.length} analyst${cards.length === 1 ? '' : 's'} scored`}
      onRefresh={() => refetch()}
    >
      {isLoading && <div className="text-slate-500 text-sm">loading scorecard…</div>}
      {error instanceof Error && (
        <div className="text-rose-400 text-sm">error: {error.message}</div>
      )}

      <div className="flex-1 overflow-auto space-y-2 text-xs" data-testid="eval-scorecard-list">
        {!isLoading && cards.length === 0 && !endpointPending && (
          <div className="text-slate-500 text-center py-4">
            no critic judgements yet — eval loop hasn't scored any analysts
          </div>
        )}
        {!isLoading && cards.length === 0 && endpointPending && (
          <div
            className="text-slate-400 text-center py-4 space-y-1"
            data-testid="eval-endpoint-pending"
          >
            <div>cross-analyst scorecard rollup not yet wired</div>
            <div className="text-[11px] text-slate-500">
              per-analyst critic scores are live in the Critiques panel
              (<code className="font-mono">/analysts/&#123;id&#125;/critiques</code>)
            </div>
          </div>
        )}
        {cards.map((c) => {
          const band = scoreBand(c.latest_overall)
          const open = expanded === c.analyst_id
          const trendSign = c.trend_delta >= 0 ? '+' : ''
          const trendColor =
            c.trend_delta > 0.001
              ? 'text-emerald-400'
              : c.trend_delta < -0.001
                ? 'text-rose-400'
                : 'text-slate-400'
          const trend = critScoreTrend(c.rows)
          return (
            <div
              key={c.analyst_id}
              className="bg-surface-100 border border-slate-800 rounded p-2"
              data-testid={`eval-card-${c.analyst_id}`}
            >
              <div>
                <div className="flex items-baseline gap-2">
                  <button
                    className="flex min-w-0 flex-1 items-baseline gap-2 text-left"
                    onClick={() => setExpanded(open ? null : c.analyst_id)}
                    data-testid={`eval-card-header-${c.analyst_id}`}
                  >
                    <span className={`shrink-0 rounded px-1 text-[10px] font-mono ${BAND_PILL[band]}`}>
                      {(c.latest_overall * 100).toFixed(0)}
                    </span>
                    <span className="truncate text-slate-200">{c.analyst_id}</span>
                  </button>
                  <RecordLink
                    kind="analyst"
                    id={c.analyst_id}
                    label="inspect"
                    origin="eval-scorecard"
                    className="shrink-0 text-[10px]"
                  />
                  <span className={`font-mono ${trendColor}`} title="trend over window">
                    {trendSign}
                    {(c.trend_delta * 100).toFixed(1)}%
                  </span>
                  {c.latest_accuracy !== null && (
                    <span className="text-slate-500 font-mono shrink-0" title="ground-truth backtest accuracy">
                      gt {(c.latest_accuracy * 100).toFixed(0)}%
                    </span>
                  )}
                  <span className="text-slate-600 shrink-0">{c.rows.length} judged</span>
                </div>
                {/* per-axis rubric mean bars */}
                <div className="mt-1.5 space-y-1">
                  {Object.entries(c.axis_means).map(([axis, v]) => (
                    <div key={axis} className="flex items-center gap-2" data-testid={`eval-axis-${c.analyst_id}-${axis}`}>
                      <span className="w-24 shrink-0 text-slate-400 truncate">{axis}</span>
                      <div className="flex-1 h-1.5 bg-surface-200 rounded overflow-hidden">
                        <div
                          className={`h-full ${BAND_BAR[scoreBand(v)]}`}
                          style={{ width: `${Math.round(v * 100)}%` }}
                        />
                      </div>
                      <span className="w-9 text-right text-slate-500 font-mono">
                        {(v * 100).toFixed(0)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>

              {open && trend.length > 1 && (
                <div className="mt-2 border-t border-slate-800 pt-2">
                  <div className="text-slate-500 text-[10px] uppercase tracking-wide mb-1">
                    critic-score trend
                  </div>
                  <div className="h-32" data-testid={`eval-trend-${c.analyst_id}`}>
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={trend} margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#334155" opacity={0.4} />
                        <XAxis dataKey="label" stroke="#94a3b8" fontSize={9} />
                        <YAxis
                          domain={[0, 1]}
                          stroke="#94a3b8"
                          fontSize={9}
                          width={34}
                          tickFormatter={(v: number) => `${(v * 100).toFixed(0)}`}
                        />
                        <Tooltip
                          contentStyle={{
                            background: '#1e293b',
                            border: '1px solid #334155',
                            borderRadius: 4,
                            fontSize: 11,
                          }}
                          labelStyle={{ color: '#cbd5e1' }}
                          formatter={(v: unknown) =>
                            typeof v === 'number' ? [`${(v * 100).toFixed(1)}%`, 'overall'] : [String(v), 'overall']
                          }
                        />
                        <Line
                          type="monotone"
                          dataKey="overall"
                          stroke="#38bdf8"
                          strokeWidth={2}
                          dot={{ r: 2 }}
                          isAnimationActive={false}
                        />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                </div>
              )}
              {open && trend.length <= 1 && (
                <div className="mt-2 border-t border-slate-800 pt-2 text-slate-600 text-[10px]">
                  only one judgement — no trend yet
                </div>
              )}
            </div>
          )
        })}
      </div>
    </PanelChrome>
  )
}
