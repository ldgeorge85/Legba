/**
 * S8 / UI-5 (Tier F). Governor Events (`system.governor`).
 *
 * The per-pack governor's BLOCK/ALLOW + escalation decision stream (P-11
 * hard-gate audit surface). Every governor decision lands a durable row in
 * `governor_events` and fans out (best-effort) on `governor.events.>`.
 *
 * Reads `GET /api/v1/registry/governor_events?pack_id=&decision=&limit=`
 * (durable source of truth) and live-tails the `governor.events.>` subject via
 * the registry-events WS multiplexer so a blocked call shows in real time
 * without grepping logs.
 *
 * BLOCK rows are emphasised (this is the surface an operator watches when a
 * pack is being throttled). Summary header counts blocked packs + causes.
 * Mapping / summary logic lives in `@/lib/evalOps`.
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, ApiError } from '@/lib/api'
import { subscribeRegistryEvents } from '@/lib/ws'
import {
  GOVERNOR_TAIL_FILTER,
  mapGovernorEnvelope,
  summariseGovernor,
  type GovernorEventRow,
} from '@/lib/evalOps'
import type { PanelProps } from '@/types'

type DecisionFilter = 'all' | 'block' | 'allow'

function rowKey(r: GovernorEventRow): string {
  return r._key ?? `${r.pack_id}:${r.decision}:${r.occurred_at}:${r.tool_name ?? ''}`
}

/**
 * Short, operator-actionable remediation hint per BLOCK cause. Causes mirror
 * the governor's pre-call gate (`legba.data.analysts.agency.governor`):
 * `global_exhausted`, `over_rate`, `over_budget`, `unknown_tool`. The hint
 * says what to change to unblock the analyst — these are read-only panels, so
 * the value is "what to do next", not a button.
 */
function remediationFor(r: GovernorEventRow): string | null {
  if (r.decision !== 'block') return null
  const cap =
    r.cap_dimension && r.cap_limit != null
      ? `${r.cap_dimension} cap ${r.cap_limit}`
      : 'the cap'
  switch (r.cause) {
    case 'global_exhausted':
      return `System-wide token envelope is exhausted — every pack is gated, not just this one. Raise global_budget_envelope.tokens_cap for today (Budget panel) or wait for the next UTC-day reset.`
    case 'over_rate':
      return `Pack exceeded ${cap} (a trailing-window rate limit). Lower this analyst's call frequency, or raise ${r.cap_dimension ?? 'the rate cap'} on the action-pack governor if the burst is expected.`
    case 'over_budget':
      return `Pack would cross ${cap} (per-day cost). Raise max_cost_usd_per_day on the action-pack governor, or let it reset at the next UTC day.`
    case 'unknown_tool':
      return `Tool ${r.tool_name ?? '(unnamed)'} is not in this pack's allow-list. Add it to the action-pack's allowed tools, or correct the analyst's tool call.`
    default:
      return r.detail
        ? `Blocked (${r.cause}). ${r.detail}`
        : `Blocked by the per-pack governor (${r.cause}).`
  }
}

