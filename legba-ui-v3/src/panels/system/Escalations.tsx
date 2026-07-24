/**
 * Escalation Deliveries (`system.escalations`) — the human-visible alert edge
 * (audit finding C3 / decision D1).
 *
 * Every escalate / create_incident emit the ChannelEmitter fires writes one
 * durable `alert_sink_deliveries` row (migration 0061) recording WHAT was
 * delivered WHERE and whether the publish CONFIRMED. Those rows land in Postgres
 * (and on NATS `channels.escalations`) but, before this panel, rendered NOWHERE
 * — so a human never saw that pushover failed 552× or that a watch-desk
 * escalation went nowhere. This panel closes that gap: it polls
 * `GET /api/v1/v3/system/escalations` and makes non-delivery LOUD.
 *
 * Two surfaces:
 *   - a 24h HEALTH BANNER over the server's window rollup — red + shouting when
 *     anything FAILED or went NOWHERE (the same non-delivery signal the W1-T3
 *     integrity-sweep canary alarms on), green + quiet when the window is clean.
 *   - the recent DELIVERY LIST, newest-first, filterable by status. Failures
 *     (`failed`) and silent losses (`logged_only`) are styled red and show the
 *     delivery error; a delivered row is green. Clicking a row with a linked
 *     finding opens its lineage walk (same cross-panel pattern as the feed).
 *
 * DISTINCT from `system.alert_center` (the localStorage subscription watchlist
 * that diffs the findings feed) — this is the delivery-audit read, not a
 * client-side rule engine. Read-only; honest empty on no data.
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import {
  ApiError,
  getSystemEscalations,
  type EscalationDeliveriesResponse,
  type EscalationDeliveryRow,
} from '@/lib/api'
import { relTime } from '@/lib/evalOps'
import { selectRow } from '@/state/selection'
import type { PanelProps } from '@/types'

const POLL_MS = 15_000

const EMPTY: EscalationDeliveriesResponse = {
  summary: {
    window_hours: 24,
    total: 0,
    delivered: 0,
    failed: 0,
    logged_only: 0,
    retrying: 0,
    other: 0,
    non_delivery: 0,
    by_sink_status: [],
  },
  rows: [],
}

type Light = 'green' | 'amber' | 'red' | 'grey'

const LIGHT_PILL: Record<Light, string> = {
  green: 'bg-emerald-900 text-emerald-200',
  amber: 'bg-amber-900 text-amber-200',
  red: 'bg-rose-900 text-rose-200',
  grey: 'bg-slate-800 text-slate-400',
}

/** Status → traffic light. Both terminal non-deliveries are RED (loud): a hard
 *  `failed` and a `logged_only` that went nowhere are equally invisible losses. */
function statusLight(status: string): Light {
  switch (status) {
    case 'delivered':
      return 'green'
    case 'failed':
      return 'red'
    case 'logged_only':
      return 'red'
    case 'retrying':
      return 'amber'
    default:
      return 'grey'
  }
}

/** Human label for a status (logged_only reads as its plain-English meaning). */
function statusLabel(status: string): string {
  return status === 'logged_only' ? 'went nowhere' : status
}

const STATUS_FILTERS = [
  'all',
  'failed',
  'logged_only',
  'retrying',
  'delivered',
] as const
type StatusFilter = (typeof STATUS_FILTERS)[number]

function StatusPill({ status, testid }: { status: string; testid?: string }) {
  return (
    <span
      className={`rounded px-1 text-[10px] whitespace-nowrap ${LIGHT_PILL[statusLight(status)]}`}
      data-testid={testid}
    >
      {statusLabel(status)}
    </span>
  )
}

function severityPill(severity: string | null): Light {
  switch (severity) {
    case 'critical':
      return 'red'
    case 'high':
      return 'amber'
    default:
      return 'grey'
  }
}

// ===========================================================================
// 24h health banner — LOUD on any non-delivery.
// ===========================================================================

function HealthBanner({ resp }: { resp: EscalationDeliveriesResponse }) {
  const s = resp.summary
  const alarm = s.non_delivery > 0
  if (alarm) {
    return (
      <div
        className="rounded border border-rose-700 bg-rose-950/60 px-3 py-2 mb-2"
        data-testid="escalations-banner-alarm"
      >
        <div className="flex items-center gap-2 flex-wrap">
          <span className="w-2 h-2 rounded-full bg-rose-400 shrink-0 animate-pulse" />
          <span className="font-semibold text-rose-200 text-sm">
            {s.non_delivery} escalation{s.non_delivery === 1 ? '' : 's'} FAILED or
            went NOWHERE
          </span>
          <span className="text-[11px] text-rose-300/80">
            in the last {s.window_hours}h · {s.delivered} delivered
          </span>
        </div>
        <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
          {s.by_sink_status.map((b) => (
            <span
              key={`${b.sink_kind}:${b.status}`}
              className={`rounded px-1.5 py-0.5 text-[10px] ${LIGHT_PILL['red']}`}
              title={b.sample_error ?? undefined}
              data-testid={`escalations-nd-${b.sink_kind}-${b.status}`}
            >
              {b.n} {b.sink_kind} · {statusLabel(b.status)}
            </span>
          ))}
        </div>
        {s.by_sink_status.some((b) => b.sample_error) && (
          <div className="mt-1 text-[11px] font-mono text-rose-300/80 truncate">
            {s.by_sink_status.find((b) => b.sample_error)?.sample_error}
          </div>
        )}
      </div>
    )
  }
  return (
    <div
      className="rounded border border-emerald-900/50 bg-emerald-950/30 px-3 py-2 mb-2 flex items-center gap-2"
      data-testid="escalations-banner-clear"
    >
      <span className="w-2 h-2 rounded-full bg-emerald-400 shrink-0" />
      <span className="text-emerald-200 text-sm">
        No delivery failures in the last {s.window_hours}h
      </span>
      <span className="text-[11px] text-slate-500">
        {s.delivered} delivered{s.retrying > 0 ? ` · ${s.retrying} retrying` : ''}
      </span>
    </div>
  )
}

