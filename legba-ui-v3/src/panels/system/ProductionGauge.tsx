/**
 * Production Gauge (`system.production_gauge`) — the whole-engine
 * "did each loop produce what its own descriptor and history promised" read.
 *
 * Reads `GET /api/v1/v3/system/production-gauge`, which serves the SAME
 * `production_gauge.read_gauge()` the `production_deficit` alert-trigger class
 * pages on. That is why this panel exists rather than another liveness table:
 * `/system/analyst-cadence` and `/system/source-firing` answer *is it running*;
 * this one answers *did it produce*, and its `pages` column is the operator's
 * phone, rendered.
 *
 * WHAT IT RENDERS, and in this order:
 *   1. THE READ STATE. `measured: false` is a FAILED read the server degraded
 *      to an empty payload at HTTP 200, and it gets a loud banner of its own —
 *      an empty table under a green header is precisely the lie this panel
 *      refuses. A measured engine with zero loops reads differently and says so.
 *   2. THE WHOLE-ENGINE TOTALS, with `gauged` and `ungauged` as SEPARATE tiles.
 *      There is deliberately no health percentage anywhere on this surface: an
 *      engine where 200 of 268 loops cannot be gauged is not an engine where
 *      261 of 268 are producing, and one number would hide which one you have.
 *      The totals come from the server's pre-filter `totals` object and stay
 *      put when you filter — the caption says so out loud.
 *   3. WHAT WOULD PAGE, against `alert_min_severity` taken from the payload.
 *      No threshold is hardcoded here, so this panel cannot drift from the
 *      alert plane.
 *   4. THE LOOPS, grouped. The bricks (integrity / metering / staleness) arrive
 *      in the same flat array as the four ordinary production loops and are
 *      distinguished only by `loop_class`, so the grouping is what makes them
 *      readable as the separate instruments they are.
 *   5. PER ROW: a measured ratio against its own bar, OR — never both, never
 *      neither — the `quiet_reason` that says why there is no bar. An `ungauged`
 *      row has no ratio to render and this panel never renders it as 0.0.
 *
 * All ordering, grouping, ratio reading and summary wording live in
 * `@/lib/productionGauge` (unit-tested without a DOM); this file renders.
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ChevronDown, ChevronRight, HelpCircle, Phone } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import { cn } from '@/lib/cn'
import { relativeTime } from '@/lib/findingsViews'
import { fetchProductionGauge } from '@/lib/api'
import type { ProductionGaugeResponse, ProductionGaugeRow } from '@/lib/api'
import {
  EMPTY_FILTER,
  LOOP_CLASSES,
  classCounts,
  describeFilter,
  evidenceFields,
  gaugeErrorText,
  gaugeNotice,
  gaugeQueryOptions,
  gaugeSummaryLine,
  groupCounts,
  groupGaugeRows,
  loopClassLabel,
  pagingExplainer,
  pagingNote,
  readRatio,
  rowKey,
  severityBuckets,
  shownLine,
  totalsCaption,
  type GaugeFilter,
  type GaugeScope,
} from '@/lib/productionGauge'
import type { PanelProps } from '@/types'

/** The whole fleet is ~270 loops; the server caps at 2000. */
const ROW_LIMIT = 500

const SCOPES: Array<{ id: GaugeScope; label: string; title: string }> = [
  { id: 'all', label: 'all loops', title: 'Every gauged loop, worst-first.' },
  {
    id: 'deficits',
    label: 'deficits',
    title: 'Only loops that failed to clear their own bar (server-side state=deficit).',
  },
  {
    id: 'paging',
    label: 'paging',
    title:
      "Only deficits at or above the alert floor — exactly the set the production_deficit trigger would page on.",
  },
]

/** `null` = the server's own default baseline depth. */
const WINDOWS: Array<{ id: number | null; label: string }> = [
  { id: null, label: 'server default' },
  { id: 7, label: '7d baseline' },
  { id: 14, label: '14d baseline' },
  { id: 30, label: '30d baseline' },
  { id: 90, label: '90d baseline' },
]

const STATE_TONE: Record<string, string> = {
  ok: 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300',
  deficit: 'border-rose-500/40 bg-rose-500/15 text-rose-300',
  ungauged: 'border-sky-500/40 bg-sky-500/15 text-sky-300',
}

