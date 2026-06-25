/**
 * S2. Budget Ledger (`system.budget`).
 *
 * Reads three sibling endpoints:
 *   GET /api/v1/budget/ledger     — per-analyst tokens/runs/cost rows
 *   GET /api/v1/budget/envelope   — global per-bucket cap + current spend
 *   GET /api/v1/budget/demotions  — recent budget-exhaustion demote events
 *
 * The ledger groups by (analyst_id, bucket); the envelope is the
 * system-wide cap.  Demote events surface when an analyst tripped the
 * global cap and got auto-paused / model-downgraded.
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet } from '@/lib/api'
import type { PanelProps } from '@/types'

/** Pass-4 chart palette — cycled per analyst line. Matches the
 *  emerald / amber / rose / slate accent set used elsewhere. */
const LINE_COLORS = [
  '#34d399', // emerald-400
  '#60a5fa', // blue-400
  '#fbbf24', // amber-400
  '#fb7185', // rose-400
  '#a78bfa', // violet-400
  '#22d3ee', // cyan-400
  '#f472b6', // pink-400
  '#94a3b8', // slate-400
]

interface LedgerRow {
  analyst_id: string
  analyst_version: string
  bucket: string
  tokens_used: number
  runs: number
  cost_usd: string
  cost_estimate_usd: string
  last_updated: string
}

interface EnvelopeRow {
  bucket: string
  tokens_cap: number | null
  usd_cap: string | null
  on_exceeded: string | null
  note: string | null
  current_tokens: number
  current_cost_usd: string
  demoted: string | null
  last_updated: string | null
}

interface DemotionRow {
  id: string
  analyst_id: string
  analyst_version: string
  bucket: string
  cause: string
  tokens_used_at_demote: number | null
  tokens_cap_at_demote: number | null
  primary_llm: string | null
  fallback_llm: string | null
  /** when the budget cap was tripped (DemotionEvent.occurred_at) */
  occurred_at: string
}

