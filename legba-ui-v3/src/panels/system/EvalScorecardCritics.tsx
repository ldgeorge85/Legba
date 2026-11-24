/**
 * The per-analyst critic rollup of the Accuracy page's grader section (UI-1),
 * moved out of the panel file. "Is this analyst getting better?" — per-analyst
 * rubric scores, the critic-judge overall trend, ground-truth backtest
 * accuracy where present. Worst-scoring analysts surface first; selecting one
 * expands its trend chart. A memory-only verdict (`score_state`
 * `carried_ungraded`) is counted on its card as *N carried, ungraded* and kept
 * out of every figure on the card, so an analyst with only such verdicts reads
 * `—`, never the capped 50. All grouping / trend logic lives in `@/lib/evalOps`.
 */
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
import { RecordLink } from '@/components/inspector/RecordLink'
import { humanizeId } from '@/lib/deskNames'
import { buildScorecards, critScoreTrend, scoreBand, type ScorecardRow, type ScoreBand } from '@/lib/evalOps'

const displayName = humanizeId

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

export function CriticRollup({
  rows,
  isLoading,
  endpointPending,
}: {
  rows: ScorecardRow[] | undefined
  isLoading: boolean
  endpointPending: boolean
}) {
  const [expanded, setExpanded] = useState<string | null>(null)
  const cards = useMemo(() => buildScorecards(rows ?? []), [rows])
  return (
      <div className="flex-1 overflow-auto space-y-2 text-xs" data-testid="eval-scorecard-list">
        {!isLoading && cards.length === 0 && !endpointPending && (
          <div className="text-ink-3 text-center py-4">
            no critic judgements yet — eval loop hasn't scored any analysts
          </div>
        )}
        {!isLoading && cards.length === 0 && endpointPending && (
          <div
            className="text-ink-2 text-center py-4 space-y-1"
            data-testid="eval-endpoint-pending"
          >
            <div>cross-analyst scorecard rollup not yet wired</div>
            <div className="text-label text-ink-3">
              per-analyst critic scores are live in the Critiques panel
              (<code className="font-mono">/analysts/&#123;id&#125;/critiques</code>)
            </div>
          </div>
        )}
        {cards.map((c) => {
          const band = c.latest_overall === null ? null : scoreBand(c.latest_overall)
          const open = expanded === c.analyst_id
          const delta = c.trend_delta
          const trendSign = delta !== null && delta >= 0 ? '+' : ''
          const trendColor =
            delta !== null && delta > 0.001
              ? 'text-emerald-400'
              : delta !== null && delta < -0.001
                ? 'text-rose-400'
                : 'text-ink-2'
          const trend = critScoreTrend(c.rows)
          return (
            <div
              key={c.analyst_id}
              className="bg-surf-2 border border-line rounded p-2"
              data-testid={`eval-card-${c.analyst_id}`}
            >
              <div>
                <div className="flex items-baseline gap-2">
                  <button
                    className="flex min-w-0 flex-1 items-baseline gap-2 text-left"
                    onClick={() => setExpanded(open ? null : c.analyst_id)}
                    data-testid={`eval-card-header-${c.analyst_id}`}
                  >
                    <span
                      className={`shrink-0 rounded px-1 text-label font-mono ${band ? BAND_PILL[band] : 'text-ink-3'}`}
                      title={c.latest_overall === null ? 'no graded verdict in the window' : undefined}
                    >
                      {c.latest_overall === null ? '—' : (c.latest_overall * 100).toFixed(0)}
                    </span>
                    <span className="truncate text-ink-1" title={c.analyst_id}>
                      {displayName(c.analyst_id)}
                    </span>
                  </button>
                  <RecordLink
                    kind="analyst"
                    id={c.analyst_id}
                    label="inspect"
                    origin="eval-scorecard"
                    className="shrink-0 text-label"
                  />
                  <span className={`font-mono ${trendColor}`} title="trend over window">
                    {delta === null ? '—' : `${trendSign}${(delta * 100).toFixed(1)}%`}
                  </span>
                  {c.latest_accuracy !== null && (
                    <span className="text-ink-3 font-mono shrink-0" title="ground-truth backtest accuracy">
                      gt {(c.latest_accuracy * 100).toFixed(0)}%
                    </span>
                  )}
                  <span className="text-ink-3 shrink-0">{c.rows.length} judged</span>
                  {c.carried_ungraded_n > 0 && (
                    <span
                      className="text-ink-3 shrink-0"
                      data-testid={`eval-carried-${c.analyst_id}`}
                      title="memory-only verdicts: every citation was the desk's own prior read or window ledger, so nothing was graded against this cycle's evidence — counted here, kept out of every score on this card"
                    >
                      · {c.carried_ungraded_n} carried, ungraded
                    </span>
                  )}
                </div>
                {/* per-axis rubric mean bars */}
                <div className="mt-1.5 space-y-1">
                  {Object.entries(c.axis_means).map(([axis, v]) => (
                    <div key={axis} className="flex items-center gap-2" data-testid={`eval-axis-${c.analyst_id}-${axis}`}>
                      <span className="w-24 shrink-0 text-ink-2 truncate">{axis}</span>
                      <div className="flex-1 h-1.5 bg-surf-1 rounded overflow-hidden">
                        <div
                          className={`h-full ${BAND_BAR[scoreBand(v)]}`}
                          style={{ width: `${Math.round(v * 100)}%` }}
                        />
                      </div>
                      <span className="w-9 text-right text-ink-3 font-mono">
                        {(v * 100).toFixed(0)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>

              {open && trend.length > 1 && (
                <div className="mt-2 border-t border-line pt-2">
                  <div className="text-ink-3 text-label uppercase tracking-wide mb-1">
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
                <div className="mt-2 border-t border-line pt-2 text-ink-3 text-label">
                  only one judgement — no trend yet
                </div>
              )}
            </div>
          )
        })}
      </div>
  )
}