const SEV_TONE: Record<string, string> = {
  critical: 'border-rose-500/60 bg-rose-500/25 text-rose-200',
  high: 'border-rose-500/40 bg-rose-500/15 text-rose-300',
  medium: 'border-amber-500/40 bg-amber-500/15 text-amber-300',
  low: 'border-sky-500/40 bg-sky-500/15 text-sky-300',
  info: 'border-line bg-surf-2 text-ink-3',
}

export default function ProductionGaugePanel({ registration }: PanelProps) {
  const [filter, setFilter] = useState<GaugeFilter>(EMPTY_FILTER)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  const { data, isLoading, error, refetch } = useQuery<ProductionGaugeResponse>({
    queryKey: ['production-gauge', filter.scope, filter.loopClass, filter.windowDays],
    queryFn: () => fetchProductionGauge(gaugeQueryOptions(filter, ROW_LIMIT)),
    refetchInterval: 30_000,
  })

  const groups = useMemo(() => groupGaugeRows(data?.loops ?? []), [data])
  const filterLabel = describeFilter(filter)
  const notice = data ? gaugeNotice(data) : null

  const subtitle = data
    ? gaugeSummaryLine(data)
    : isLoading
      ? 'reading every producing loop…'
      : 'the gauge has not been read'

  return (
    <PanelChrome
      registration={registration}
      subtitle={subtitle}
      onRefresh={() => refetch()}
      actions={
        <div className="flex items-center gap-1" data-testid="production-gauge-filters">
          {SCOPES.map((s) => (
            <button
              key={s.id}
              type="button"
              title={s.title}
              onClick={() => setFilter((f) => ({ ...f, scope: s.id }))}
              data-testid={`production-gauge-filter-${s.id}`}
              className={cn(
                'rounded border px-2 py-0.5 text-label',
                filter.scope === s.id
                  ? 'border-line-strong bg-surf-3 text-ink-1'
                  : 'border-line text-ink-3 hover:text-ink-1',
              )}
            >
              {s.label}
            </button>
          ))}
          <select
            value={filter.loopClass ?? ''}
            onChange={(e) =>
              setFilter((f) => ({ ...f, loopClass: e.target.value === '' ? null : e.target.value }))
            }
            data-testid="production-gauge-class-filter"
            title="Narrow to one loop class. The totals stay whole-engine."
            className="rounded border border-line bg-surf-base px-1 py-0.5 text-label text-ink-2"
          >
            <option value="">every class</option>
            {LOOP_CLASSES.map((c) => (
              <option key={c} value={c}>
                {loopClassLabel(c)}
              </option>
            ))}
          </select>
          <select
            value={filter.windowDays == null ? '' : String(filter.windowDays)}
            onChange={(e) =>
              setFilter((f) => ({
                ...f,
                windowDays: e.target.value === '' ? null : Number(e.target.value),
              }))
            }
            data-testid="production-gauge-window-filter"
            title="Trailing history every baseline is computed over. Widening it makes long-dead producers visible again; narrowing it makes the baselines twitchier."
            className="rounded border border-line bg-surf-base px-1 py-0.5 text-label text-ink-2"
          >
            {WINDOWS.map((w) => (
              <option key={String(w.id)} value={w.id == null ? '' : String(w.id)}>
                {w.label}
              </option>
            ))}
          </select>
        </div>
      }
    >
      {isLoading && (
        <div className="text-body text-ink-3" data-testid="production-gauge-loading">
          reading every producing loop…
        </div>
      )}

      {error != null && (
        <div
          className="rounded border border-rose-500/40 bg-rose-500/10 p-2 text-body text-rose-300"
          data-testid="production-gauge-error"
        >
          Could not read the production gauge — {gaugeErrorText(error)}. Nothing below has been
          measured; this is not an all-clear.
        </div>
      )}

      {data && (
        <div className="space-y-3">
          {notice && (
            <div
              className={cn(
                'flex items-start gap-2 rounded border p-2',
                notice.state === 'read_failed'
                  ? 'border-rose-500/60 bg-rose-500/15 text-rose-200'
                  : 'border-line-strong bg-surf-1 text-ink-2',
              )}
              data-testid={
                notice.state === 'read_failed'
                  ? 'production-gauge-degraded'
                  : 'production-gauge-quiet'
              }
            >
              {notice.state === 'read_failed' ? (
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
              ) : (
                <HelpCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
              )}
              <div className="min-w-0">
                <div className="text-body-lg font-semibold">{notice.headline}</div>
                <div className="mt-0.5 text-body">{notice.detail}</div>
              </div>
            </div>
          )}

          {/* No totals strip under a FAILED read — an all-zero "0 deficit"
              tile is exactly the reassurance a failed read must not give. */}
          {data.measured && <Totals data={data} filterLabel={filterLabel} />}

          {data.measured && data.totals.loops > 0 && (
            <>
              <Paging data={data} />

              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <span className="text-label text-ink-3" data-testid="production-gauge-shown">
                  {shownLine(data.loops.length, data.totals)}
                </span>
                {filterLabel && (
                  <span className="text-label text-ink-3">· filtered to {filterLabel}</span>
                )}
                {data.generated_at && (
                  <span className="text-label text-ink-3">
                    · gauged {relativeTime(data.generated_at)}
                  </span>
                )}
              </div>

              {data.loops.length === 0 && (
                <div
                  className="rounded border border-line bg-surf-1 p-2 text-body text-ink-2"
                  data-testid="production-gauge-empty-filter"
                >
                  No loop matches {filterLabel ?? 'this view'}. The whole-engine totals above are
                  unchanged — they were computed before this filter.
                </div>
              )}

              <div className="space-y-3" data-testid="production-gauge-groups">
                {groups.map((g) => {
                  const counts = groupCounts(data.totals, g.id)
                  return (
                    <section key={g.id} data-testid={`production-gauge-group-${g.id}`}>
                      <div className="flex flex-wrap items-baseline gap-x-2 border-b border-line pb-1">
                        <h3 className="text-body-lg font-semibold text-ink-1">{g.label}</h3>
                        <span className="text-label text-ink-3">{g.blurb}</span>
                        <span
                          className="ml-auto text-label text-ink-3"
                          data-testid={`production-gauge-group-counts-${g.id}`}
                          title="Whole-engine counts for this family, from the server's pre-filter by_class totals."
                        >
                          engine-wide: {counts.gauged} gauged ({counts.ok} ok · {counts.deficit}{' '}
                          deficit) · {counts.ungauged} ungauged
                        </span>
                      </div>
                      <div className="mt-1 space-y-1">
                        {g.rows.map((r) => (
                          <GaugeRow
                            key={rowKey(r)}
                            row={r}
                            alertMinSeverity={data.alert_min_severity}
                            classLoops={classCounts(data.totals, r.loop_class).loops}
                            expanded={Boolean(expanded[rowKey(r)])}
                            onToggle={() =>
                              setExpanded((prev) => ({
                                ...prev,
                                [rowKey(r)]: !prev[rowKey(r)],
                              }))
                            }
                          />
                        ))}
                      </div>
                    </section>
                  )
                })}
              </div>
            </>
          )}
        </div>
      )}
    </PanelChrome>
  )
}