export default function BudgetPanel({ registration }: PanelProps) {
  const ledgerQ = useQuery<LedgerRow[]>({
    queryKey: ['budget-ledger'],
    queryFn: () => apiGet<LedgerRow[]>('/budget/ledger?limit=50'),
    refetchInterval: 30_000,
  })
  const envelopeQ = useQuery<EnvelopeRow>({
    queryKey: ['budget-envelope'],
    queryFn: () => apiGet<EnvelopeRow>('/budget/envelope'),
    refetchInterval: 30_000,
  })
  const demoQ = useQuery<DemotionRow[]>({
    queryKey: ['budget-demotions'],
    queryFn: () => apiGet<DemotionRow[]>('/budget/demotions?limit=20'),
    refetchInterval: 60_000,
  })

  const totals = useMemo(() => {
    const rows = ledgerQ.data ?? []
    return {
      tokens: rows.reduce((a, r) => a + r.tokens_used, 0),
      runs: rows.reduce((a, r) => a + r.runs, 0),
      analysts: new Set(rows.map((r) => r.analyst_id)).size,
    }
  }, [ledgerQ.data])

  /** Best-available per-analyst CAP. The `/ledger` endpoint does NOT carry a
   *  per-analyst cap (it returns only `tokens_used`); the descriptor-set
   *  `method.budget_tokens_per_day` lives on the analyst descriptor and is
   *  consulted by the runtime BudgetEnforcer but never joined into `/ledger`.
   *  The one per-analyst cap value the budget surface DOES expose is the
   *  snapshot stamped into a demotion event (`tokens_cap_at_demote`) at the
   *  moment that analyst last tripped its cap. We fold the most-recent such
   *  snapshot in as the displayed cap. Analysts that have never been demoted
   *  show no cap (the honest "uncapped / unknown" state) until the backend
   *  joins the descriptor cap into `/ledger` — see central_changes. */
  const capByAnalyst = useMemo(() => {
    const m = new Map<string, number>()
    // demotions arrive newest-first; first-seen wins (most-recent snapshot).
    for (const d of demoQ.data ?? []) {
      if (d.tokens_cap_at_demote != null && !m.has(d.analyst_id)) {
        m.set(d.analyst_id, d.tokens_cap_at_demote)
      }
    }
    return m
  }, [demoQ.data])

  /** Per-analyst rollup — tokens used (summed across buckets) and runs vs the
   *  best-available cap, sorted so the analysts closest to their cap (most at
   *  risk of demotion) surface first; the uncapped tail follows by raw burn. */
  const perAnalyst = useMemo(() => {
    const agg = new Map<string, { tokens: number; runs: number }>()
    for (const r of ledgerQ.data ?? []) {
      const cur = agg.get(r.analyst_id) ?? { tokens: 0, runs: 0 }
      cur.tokens += r.tokens_used
      cur.runs += r.runs
      agg.set(r.analyst_id, cur)
    }
    const out = Array.from(agg.entries()).map(([analyst_id, a]) => {
      const cap = capByAnalyst.get(analyst_id) ?? null
      const pct = cap && cap > 0 ? Math.min(100, (a.tokens / cap) * 100) : null
      return { analyst_id, tokens: a.tokens, runs: a.runs, cap, pct }
    })
    return out.sort((a, b) => {
      if (a.pct !== null && b.pct !== null) return b.pct - a.pct
      if (a.pct !== null) return -1
      if (b.pct !== null) return 1
      return b.tokens - a.tokens
    })
  }, [ledgerQ.data, capByAnalyst])

  /** Tokens-per-day-per-analyst series for the last 14 days. The
   *  ledger backend already buckets by (analyst_id, bucket); we just
   *  pivot client-side: one row per bucket, one numeric column per
   *  analyst. Buckets that don't parse as dates are skipped. */
  const tokenSeries = useMemo(() => {
    const rows = ledgerQ.data ?? []
    const cutoff = Date.now() - 14 * 24 * 60 * 60 * 1000
    const byBucket = new Map<string, Record<string, number | string>>()
    const analysts = new Set<string>()
    for (const r of rows) {
      const ts = Date.parse(r.bucket)
      if (!Number.isFinite(ts) || ts < cutoff) continue
      const dayKey = new Date(ts).toISOString().slice(0, 10)
      const slot = byBucket.get(dayKey) ?? { bucket: dayKey }
      const prior = Number(slot[r.analyst_id] ?? 0)
      slot[r.analyst_id] = prior + r.tokens_used
      byBucket.set(dayKey, slot)
      analysts.add(r.analyst_id)
    }
    const series = Array.from(byBucket.values()).sort((a, b) =>
      String(a.bucket).localeCompare(String(b.bucket)),
    )
    return { series, analysts: Array.from(analysts).sort() }
  }, [ledgerQ.data])

  const env = envelopeQ.data
  const capPct =
    env && env.tokens_cap !== null && env.tokens_cap > 0
      ? Math.min(100, (env.current_tokens / env.tokens_cap) * 100)
      : null

  return (
    <PanelChrome
      registration={registration}
      subtitle={env ? `bucket ${env.bucket}` : 'budget ledger'}
      onRefresh={() => {
        ledgerQ.refetch()
        envelopeQ.refetch()
        demoQ.refetch()
      }}
    >
      <div className="flex-1 overflow-auto space-y-3 text-xs">
        {/* Envelope summary */}
        <section>
          <div className="text-slate-500 text-[10px] uppercase tracking-wide mb-1">
            global envelope
          </div>
          <div className="bg-surface-100 border border-slate-800 rounded p-2 grid grid-cols-2 gap-x-3 gap-y-1">
            <span className="text-slate-500">tokens</span>
            <span>
              {env?.current_tokens.toLocaleString() ?? '—'}
              {env?.tokens_cap !== null && env?.tokens_cap !== undefined
                ? ` / ${env.tokens_cap.toLocaleString()}`
                : ' (uncapped)'}
              {capPct !== null && (
                <span
                  className={`ml-2 ${
                    capPct >= 90 ? 'text-rose-400' : capPct >= 70 ? 'text-amber-400' : 'text-emerald-400'
                  }`}
                >
                  {capPct.toFixed(0)}%
                </span>
              )}
            </span>
            <span className="text-slate-500">cost (USD)</span>
            <span>
              ${env?.current_cost_usd ?? '0.000000'}
              {env?.usd_cap ? ` / $${env.usd_cap}` : ''}
            </span>
            <span className="text-slate-500">demoted</span>
            <span className={env?.demoted ? 'text-amber-400' : 'text-slate-400'}>
              {env?.demoted ?? '—'}
            </span>
            <span className="text-slate-500">on-exceeded</span>
            <span>{env?.on_exceeded ?? 'no policy'}</span>
          </div>
        </section>

        {/* Tokens-per-day overlay chart (Pass 4) */}
        <section>
          <div className="text-slate-500 text-[10px] uppercase tracking-wide mb-1">
            tokens / day / analyst (last 14d)
          </div>
          {tokenSeries.series.length === 0 ? (
            <div className="text-slate-500 bg-surface-100 border border-slate-800 rounded p-2">
              no per-day ledger rows in the last 14 days
            </div>
          ) : (
            <div className="bg-surface-100 border border-slate-800 rounded p-2 h-48">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart
                  data={tokenSeries.series}
                  margin={{ top: 4, right: 8, bottom: 4, left: 0 }}
                >
                  <CartesianGrid
                    strokeDasharray="3 3"
                    stroke="#334155"
                    opacity={0.4}
                  />
                  <XAxis
                    dataKey="bucket"
                    stroke="#94a3b8"
                    fontSize={10}
                    tickFormatter={(v: string) => v.slice(5)}
                  />
                  <YAxis
                    stroke="#94a3b8"
                    fontSize={10}
                    width={50}
                    tickFormatter={(v: number) =>
                      v >= 1_000_000
                        ? `${(v / 1_000_000).toFixed(1)}M`
                        : v >= 1_000
                          ? `${(v / 1_000).toFixed(0)}k`
                          : String(v)
                    }
                  />
                  <Tooltip
                    contentStyle={{
                      background: '#1e293b',
                      border: '1px solid #334155',
                      borderRadius: 4,
                      fontSize: 11,
                    }}
                    labelStyle={{ color: '#cbd5e1' }}
                  />
                  <Legend
                    wrapperStyle={{ fontSize: 10, color: '#94a3b8' }}
                    iconSize={8}
                  />
                  {tokenSeries.analysts.map((a, i) => (
                    <Line
                      key={a}
                      type="monotone"
                      dataKey={a}
                      stroke={LINE_COLORS[i % LINE_COLORS.length]}
                      strokeWidth={1.5}
                      dot={{ r: 2 }}
                      activeDot={{ r: 4 }}
                      connectNulls
                    />
                  ))}
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
        </section>

        {/* Per-analyst used / cap (Pass 5) */}
        <section>
          <div className="text-slate-500 text-[10px] uppercase tracking-wide mb-1">
            per-analyst used / cap
          </div>
          {perAnalyst.length === 0 ? (
            <div className="text-slate-500 bg-surface-100 border border-slate-800 rounded p-2">
              no analyst budget rows yet
            </div>
          ) : (
            <div className="bg-surface-100 border border-slate-800 rounded p-2 space-y-1.5">
              {perAnalyst.map((a) => {
                const band =
                  a.pct === null
                    ? 'text-slate-400'
                    : a.pct >= 90
                      ? 'text-rose-400'
                      : a.pct >= 70
                        ? 'text-amber-400'
                        : 'text-emerald-400'
                const barColor =
                  a.pct === null
                    ? 'bg-slate-700'
                    : a.pct >= 90
                      ? 'bg-rose-500'
                      : a.pct >= 70
                        ? 'bg-amber-500'
                        : 'bg-emerald-500'
                return (
                  <div key={a.analyst_id}>
                    <div className="flex items-baseline gap-2">
                      <span className="truncate max-w-[180px]">{a.analyst_id}</span>
                      <span className="text-slate-600 text-[10px]">{a.runs} runs</span>
                      <span className="ml-auto font-mono text-slate-400">
                        {a.tokens.toLocaleString()}
                        {a.cap !== null ? (
                          <>
                            {' / '}
                            {a.cap.toLocaleString()}
                            <span className={`ml-1.5 ${band}`}>{a.pct!.toFixed(0)}%</span>
                          </>
                        ) : (
                          <span className="ml-1.5 text-slate-600">(no cap)</span>
                        )}
                      </span>
                    </div>
                    <div className="mt-0.5 h-1 rounded bg-surface-200 overflow-hidden">
                      <div
                        className={`h-full ${barColor}`}
                        style={{ width: `${a.pct ?? 0}%` }}
                      />
                    </div>
                  </div>
                )
              })}
              <div className="text-[10px] text-slate-600 pt-1 border-t border-slate-800/60">
                cap = most-recent <code className="font-mono">tokens_cap_at_demote</code>{' '}
                snapshot; analysts never demoted show no cap (the descriptor cap
                is not yet exposed on <code className="font-mono">/ledger</code>).
              </div>
            </div>
          )}
        </section>

        {/* Per-analyst ledger */}
        <section>
          <div className="text-slate-500 text-[10px] uppercase tracking-wide mb-1">
            per-analyst ledger ({totals.analysts} analysts · {totals.runs} runs · {totals.tokens.toLocaleString()} tokens)
          </div>
          {ledgerQ.isLoading && <div className="text-slate-500">loading…</div>}
          {ledgerQ.data?.length === 0 && !ledgerQ.isLoading && (
            <div className="text-slate-500">no ledger rows</div>
          )}
          <table className="w-full">
            <thead className="text-slate-500 text-[10px] uppercase tracking-wide border-b border-slate-800">
              <tr>
                <th className="py-1 px-1 text-left">analyst</th>
                <th className="py-1 px-1 text-left">bucket</th>
                <th className="py-1 px-1 text-right">tokens</th>
                <th className="py-1 px-1 text-right">runs</th>
                <th className="py-1 px-1 text-right">cost (USD)</th>
              </tr>
            </thead>
            <tbody>
              {(ledgerQ.data ?? []).map((r) => (
                <tr key={`${r.analyst_id}-${r.bucket}`} className="border-b border-slate-800/40 hover:bg-surface-100">
                  <td className="py-1 px-1 truncate max-w-[200px]">{r.analyst_id}</td>
                  <td className="py-1 px-1 text-slate-500 font-mono">{r.bucket}</td>
                  <td className="py-1 px-1 text-right font-mono">{r.tokens_used.toLocaleString()}</td>
                  <td className="py-1 px-1 text-right font-mono">{r.runs}</td>
                  <td className="py-1 px-1 text-right font-mono text-slate-400">${r.cost_usd}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        {/* Demotion events */}
        <section>
          <div className="text-slate-500 text-[10px] uppercase tracking-wide mb-1">
            recent demotion events
          </div>
          {(demoQ.data ?? []).length === 0 && (
            <div className="text-slate-500">no demotions</div>
          )}
          {(demoQ.data ?? []).map((d) => (
            <div key={d.id} className="bg-surface-100 border border-amber-800/40 rounded p-2 mb-1">
              <div className="flex items-baseline gap-3">
                <span className="text-amber-300">{d.cause}</span>
                <span className="text-slate-400 truncate">{d.analyst_id}</span>
                <span className="text-slate-600 ml-auto">{new Date(d.occurred_at).toLocaleString()}</span>
              </div>
              <div className="text-slate-500 mt-1">
                {d.primary_llm || '(no model)'} → {d.fallback_llm || '(paused)'}
                {d.tokens_cap_at_demote !== null && (
                  <span className="ml-2">
                    {d.tokens_used_at_demote?.toLocaleString() ?? '?'} / {d.tokens_cap_at_demote.toLocaleString()}
                  </span>
                )}
              </div>
            </div>
          ))}
        </section>
      </div>
    </PanelChrome>
  )
}
