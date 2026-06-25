/**
 * A3. Cross-target Analyst (`analyst.cross_target`).
 *
 * For `cross_target_raw` and `cross_analyst_correlator` analyst kinds. Reads
 * `GET /api/v1/findings?analyst_id=&limit=` — the analyst's emitted outputs
 * across every target it subscribes to — and presents two cuts:
 *
 *  - **Per-target contribution**: outputs grouped by `target_id` with counts +
 *    a top-severity marker, so an operator sees which targets this analyst is
 *    actually producing for (and which are silent).
 *  - **Correlation surface**: when the analyst is a correlator, each finding's
 *    `data.correlation_type` (`contradiction` | `agreement` | `blind_spot`) +
 *    `referenced_analyst_ids` are surfaced, **contradiction-first** — the
 *    highest-leverage signal (two analysts disagreeing about one target).
 *
 * Both cuts are reconstructed client-side from the frozen `/findings` shape +
 * the correlator's `data` payload (see `cross_analyst_correlator.py`). Rows
 * deep-link to Lineage.
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet } from '@/lib/api'
import { severityRank } from '@/lib/findingsViews'
import type { PanelProps } from '@/types'
import { selectRow } from '@/state/selection'

type CorrelationType = 'contradiction' | 'agreement' | 'blind_spot'

interface CrossRow {
  id: string
  kind: string
  title: string
  body: string | null
  confidence: number | null
  severity: string | null
  target_id: string | null
  analyst_id: string | null
  produced_at: string
  derived_from: string[]
  data?: Record<string, unknown> | null
}

interface FindingsResponse {
  data: CrossRow[]
  next_cursor: string | null
}

const CORR_PILL: Record<CorrelationType, string> = {
  contradiction: 'bg-rose-900 text-rose-200',
  agreement: 'bg-emerald-900 text-emerald-200',
  blind_spot: 'bg-amber-900 text-amber-200',
}
const CORR_ORDER: Record<CorrelationType, number> = {
  contradiction: 0,
  agreement: 1,
  blind_spot: 2,
}

function correlationOf(row: CrossRow): CorrelationType | null {
  const t = row.data?.correlation_type
  if (t === 'contradiction' || t === 'agreement' || t === 'blind_spot') return t
  return null
}
function referencedAnalysts(row: CrossRow): string[] {
  const r = row.data?.referenced_analyst_ids
  return Array.isArray(r) ? r.map((x) => String(x)) : []
}

function openLineage(row: CrossRow) {
  // Redesign Move 2: unified selection store (opens the Inspector).
  selectRow(row.kind || 'finding', row.id, row.title ?? undefined, { origin: 'analyst-cross-target' })
}

export default function CrossTargetPanel({ registration, scope }: PanelProps) {
  const analyst_id = scope.analyst_id ?? registration.analyst_id ?? '(unbound)'
  const bound = analyst_id !== '(unbound)'
  const [tab, setTab] = useState<'targets' | 'correlations'>('targets')

  const { data, isLoading, error, refetch } = useQuery<FindingsResponse>({
    queryKey: ['cross-target-feed', analyst_id],
    enabled: bound,
    refetchInterval: 30_000,
    queryFn: () =>
      apiGet<FindingsResponse>(`/findings?analyst_id=${encodeURIComponent(analyst_id)}&limit=200`),
  })

  const rows = data?.data ?? []

  // per-target contribution rollup
  const byTarget = useMemo(() => {
    const map = new Map<string, { count: number; topSev: string | null; latest: string }>()
    for (const r of rows) {
      const key = r.target_id ?? '(no target)'
      const cur = map.get(key) ?? { count: 0, topSev: null, latest: r.produced_at }
      cur.count += 1
      if (severityRank(r.severity) > severityRank(cur.topSev)) cur.topSev = r.severity
      if (Date.parse(r.produced_at) > Date.parse(cur.latest)) cur.latest = r.produced_at
      map.set(key, cur)
    }
    return [...map.entries()]
      .map(([target_id, v]) => ({ target_id, ...v }))
      .sort((a, b) => b.count - a.count)
  }, [rows])

  // correlation findings, contradiction-first
  const correlations = useMemo(
    () =>
      rows
        .map((r) => ({ row: r, type: correlationOf(r) }))
        .filter((x): x is { row: CrossRow; type: CorrelationType } => x.type !== null)
        .sort(
          (a, b) =>
            CORR_ORDER[a.type] - CORR_ORDER[b.type] ||
            Date.parse(b.row.produced_at) - Date.parse(a.row.produced_at),
        ),
    [rows],
  )

  const isCorrelator = correlations.length > 0

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${rows.length} outputs · ${byTarget.length} target${byTarget.length === 1 ? '' : 's'}${
        isCorrelator ? ` · ${correlations.length} correlations` : ''
      }`}
      onRefresh={() => refetch()}
    >
      {!bound && (
        <div className="text-xs text-slate-400">
          unbound — open this panel scoped to a cross-target analyst.
        </div>
      )}

      {bound && (
        <>
          <div className="flex items-center gap-1 mb-2 text-xs border-b border-slate-800">
            <button
              onClick={() => setTab('targets')}
              className={`px-3 py-1 ${
                tab === 'targets'
                  ? 'border-b-2 border-amber-500 text-amber-300'
                  : 'text-slate-500 hover:text-slate-300'
              }`}
              data-testid="crosstarget-tab-targets"
            >
              per-target
            </button>
            <button
              onClick={() => setTab('correlations')}
              className={`px-3 py-1 ${
                tab === 'correlations'
                  ? 'border-b-2 border-amber-500 text-amber-300'
                  : 'text-slate-500 hover:text-slate-300'
              }`}
              data-testid="crosstarget-tab-correlations"
            >
              correlations{isCorrelator ? ` (${correlations.length})` : ''}
            </button>
          </div>

          {isLoading && <div className="text-slate-500 text-sm">loading…</div>}
          {error instanceof Error && (
            <div className="text-rose-400 text-sm">error: {error.message}</div>
          )}

          {tab === 'targets' && (
            <div className="flex-1 overflow-auto text-xs" data-testid="crosstarget-targets">
              {byTarget.length === 0 && !isLoading && (
                <div className="text-slate-500 text-center py-4">no outputs yet</div>
              )}
              <table className="w-full">
                <thead className="text-slate-500 text-left text-[10px] uppercase tracking-wide border-b border-slate-800">
                  <tr>
                    <th className="py-1 px-1">target</th>
                    <th className="py-1 px-1 text-right">outputs</th>
                    <th className="py-1 px-1">top severity</th>
                    <th className="py-1 px-1">latest</th>
                  </tr>
                </thead>
                <tbody>
                  {byTarget.map((t) => (
                    <tr
                      key={t.target_id}
                      className="border-b border-slate-800/40 hover:bg-surface-100"
                    >
                      <td className="py-1 px-1 font-mono text-slate-300 truncate max-w-[160px]">
                        {t.target_id}
                      </td>
                      <td className="py-1 px-1 text-right font-mono text-slate-200">{t.count}</td>
                      <td className="py-1 px-1">
                        {t.topSev ? (
                          <span
                            className={`rounded px-1 text-[10px] ${
                              t.topSev === 'critical'
                                ? 'bg-rose-900 text-rose-200'
                                : t.topSev === 'high'
                                  ? 'bg-amber-900 text-amber-200'
                                  : 'bg-slate-700 text-slate-200'
                            }`}
                          >
                            {t.topSev}
                          </span>
                        ) : (
                          <span className="text-slate-600">—</span>
                        )}
                      </td>
                      <td className="py-1 px-1 text-slate-600">
                        {new Date(t.latest).toLocaleString()}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {tab === 'correlations' && (
            <div className="flex-1 overflow-auto space-y-1 text-xs" data-testid="crosstarget-correlations">
              {correlations.length === 0 && !isLoading && (
                <div className="text-slate-500 text-center py-4">
                  no correlation outputs — this analyst isn't a correlator, or hasn't run yet
                </div>
              )}
              {correlations.map(({ row, type }) => {
                const refs = referencedAnalysts(row)
                return (
                  <button
                    key={row.id}
                    onClick={() => openLineage(row)}
                    className="w-full text-left bg-surface-100 hover:bg-surface-200 border border-slate-800 rounded p-2 block"
                    data-testid={`correlation-${row.id}`}
                  >
                    <div className="flex items-baseline gap-2">
                      <span className={`shrink-0 rounded px-1 text-[10px] ${CORR_PILL[type]}`}>
                        {type}
                      </span>
                      {row.target_id && (
                        <span className="text-slate-500 shrink-0 font-mono truncate max-w-[120px]">
                          {row.target_id}
                        </span>
                      )}
                      <span className="text-slate-200 truncate flex-1">{row.title}</span>
                      <span className="text-slate-600 shrink-0">
                        {new Date(row.produced_at).toLocaleString()}
                      </span>
                    </div>
                    {row.body && <div className="text-slate-400 line-clamp-2 mt-1">{row.body}</div>}
                    {refs.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1">
                        {refs.map((a) => (
                          <span
                            key={a}
                            className="rounded px-1 bg-surface-200 text-slate-400 text-[10px] font-mono"
                          >
                            {a}
                          </span>
                        ))}
                      </div>
                    )}
                  </button>
                )
              })}
            </div>
          )}
        </>
      )}
    </PanelChrome>
  )
}