/**
 * The roll-up. `gauged` and `ungauged` are two tiles, never one percentage —
 * see the honesty contract in `@/lib/productionGauge`.
 */
function Totals({
  data,
  filterLabel,
}: {
  data: ProductionGaugeResponse
  filterLabel: string | null
}) {
  const t = data.totals
  const buckets = severityBuckets(data)
  return (
    <div
      className="rounded border border-line bg-surf-1 p-2"
      data-testid="production-gauge-totals"
    >
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Tile testid="production-gauge-total-loops" label="loops" value={t.loops} />
        <Tile
          testid="production-gauge-total-gauged"
          label="gauged"
          value={t.gauged}
          hint="loops with an honest expectation to measure against"
        />
        <Tile testid="production-gauge-total-ok" label="ok" value={t.ok} tone="text-emerald-300" />
        <Tile
          testid="production-gauge-total-deficit"
          label="deficit"
          value={t.deficit}
          tone={t.deficit > 0 ? 'text-rose-300' : undefined}
        />
        <Tile
          testid="production-gauge-total-ungauged"
          label="ungauged"
          value={t.ungauged}
          tone="text-sky-300"
          hint="no honest expectation exists — we cannot say, which is NOT the same as fine"
        />
        <Tile
          testid="production-gauge-total-paging"
          label="paging"
          value={t.paging}
          tone={t.paging > 0 ? 'text-rose-300' : undefined}
          hint={`deficits at or above the ${data.alert_min_severity} alert floor`}
        />
      </div>

      {buckets.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1">
          <span className="text-label text-ink-3">deficits by severity:</span>
          {buckets.map((b) => (
            <span
              key={b.severity}
              data-testid={`production-gauge-severity-${b.severity}`}
              className={cn(
                'rounded border px-1.5 py-0.5 text-label',
                SEV_TONE[b.severity] ?? 'border-line bg-surf-2 text-ink-2',
              )}
              title={
                b.pages
                  ? `at or above the ${data.alert_min_severity} alert floor — these page`
                  : `below the ${data.alert_min_severity} alert floor — these stay off the phone`
              }
            >
              {b.severity} {b.count}
              {b.pages && ' · pages'}
            </span>
          ))}
        </div>
      )}

      <div className="mt-1.5 text-label text-ink-3" data-testid="production-gauge-totals-caption">
        {totalsCaption(filterLabel)}
      </div>
    </div>
  )
}