// ===========================================================================
// Delivery row.
// ===========================================================================

function DeliveryRow({ row }: { row: EscalationDeliveryRow }) {
  const light = statusLight(row.status)
  const isFailure = light === 'red'
  const title = String(
    (row.payload_summary?.['title'] as string | undefined) ||
      row.channel_name ||
      row.sink_target ||
      '(escalation)',
  )
  const clickable = !!row.alert_row_id
  return (
    <tr
      className={`border-b border-slate-800/40 ${
        isFailure ? 'bg-rose-950/30' : ''
      } ${clickable ? 'hover:bg-surface-200 cursor-pointer' : ''}`}
      data-testid={`escalations-row-${row.id}`}
      onClick={
        clickable
          ? () =>
              selectRow('finding', row.alert_row_id as string, title, {
                origin: 'escalations',
              })
          : undefined
      }
      title={clickable ? 'open lineage walk' : undefined}
    >
      <td className="py-1 px-1 align-top">
        <StatusPill status={row.status} testid={`escalations-row-status-${row.id}`} />
      </td>
      <td className="py-1 px-1 align-top">
        {row.severity ? (
          <span className={`rounded px-1 text-[10px] ${LIGHT_PILL[severityPill(row.severity)]}`}>
            {row.severity}
          </span>
        ) : (
          <span className="text-slate-600 text-[10px]">—</span>
        )}
      </td>
      <td className="py-1 px-1 align-top font-mono text-slate-300 truncate max-w-[120px]">
        {row.target_id ?? '—'}
      </td>
      <td className="py-1 px-1 align-top text-slate-400 truncate max-w-[160px]">
        <span className="text-slate-300">{row.channel_name ?? row.sink_kind}</span>
        {row.sink_target && (
          <span className="text-slate-600"> · {row.sink_target}</span>
        )}
        {isFailure && row.error_message && (
          <div
            className="text-rose-300 font-mono text-[10px] mt-0.5 break-all"
            data-testid={`escalations-row-error-${row.id}`}
          >
            {row.error_message}
          </div>
        )}
      </td>
      <td className="py-1 px-1 align-top text-right font-mono text-slate-500">
        {row.effective_confidence != null
          ? row.effective_confidence.toFixed(2)
          : '—'}
      </td>
      <td className="py-1 px-1 align-top text-slate-500 whitespace-nowrap">
        {relTime(row.attempted_at)}
      </td>
    </tr>
  )
}

// ===========================================================================
// Panel root.
// ===========================================================================

export default function EscalationsPanel({ registration }: PanelProps) {
  const [status, setStatus] = useState<StatusFilter>('all')

  const { data, isLoading, error, refetch } = useQuery<EscalationDeliveriesResponse>({
    queryKey: ['system-escalations', status],
    queryFn: async () => {
      try {
        return await getSystemEscalations(
          status === 'all' ? {} : { status },
        )
      } catch (e) {
        // Honest empty on a 404 (route not mounted on an older backend) — the
        // panel renders "no deliveries", never a fabricated row.
        if (e instanceof ApiError && e.status === 404) return EMPTY
        throw e
      }
    },
    refetchInterval: POLL_MS,
  })

  const resp = data ?? EMPTY
  const rows = resp.rows
  const s = resp.summary

  const subtitle = useMemo(
    () =>
      `${s.delivered} delivered · ${s.failed} failed · ${s.logged_only} nowhere` +
      (s.retrying > 0 ? ` · ${s.retrying} retrying` : '') +
      ` (last ${s.window_hours}h)`,
    [s],
  )

  return (
    <PanelChrome
      registration={registration}
      subtitle={subtitle}
      onRefresh={() => refetch()}
    >
      <div className="text-xs flex flex-col h-full" data-testid="escalations">
        <HealthBanner resp={resp} />

        {/* status filter */}
        <div className="flex items-center gap-2 mb-2">
          <span className="text-[11px] text-slate-500">status</span>
          <select
            className="bg-surface-200 border border-slate-700 rounded p-1 px-2 text-slate-200"
            value={status}
            onChange={(e) => setStatus(e.target.value as StatusFilter)}
            data-testid="escalations-status-filter"
          >
            {STATUS_FILTERS.map((f) => (
              <option key={f} value={f}>
                {f === 'all' ? 'all' : statusLabel(f)}
              </option>
            ))}
          </select>
        </div>

        {error instanceof Error && (
          <div className="text-rose-400 py-1" data-testid="escalations-error">
            error: {error.message}
          </div>
        )}

        <div className="flex-1 overflow-auto">
          {!isLoading && rows.length === 0 && (
            <div
              className="text-slate-500 text-center py-6"
              data-testid="escalations-empty"
            >
              {status === 'all'
                ? 'no escalation deliveries recorded'
                : `no ${statusLabel(status)} deliveries`}
            </div>
          )}
          {rows.length > 0 && (
            <table className="w-full">
              <thead className="text-slate-500 text-[10px] uppercase tracking-wide border-b border-slate-800">
                <tr>
                  <th className="py-1 px-1 text-left">status</th>
                  <th className="py-1 px-1 text-left">sev</th>
                  <th className="py-1 px-1 text-left">target</th>
                  <th className="py-1 px-1 text-left">channel · error</th>
                  <th className="py-1 px-1 text-right">conf</th>
                  <th className="py-1 px-1 text-left">when</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <DeliveryRow key={r.id} row={r} />
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </PanelChrome>
  )
}
