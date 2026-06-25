/**
 * S7b / UI-5 (Tier F). NATS Consumer-Lag Monitor (`system.stream_lag`).
 *
 * Per-source / per-target consumer lag — the "is anything falling behind?"
 * ops surface mandated by PIVOT §6.1. `num_pending` is the headline lag
 * (messages on the stream a durable consumer hasn't delivered yet);
 * redeliveries flag poison messages; unacked backlog flags a slow consumer.
 *
 * Reads `GET /api/v1/v3/streams/consumer_lag` → `ConsumerLagRow[]` (one row per
 * durable consumer, projecting `NatsStore.consumer_lag()` /
 * `SubscriptionEngine.consumer_lag()`). Polls every 5s.
 *
 * Severity classification + sort live in `@/lib/evalOps` (unit-tested).
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, ApiError } from '@/lib/api'
import {
  lagSeverity,
  sortLag,
  type ConsumerLagRow,
  type LagSeverity,
} from '@/lib/evalOps'
import type { PanelProps } from '@/types'

const SEV_PILL: Record<LagSeverity, string> = {
  ok: 'bg-emerald-900 text-emerald-200',
  warn: 'bg-amber-900 text-amber-200',
  critical: 'bg-rose-900 text-rose-200',
}
const SEV_NUM: Record<LagSeverity, string> = {
  ok: 'text-slate-300',
  warn: 'text-amber-300',
  critical: 'text-rose-300',
}

const SCOPES = ['all', 'source', 'target'] as const

export default function StreamLagPanel({ registration }: PanelProps) {
  const [scope, setScope] = useState<(typeof SCOPES)[number]>('all')
  const [query, setQuery] = useState('')

  const { data, isLoading, error, refetch } = useQuery<ConsumerLagRow[]>({
    queryKey: ['consumer-lag'],
    queryFn: async () => {
      try {
        return await apiGet<ConsumerLagRow[]>('/v3/streams/consumer_lag')
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return []
        throw e
      }
    },
    refetchInterval: 5_000,
  })

  const filtered = useMemo(() => {
    const rows = (data ?? []).filter((r) => {
      if (scope !== 'all' && r.scope_kind !== scope) return false
      if (query && !`${r.scope_id} ${r.durable}`.toLowerCase().includes(query.toLowerCase()))
        return false
      return true
    })
    return sortLag(rows)
  }, [data, scope, query])

  const summary = useMemo(() => {
    const rows = data ?? []
    let critical = 0
    let warn = 0
    let totalPending = 0
    for (const r of rows) {
      const s = lagSeverity(r)
      if (s === 'critical') critical++
      else if (s === 'warn') warn++
      totalPending += r.num_pending
    }
    return { critical, warn, totalPending, total: rows.length }
  }, [data])

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${summary.total} consumers · ${summary.totalPending} pending${
        summary.critical ? ` · ${summary.critical} critical` : ''
      }`}
      onRefresh={() => refetch()}
    >
      <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={scope}
          onChange={(e) => setScope(e.target.value as (typeof SCOPES)[number])}
          data-testid="lag-scope-filter"
        >
          {SCOPES.map((s) => (
            <option key={s} value={s}>
              scope: {s}
            </option>
          ))}
        </select>
        <input
          className="flex-1 min-w-[140px] bg-surface-200 border border-slate-700 rounded p-1 px-2"
          placeholder="filter by id / durable…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          data-testid="lag-query"
        />
        {isLoading && <span className="text-slate-500">loading…</span>}
        {summary.warn > 0 && (
          <span className="text-amber-400 text-[10px]">{summary.warn} warn</span>
        )}
      </div>

      {error instanceof Error && (
        <div className="text-rose-400 text-sm">error: {error.message}</div>
      )}

      <div className="flex-1 overflow-auto text-xs" data-testid="lag-list">
        {!isLoading && filtered.length === 0 && (
          <div className="text-slate-500 text-center py-4">
            no consumers — no per-source / per-target subscription is registered
          </div>
        )}
        {filtered.length > 0 && (
          <table className="w-full">
            <thead className="text-slate-500 text-[10px] uppercase tracking-wide border-b border-slate-800 sticky top-0 bg-surface-100">
              <tr>
                <th className="py-1 px-1 text-left">consumer</th>
                <th className="py-1 px-1 text-left">scope</th>
                <th className="py-1 px-1 text-right">pending</th>
                <th className="py-1 px-1 text-right">unacked</th>
                <th className="py-1 px-1 text-right">redeliv</th>
                <th className="py-1 px-1 text-left">health</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((r) => {
                const sev = lagSeverity(r)
                return (
                  <tr
                    key={`${r.stream}:${r.durable}`}
                    className="border-b border-slate-800/40 hover:bg-surface-100"
                    data-testid={`lag-row-${r.scope_id}`}
                  >
                    <td className="py-1 px-1 font-mono text-slate-300 truncate max-w-[200px]">
                      {r.durable}
                      <span className="text-slate-600"> · {r.stream}</span>
                    </td>
                    <td className="py-1 px-1 text-slate-500">
                      <span className="text-slate-600">{r.scope_kind}/</span>
                      {r.scope_id}
                    </td>
                    <td className={`py-1 px-1 text-right font-mono ${SEV_NUM[sev]}`}>
                      {r.num_pending}
                    </td>
                    <td className="py-1 px-1 text-right font-mono text-slate-400">
                      {r.num_ack_pending}
                    </td>
                    <td
                      className={`py-1 px-1 text-right font-mono ${
                        r.num_redelivered > 0 ? 'text-rose-400' : 'text-slate-500'
                      }`}
                    >
                      {r.num_redelivered}
                    </td>
                    <td className="py-1 px-1">
                      <span
                        className={`rounded px-1 text-[10px] ${SEV_PILL[sev]}`}
                        data-testid={`lag-sev-${sev}`}
                      >
                        {sev}
                      </span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>
    </PanelChrome>
  )
}