function Tile({
  label,
  value,
  testid,
  tone,
  hint,
}: {
  label: string
  value: number
  testid: string
  tone?: string
  hint?: string
}) {
  return (
    <div className="rounded border border-line bg-surf-base px-2 py-1" title={hint}>
      <div className="text-label uppercase tracking-wider text-ink-3">{label}</div>
      <div className={cn('text-heading-lg font-semibold', tone ?? 'text-ink-1')} data-testid={testid}>
        {value}
      </div>
    </div>
  )
}

function Paging({ data }: { data: ProductionGaugeResponse }) {
  const pages = data.totals.paging > 0
  return (
    <div
      className={cn(
        'flex items-start gap-2 rounded border p-2 text-body',
        pages
          ? 'border-rose-500/40 bg-rose-500/10 text-rose-300'
          : 'border-line bg-surf-1 text-ink-2',
      )}
      data-testid="production-gauge-paging-note"
    >
      <Phone className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
      <span>{pagingExplainer(data)}</span>
    </div>
  )
}

function GaugeRow({
  row,
  alertMinSeverity,
  classLoops,
  expanded,
  onToggle,
}: {
  row: ProductionGaugeRow
  alertMinSeverity: string
  classLoops: number
  expanded: boolean
  onToggle: () => void
}) {
  const key = rowKey(row)
  const reading = readRatio(row)
  const note = pagingNote(row, alertMinSeverity)
  const evidence = evidenceFields(row.evidence)

  return (
    <div
      className={cn(
        'rounded border bg-surf-1',
        row.pages ? 'border-rose-500/50' : 'border-line',
      )}
      data-testid={`production-gauge-row-${key}`}
    >
      <button
        type="button"
        onClick={onToggle}
        data-testid={`production-gauge-toggle-${key}`}
        className="flex w-full items-start gap-2 px-2 py-1.5 text-left hover:bg-surf-2"
      >
        {expanded ? (
          <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden />
        ) : (
          <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden />
        )}

        <span
          className="shrink-0 rounded border border-line bg-surf-2 px-1.5 py-0.5 text-label text-ink-2"
          title={`${classLoops} loops of this class engine-wide`}
        >
          {loopClassLabel(row.loop_class)}
        </span>

        <span className="min-w-0 flex-1">
          <span className="block truncate text-body text-ink-1">{row.label}</span>
          <span className="block truncate font-mono text-label text-ink-3">{row.loop_id}</span>
        </span>

        {/* The ratio, OR the reason there is none. Never a null rendered as 0.0. */}
        {reading.measured ? (
          <span className="w-40 shrink-0" data-testid={`production-gauge-ratio-${key}`}>
            <span
              className={cn(
                'block text-right font-mono text-body',
                reading.overBar ? 'text-rose-300' : 'text-emerald-300',
              )}
            >
              {reading.text}
            </span>
            <span
              className="relative mt-0.5 block h-1.5 w-full overflow-hidden rounded bg-surf-3"
              title={`observed absence over the bar it had to clear; the meter runs to ${reading.meter.cap}× (the critical rung)${
                reading.meter.clamped ? ' and this row runs off the end' : ''
              }`}
            >
              <span
                className={cn(
                  'absolute inset-y-0 left-0 rounded',
                  reading.overBar ? 'bg-rose-500/70' : 'bg-emerald-500/70',
                )}
                style={{ width: `${reading.meter.pct}%` }}
              />
              {/* The 1.0× mark — the loop's own bar. */}
              <span
                className="absolute inset-y-0 w-px bg-amber-300"
                style={{ left: `${reading.meter.thresholdPct}%` }}
              />
            </span>
          </span>
        ) : (
          <span
            className={cn(
              'w-40 shrink-0 text-right text-label',
              reading.readFailure ? 'text-amber-300' : 'text-sky-300',
            )}
            data-testid={`production-gauge-quiet-${key}`}
            title={reading.text}
          >
            <span className="block font-mono">no ratio — ungauged</span>
            <span className="block truncate font-mono text-ink-3">{reading.quietReason}</span>
          </span>
        )}

        <span
          className={cn(
            'shrink-0 rounded border px-1.5 py-0.5 text-label',
            STATE_TONE[row.state] ?? 'border-line bg-surf-2 text-ink-2',
          )}
          data-testid={`production-gauge-state-${key}`}
        >
          {row.state}
        </span>

        {row.state === 'deficit' && (
          <span
            className={cn(
              'shrink-0 rounded border px-1.5 py-0.5 text-label',
              SEV_TONE[row.severity] ?? 'border-line bg-surf-2 text-ink-2',
            )}
            data-testid={`production-gauge-severity-pill-${key}`}
          >
            {row.severity}
          </span>
        )}

        {row.pages && (
          <span
            className="flex shrink-0 items-center gap-1 rounded border border-rose-500/60 bg-rose-500/25 px-1.5 py-0.5 text-label text-rose-200"
            data-testid={`production-gauge-pages-${key}`}
            title={note ?? undefined}
          >
            <Phone className="h-3 w-3" aria-hidden />
            PAGES
          </span>
        )}

        <span className="w-16 shrink-0 text-right text-label text-ink-3">
          {row.last_production_at ? relativeTime(row.last_production_at) : 'never'}
        </span>
      </button>

      {expanded && (
        <div className="space-y-1.5 border-t border-line px-2 py-2">
          <Field label="Expected (its own promise)" value={row.expected} testid={`production-gauge-expected-${key}`} />
          <Field label="Actual" value={row.actual} testid={`production-gauge-actual-${key}`} />

          {!reading.measured && (
            <Field
              label="Why there is no expectation to measure against"
              value={reading.text}
              testid={`production-gauge-quiet-reason-${key}`}
              tone={reading.readFailure ? 'text-amber-300' : 'text-sky-300'}
            />
          )}

          {note && (
            <Field
              label="Alert plane"
              value={note}
              testid={`production-gauge-paging-${key}`}
              tone={row.pages ? 'text-rose-300' : 'text-ink-2'}
            />
          )}

          <Field
            label="Last production"
            value={
              row.last_production_at
                ? `${row.last_production_at} (${relativeTime(row.last_production_at)})`
                : 'never produced'
            }
            testid={`production-gauge-last-${key}`}
          />

          <div>
            <div className="text-label uppercase tracking-wider text-ink-3">
              Evidence the verdict was computed from
            </div>
            {evidence.length === 0 ? (
              <div className="text-body text-ink-3" data-testid={`production-gauge-evidence-${key}`}>
                the server attached no evidence to this row
              </div>
            ) : (
              <dl
                className="mt-0.5 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-body"
                data-testid={`production-gauge-evidence-${key}`}
              >
                {evidence.map((f) => (
                  <div key={f.key} className="contents">
                    <dt className="font-mono text-ink-3">{f.key}</dt>
                    <dd className="min-w-0 break-words font-mono text-ink-2">{f.value}</dd>
                  </div>
                ))}
              </dl>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function Field({
  label,
  value,
  testid,
  tone,
}: {
  label: string
  value: string
  testid: string
  tone?: string
}) {
  return (
    <div>
      <div className="text-label uppercase tracking-wider text-ink-3">{label}</div>
      <div className={cn('text-body', tone ?? 'text-ink-2')} data-testid={testid}>
        {value === '' ? '—' : value}
      </div>
    </div>
  )
}