export default function GovernorEventsPanel({ registration }: PanelProps) {
  const qc = useQueryClient()
  const [decision, setDecision] = useState<DecisionFilter>('all')
  const [packFilter, setPackFilter] = useState('')
  const [tailOn, setTailOn] = useState(true)
  const [live, setLive] = useState<GovernorEventRow[]>([])

  const params = useMemo(() => {
    const p = new URLSearchParams({ limit: '100' })
    if (decision !== 'all') p.set('decision', decision)
    if (packFilter.trim()) p.set('pack_id', packFilter.trim())
    return p
  }, [decision, packFilter])

  const { data, isLoading, error, refetch } = useQuery<GovernorEventRow[]>({
    queryKey: ['governor-events', params.toString()],
    queryFn: async () => {
      try {
        const r = await apiGet<GovernorEventRow[]>(
          `/registry/governor_events?${params.toString()}`,
        )
        setLive([])
        return r
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return []
        throw e
      }
    },
    refetchInterval: 30_000,
  })

  // ---- live tail on governor.events.> ----
  const decisionRef = useRef(decision)
  decisionRef.current = decision
  const packRef = useRef(packFilter)
  packRef.current = packFilter
  useEffect(() => {
    if (!tailOn) return
    const sub = subscribeRegistryEvents(GOVERNOR_TAIL_FILTER, (ev) => {
      if (ev.type !== 'event') return
      const row = mapGovernorEnvelope(ev.payload)
      if (!row) return
      if (decisionRef.current !== 'all' && row.decision !== decisionRef.current) return
      const pf = packRef.current.trim()
      if (pf && row.pack_id !== pf) return
      setLive((prev) => {
        const k = rowKey(row)
        if (prev.some((r) => rowKey(r) === k)) return prev
        return [row, ...prev].slice(0, 200)
      })
    })
    return () => sub.close()
  }, [tailOn])

  const rows = useMemo(() => {
    const seen = new Set<string>()
    const merged: GovernorEventRow[] = []
    for (const r of [...live, ...(data ?? [])]) {
      const k = rowKey(r)
      if (seen.has(k)) continue
      seen.add(k)
      merged.push(r)
    }
    return merged
  }, [live, data])

  const summary = useMemo(() => summariseGovernor(rows), [rows])

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${summary.blocked} blocked · ${summary.allowed} allowed${
        live.length ? ` · ${live.length} live` : ''
      }`}
      actions={
        <button
          onClick={() => setTailOn((v) => !v)}
          className={`text-[10px] px-2 py-0.5 rounded border ${
            tailOn ? 'border-accent-ok text-accent-ok' : 'border-slate-700 text-slate-500'
          }`}
          title="Toggle governor live-tail"
          data-testid="governor-tail-toggle"
        >
          {tailOn ? '● live' : '○ paused'}
        </button>
      }
      onRefresh={() => {
        setLive([])
        qc.invalidateQueries({ queryKey: ['governor-events'] })
        refetch()
      }}
    >
      <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={decision}
          onChange={(e) => setDecision(e.target.value as DecisionFilter)}
          data-testid="governor-decision-filter"
        >
          <option value="all">all decisions</option>
          <option value="block">block</option>
          <option value="allow">allow</option>
        </select>
        <input
          className="flex-1 min-w-[120px] bg-surface-200 border border-slate-700 rounded p-1 px-2"
          placeholder="pack_id filter…"
          value={packFilter}
          onChange={(e) => setPackFilter(e.target.value)}
          data-testid="governor-pack-filter"
        />
        {isLoading && <span className="text-slate-500">loading…</span>}
      </div>

      {/* cause breakdown for blocks */}
      {summary.blocked > 0 && (
        <div className="flex flex-wrap gap-1 mb-2 text-[10px]" data-testid="governor-causes">
          {Object.entries(summary.by_cause).map(([cause, n]) => (
            <span
              key={cause}
              className="rounded px-1.5 py-0.5 bg-rose-950 text-rose-300 border border-rose-900/50"
            >
              {cause}: {n}
            </span>
          ))}
        </div>
      )}

      {error instanceof Error && (
        <div className="text-rose-400 text-sm">error: {error.message}</div>
      )}

      <div className="flex-1 overflow-auto text-xs space-y-1" data-testid="governor-list">
        {!isLoading && rows.length === 0 && (
          <div className="text-slate-500 text-center py-4">
            no governor decisions — no pack has been gated yet
          </div>
        )}
        {rows.map((r) => {
          const blocked = r.decision === 'block'
          const remediation = remediationFor(r)
          return (
            <div
              key={rowKey(r)}
              className={`bg-surface-100 border rounded p-2 ${
                blocked ? 'border-rose-900/50' : 'border-slate-800'
              }`}
              data-testid={`governor-row-${blocked ? 'block' : 'allow'}`}
            >
              <div className="flex items-baseline gap-2">
                {r._live && (
                  <span className="shrink-0 rounded px-1 bg-emerald-900 text-emerald-200 text-[10px]">
                    live
                  </span>
                )}
                <span
                  className={`shrink-0 rounded px-1 text-[10px] font-mono ${
                    blocked ? 'bg-rose-900 text-rose-200' : 'bg-emerald-900 text-emerald-200'
                  }`}
                >
                  {r.decision}
                </span>
                <span className="text-slate-200 truncate">{r.pack_id}</span>
                {r.tool_name && (
                  <span className="text-slate-500 font-mono truncate">{r.tool_name}</span>
                )}
                <span className="ml-auto text-slate-600 shrink-0">
                  {new Date(r.occurred_at).toLocaleString()}
                </span>
              </div>
              {/* triggering analyst — the actor that asked for the gated call */}
              <div className="mt-1 flex items-baseline gap-1.5 text-[10px]">
                <span className="text-slate-500 uppercase tracking-wide">analyst</span>
                <span
                  className="font-mono text-slate-300 truncate"
                  data-testid="governor-analyst"
                  title={r.requested_by}
                >
                  {r.requested_by || 'system'}
                </span>
              </div>
              <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-slate-500">
                <span className={blocked ? 'text-rose-300' : ''}>cause: {r.cause}</span>
                <span>tenant: {r.tenant_id}</span>
                <span>account: {r.budget_account}</span>
                {r.cap_dimension && (
                  <span className="font-mono">
                    {r.cap_dimension}: {r.observed_value ?? '?'}/{r.cap_limit ?? '?'}
                  </span>
                )}
              </div>
              {r.detail && <div className="mt-1 text-slate-400">{r.detail}</div>}
              {/* remediation hint — what the operator can change to unblock */}
              {remediation && (
                <div
                  className="mt-1.5 flex items-start gap-1.5 text-[10px] text-amber-300/90 bg-amber-950/30 border border-amber-900/40 rounded p-1.5"
                  data-testid="governor-remediation"
                >
                  <span className="shrink-0">⚑</span>
                  <span>{remediation}</span>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </PanelChrome>
  )
}
