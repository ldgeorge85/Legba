/**
 * AlertLog — the Watch surface's stage (U-9): the ONE alert log
 * (`GET /v3/system/alerts/log`, docs/ALERTING.md), newest first.
 *
 * Each row says what fired and when, its kind in plain words, its severity
 * (a word, and a 2 px rule in the severity ramp — never colour alone), whether
 * it PAGED or was only LOGGED and by which entry of the page rule, and its
 * subject — a source, a unit or a subject — as a link that makes it the
 * frame's point, so the Record sheet, the map and Consult follow.
 *
 * Its window is the one clock's span (6 h … 30 d): the log's closed episodes
 * and its pages are read over it; open episodes are always shown. Filters
 * (kind group, severity floor, paged or logged, open or closed) are applied
 * to what was read — a filter is never another request. The kind and the
 * severity floor are always visible; the rest sit behind a "Filters" control
 * that counts what is set. Page-rule ids and the producer's statistics show
 * for admins only.
 */
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import { useBelowAdmin } from '@/auth/roles'
import { cn } from '@/lib/cn'
import { pinForSelection, usePinScope } from '@/lib/consultContext'
import { useSelection } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'
import {
  ALERT_GROUP_LABEL,
  alertRows,
  agoText,
  filterAlerts,
  groupCounts,
  whenText,
  windowHours,
  type AlertFilter,
  type AlertGroup,
  type AlertRow,
} from './model'
import { focusAlert, useWatch } from './state'
import { PointFilterLine, pointLabel, usePointFilter } from '@/components/PointFilter'
import type { AlertLogResponse } from './types'
import { Chip, ChipRow, Figure, Notice, SubjectLink, ViewHead } from './parts'

export const ALERT_LOG_ROUTE = '/v3/system/alerts/log'
/**
 * RA-1: the reader's form of the log (`reader_reads_api.py`): the world's
 * kinds only, without who acknowledged or snoozed, who raised it, the page
 * sinks' outcomes or the rule's file path. A signed-in caller below admin
 * reads this; an admin (and a caller whose role is not known) reads the
 * whole log. Same shape for every field the Watch draws.
 */
export const READER_ALERT_LOG_ROUTE = '/v3/watch/alerts'

/** The alert-log route for a role: the whole log only for an admin. */
export function alertLogRoute(belowAdmin: boolean): string {
  return belowAdmin ? READER_ALERT_LOG_ROUTE : ALERT_LOG_ROUTE
}
const PAGE = 40

const RULE: Record<string, string> = {
  critical: 'border-severity-critical',
  high: 'border-severity-high',
  medium: 'border-severity-medium',
  low: 'border-severity-low',
  info: 'border-severity-info',
}

function spanWords(hours: number): string {
  if (hours % 24 === 0) return hours === 24 ? '24 hours' : `${hours / 24} days`
  return `${hours} hours`
}

export function useAlertLog() {
  const spanMs = useWorldState((s) => s.spanMs)
  const hours = windowHours(spanMs)
  const route = alertLogRoute(useBelowAdmin())
  const q = useQuery<AlertLogResponse>({
    queryKey: ['watch', 'alert-log', route, hours],
    queryFn: () => apiGet<AlertLogResponse>(`${route}?hours=${hours}`),
    staleTime: 30_000,
    refetchInterval: 60_000,
  })
  return { q, hours }
}

/** The producer's line can carry its mean and spread (μ/σ); a reader never sees those. */
const hasStatsLine = (t: string) => /[μσ]/.test(t)

function Row({ row, focused, now, admin }: { row: AlertRow; focused: boolean; now: number; admin: boolean }) {
  const d = row.decision
  return (
    <li
      className={cn('border-b border-l-2 border-b-line-subtle py-2 pl-3 pr-1', RULE[row.severity], focused && 'bg-surf-2')}
      data-testid="watch-alert-row"
      data-group={row.group}
      data-paged={d.paged ? '1' : '0'}
      data-open={row.open ? '1' : '0'}
      onClick={() => focusAlert(row.id, row.subject)}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 text-label text-ink-3">
        <span className="font-semibold uppercase tracking-[0.06em] text-ink-2">{row.severityWord}</span>
        <span className="rounded-control border border-line-subtle px-1.5" data-testid="watch-alert-kind">
          {row.groupLabel}
        </span>
        <span title={whenText(row.at)}>{agoText(row.at, now)}</span>
        {row.occurrences > 1 ? <span>· raised {row.occurrences} times</span> : null}
      </div>
      <div className="mt-0.5 text-body-lg text-ink-1">
        <SubjectLink subject={row.subject} onPick={() => useWatch.setState({ focusedAlert: row.id })} />
        <span className="text-ink-2"> — {row.what}</span>
      </div>
      <div className="mt-0.5 text-label text-ink-2" data-testid="watch-alert-decision">
        <span className={d.paged ? 'font-semibold text-ink-1' : undefined}>{d.words}</span>
        {admin ? <span> · {d.rule}</span> : null}
        {d.paged && d.delivered != null ? <span> · {d.delivered ? 'delivered' : 'not delivered'}</span> : null}
        <span> · {row.stateWords}</span>
      </div>
      {focused && row.detail && (admin || !hasStatsLine(row.detail)) ? (
        <div className="mt-1 max-w-[80ch] text-label text-ink-3" data-testid="watch-alert-detail">
          As raised: {row.detail}
        </div>
      ) : null}
    </li>
  )
}

