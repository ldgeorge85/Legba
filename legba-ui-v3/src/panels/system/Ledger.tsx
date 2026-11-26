/**
 * Ledger — the forecast ledger, the trust object.
 *
 * Every exogenous forecast question sorted by what resolves next, with each
 * arm's probability (operator · tower · consult · frontier · decision model), the
 * sealed/open state, the due date and the resolution. An arm with no number
 * reads `absent` with its reason on hover — never a blank; `—` is "not
 * stated". The Brier column is a placeholder until resolutions are scored:
 * the page is never more confident than the ledger.
 *
 * Reads `GET /api/v1/v3/ledger/forecasts`; mounted as the Forecasts view of
 * `panels/ledger/ForecastLedger` (the Ledger surface). The mapping is `@/lib/ledgerModel`
 * (unit-tested).
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, ApiError } from '@/lib/api'
import {
  ARM_COLUMNS,
  BRIER_PLACEHOLDER,
  LEDGER_ROUTE,
  ledgerSummary,
  ledgerTableRows,
  type LedgerForecastsResponse,
  type LedgerTableRow,
} from '@/lib/ledgerModel'
import type { PanelProps } from '@/types'

const STATES = ['all', 'open', 'resolved', 'void'] as const

const STATE_PILL: Record<LedgerTableRow['state'], string> = {
  open: 'bg-surf-3 text-ink-1',
  sealed: 'bg-emerald-900 text-emerald-200',
  resolved: 'bg-sky-900 text-sky-200',
  void: 'bg-surf-2 text-ink-3',
  overdue: 'bg-amber-900 text-amber-200',
}

export default function LedgerPanel({ registration }: PanelProps) {
  const [state, setState] = useState<(typeof STATES)[number]>('all')
  const { data, isLoading, error, refetch } = useQuery<LedgerForecastsResponse | null>({
    queryKey: ['ledger-forecasts'],
    queryFn: async () => {
      try {
        return await apiGet<LedgerForecastsResponse>(LEDGER_ROUTE)
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null
        throw e
      }
    },
    refetchInterval: 60_000,
  })

  const rows = useMemo(() => ledgerTableRows(data, new Date()), [data])
  const shown = useMemo(
    () =>
      rows.filter((r) => {
        if (state === 'all') return true
        if (state === 'open') return r.state === 'open' || r.state === 'sealed' || r.state === 'overdue'
        return r.state === state
      }),
    [rows, state],
  )

  const unavailable = data === null || (data != null && data.available === false)

  return (
    <PanelChrome registration={registration} subtitle={ledgerSummary(rows)} onRefresh={() => refetch()}>
      <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
        <select
          className="bg-surface-200 border border-line-strong rounded p-1 px-2"
          value={state}
          onChange={(e) => setState(e.target.value as (typeof STATES)[number])}
          data-testid="ledger-state-filter"
        >
          {STATES.map((s) => (
            <option key={s} value={s}>
              state: {s}
            </option>
          ))}
        </select>
        {isLoading && <span className="text-ink-3">loading…</span>}
      </div>

      {error instanceof Error && <div className="text-rose-400 text-sm">error: {error.message}</div>}
      {unavailable && (
        <div className="text-amber-400 text-xs mb-2" data-testid="ledger-unavailable">
          the forecast ledger could not be read — this is not the same as an empty ledger
        </div>
      )}

      <div className="flex-1 overflow-auto text-xs" data-testid="ledger-table">
        {!isLoading && !unavailable && shown.length === 0 && (
          <div className="text-ink-3 text-center py-4" data-testid="ledger-empty">
            no forecasts on the ledger{state !== 'all' ? ` in state ${state}` : ''}
          </div>
        )}
        {shown.length > 0 && (
          // The question column keeps a readable measure; a region narrower
          // than the table scrolls sideways instead of wrapping one word a line.
          <table className="w-full min-w-[48rem]">
            <thead className="text-ink-3 text-label uppercase tracking-wide border-b border-line sticky top-0 bg-surface-100">
              <tr>
                <th className="py-1 px-1 text-left">question</th>
                <th className="py-1 px-1 text-left">resolver</th>
                <th className="py-1 px-1 text-left">due</th>
                <th className="py-1 px-1 text-left">state</th>
                {ARM_COLUMNS.map((c) => (
                  <th key={c.kind} className="py-1 px-1 text-right">
                    {c.label}
                  </th>
                ))}
                <th className="py-1 px-1 text-left">resolution</th>
                <th className="py-1 px-1 text-left">{BRIER_PLACEHOLDER}</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <tr
                  key={r.id}
                  className="border-b border-line-subtle hover:bg-surface-100 align-top"
                  data-testid={`ledger-row-${r.slug}`}
                >
                  <td className="py-1 px-1 text-ink-1 min-w-[16rem] max-w-[360px]" title={r.slug}>
                    {/* The forecast's id stays on hover and in the row's test id; the reader sees the question. */}
                    <div>{r.question}</div>
                    {r.condition && <div className="text-ink-3 text-label">condition: {r.condition}</div>}
                  </td>
                  <td className="py-1 px-1 text-ink-2">
                    {r.resolverKind.replace(/_/g, ' ')}
                    {r.machineResolvable && <span className="text-ink-3"> · auto</span>}
                  </td>
                  <td className="py-1 px-1 text-ink-1 whitespace-nowrap">
                    {r.dueAt}
                    <div className="text-ink-3 text-label">{r.dueIn}</div>
                  </td>
                  <td className="py-1 px-1">
                    <span className={`rounded px-1 text-label ${STATE_PILL[r.state]}`} data-testid={`ledger-state-${r.slug}`}>
                      {r.stateLabel}
                    </span>
                  </td>
                  {r.arms.map((a) => (
                    <td
                      key={a.kind}
                      className={`py-1 px-1 text-right font-mono ${
                        a.state === 'number' ? 'text-ink-1' : a.state === 'absent' ? 'text-amber-400' : 'text-ink-3'
                      }`}
                      title={a.title}
                      data-testid={`ledger-arm-${r.slug}-${a.kind}`}
                    >
                      {a.text}
                    </td>
                  ))}
                  <td className="py-1 px-1 text-ink-1">{r.resolution}</td>
                  <td className="py-1 px-1 text-ink-3">{r.brier}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </PanelChrome>
  )
}