const GROUPS: readonly AlertGroup[] = ['fired', 'quiet', 'changed', 'desk', 'forecast']

export default function AlertLog() {
  const { q, hours } = useAlertLog()
  // Page-rule ids and the producer's statistics are for admins; a role that is not known reads as before.
  const admin = !useBelowAdmin()
  const [moreFilters, setMoreFilters] = useState(false)
  const filter = useWatch((s) => s.alertFilter)
  const focused = useWatch((s) => s.focusedAlert)
  const [limit, setLimit] = useState(PAGE)
  const selection = useSelection((s) => s.selection)
  // THE PIN: the selected alert's subject is what Consult is asked about here.
  usePinScope(pinForSelection(selection, ['source', 'analyst', 'target', 'finding', 'signal']), 'watch')

  const allRows = useMemo(() => alertRows(q.data), [q.data])
  // THE POINT: with a subject in focus the log narrows to the alerts about
  // it; "Show all" undoes that here (`components/PointFilter.tsx`).
  const pointFilter = usePointFilter()
  const pointId = pointFilter.filtered ? pointFilter.point!.id : null
  const rows = useMemo(
    () => (pointId ? allRows.filter((r) => r.subject.kind === 'target' && r.subject.id === pointId) : allRows),
    [allRows, pointId],
  )
  const shown = useMemo(() => filterAlerts(rows, filter), [rows, filter])
  const counts = useMemo(() => groupCounts(rows), [rows])
  const now = Date.now()

  useEffect(() => {
    const subjects: Record<string, AlertRow['subject']> = {}
    for (const r of shown.slice(0, limit)) subjects[r.id] = r.subject
    useWatch.setState({ shownAlerts: q.data?.measured ? shown.length : null, alertSubjects: subjects })
  }, [shown, limit, q.data?.measured])

  const set = (patch: Partial<AlertFilter>) => useWatch.setState({ alertFilter: { ...filter, ...patch } })
  const log = q.data
  const read = !!log?.measured
  const open = read ? (log.open ?? []).length : null
  const closed = read ? (log.closed ?? []).length : null
  // Filters behind the disclosure that are narrowing the list.
  const activeExtra = (filter.paged !== 'any' ? 1 : 0) + (filter.state !== 'any' ? 1 : 0)
  const paged = read ? allRows.filter((r) => r.decision.paged).length : null

  return (
    <section
      className="flex h-full min-h-0 flex-col gap-2 overflow-auto bg-surf-1 px-5 py-4"
      data-testid="watch-alert-log"
      data-read={read ? '1' : '0'}
      // What the filters select, before the PAGE cap: the list draws at most
      // `limit` rows, so a count of drawn rows cannot tell a filter that
      // narrowed from one that did not (e.g. 40 drawn of 63 paged).
      data-shown={read ? shown.length : undefined}
      data-shown-paged={read ? shown.filter((r) => r.decision.paged).length : undefined}
    >
      <ViewHead
        title="Alert log"
        testId="watch-alert-head"
        line={
          <>
            <Figure value={open} testId="watch-alert-open" /> open · <Figure value={closed} testId="watch-alert-closed" /> closed and{' '}
            <Figure value={paged} /> paged in the last {spanWords(hours)}
            {read && log.as_of ? <> · newest {whenText(log.as_of)}</> : null}
            {admin && read && log.rule_version ? <> · notification settings {log.rule_version}</> : null}
          </>
        }
      />
      <PointFilterLine state={pointFilter} testId="watch-alert-point" />
      <div className="flex flex-col gap-1.5" data-testid="watch-alert-filters">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-label text-ink-3">
          <label className="flex items-center gap-1.5">
            Kind
            <select
              className="rounded-control border border-line bg-surf-2 px-1.5 py-0.5 text-ink-1"
              value={filter.group}
              onChange={(e) => set({ group: e.target.value as AlertFilter['group'] })}
              data-testid="watch-filter-kind"
            >
              <option value="reader">Everything for readers</option>
              {GROUPS.map((g) => (
                <option key={g} value={g}>
                  {ALERT_GROUP_LABEL[g]}
                  {read ? ` (${counts[g]})` : ''}
                </option>
              ))}
              <option value="all">All, with system alerts</option>
            </select>
          </label>
          <label className="flex items-center gap-1.5">
            Severity
            <select
              className="rounded-control border border-line bg-surf-2 px-1.5 py-0.5 text-ink-1"
              value={filter.severity}
              onChange={(e) => set({ severity: e.target.value as AlertFilter['severity'] })}
              data-testid="watch-filter-severity"
            >
              {(['any', 'medium', 'high', 'critical'] as const).map((s) => (
                <option key={s} value={s}>
                  {s === 'any' ? 'Any' : s === 'critical' ? 'Critical' : `${s.charAt(0).toUpperCase()}${s.slice(1)} or above`}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="text-ink-2 underline decoration-line underline-offset-2 hover:text-ink-1"
            aria-expanded={moreFilters}
            onClick={() => setMoreFilters((v) => !v)}
            data-testid="watch-filters-toggle"
          >
            Filters{activeExtra > 0 ? ` · ${activeExtra}` : ''}
          </button>
        </div>
        {moreFilters ? (
          <div className="flex flex-wrap gap-x-5 gap-y-1.5" data-testid="watch-filters-more">
            <ChipRow label="Paged">
              {(['any', 'paged', 'logged'] as const).map((p) => (
                <Chip key={p} on={filter.paged === p} onClick={() => set({ paged: p })} testId={`watch-filter-paged-${p}`}>
                  {p === 'any' ? 'Either' : p === 'paged' ? 'Paged' : 'Logged only'}
                </Chip>
              ))}
            </ChipRow>
            <ChipRow label="State">
              {(['any', 'open', 'closed'] as const).map((s) => (
                <Chip key={s} on={filter.state === s} onClick={() => set({ state: s })} testId={`watch-filter-state-${s}`}>
                  {s === 'any' ? 'Either' : s === 'open' ? 'Open' : 'Closed'}
                </Chip>
              ))}
            </ChipRow>
          </div>
        ) : null}
      </div>
      {q.isPending ? (
        <Notice title="Reading the alert log…" testId="watch-alert-reading" />
      ) : q.isError ? (
        <Notice title="The alert log could not be read." testId="watch-alert-error">
          Nothing here is a measurement: the request failed. It is asked again within a minute.
        </Notice>
      ) : !read ? (
        <Notice title="The alert log could not be read." testId="watch-alert-unmeasured">
          {log?.not_measured_reason ?? 'The server answered without a reading.'} Nothing here is a measurement.
        </Notice>
      ) : rows.length === 0 && pointId ? (
        <Notice title={`Nothing fired about ${pointLabel(pointFilter.point!)} in the last ${spanWords(hours)}.`} testId="watch-alert-none-on-point">
          {allRows.length} alert{allRows.length === 1 ? '' : 's'} in the window are about other things; Show all lists them.
        </Notice>
      ) : rows.length === 0 ? (
        <Notice title={`Nothing fired in the last ${spanWords(hours)}, and nothing is open.`} testId="watch-alert-none">
          The log was read; it holds no alert in this window. A longer window on the time band reads further back.
        </Notice>
      ) : shown.length === 0 ? (
        <Notice title="Nothing matches these filters." testId="watch-alert-filtered-out">
          {rows.length} alert{rows.length === 1 ? '' : 's'} in the window; widen a filter to see them.
        </Notice>
      ) : (
        <>
          <ol className="shrink-0" data-testid="watch-alert-list">
            {shown.slice(0, limit).map((r) => (
              <Row key={r.id} row={r} focused={r.id === focused} now={now} admin={admin} />
            ))}
            {shown.length > limit ? (
              <li className="py-2 pl-3">
                <button type="button" className="text-label text-ink-2 underline" onClick={() => setLimit((n) => n + PAGE)} data-testid="watch-alert-more">
                  Show {Math.min(PAGE, shown.length - limit)} more of {shown.length - limit}
                </button>
              </li>
            ) : null}
          </ol>
        </>
      )}
    </section>
  )
}
