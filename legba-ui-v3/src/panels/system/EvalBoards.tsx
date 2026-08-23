/**
 * Eval Boards (`system.eval_boards`) — three engine-level boards, one tab each.
 *
 * `/v3/eval/desk_baselines`, `/v3/eval/band_trajectory` and
 * `/v3/eval/analyst_runtime` all shipped live and were consumed by NOTHING.
 * They answer three different operator errands — is this desk's current volume
 * outside its own historical band, is a verdict's band drifting, and how long
 * are analyst runs taking — so they get three tabs rather than three more
 * sections on `system.eval_scorecard` (which answers "is this ANALYST getting
 * better" and is already 800+ lines).
 *
 * Only the ACTIVE tab fetches (`enabled:` per query), so switching tabs is what
 * triggers a load and an unopened board costs nothing.
 *
 * The honesty contract each board carries is enforced in `@/lib/evalBoards`,
 * not here — this file is rendering. The three rules that matter most on
 * screen:
 *   * the desk-baseline `note` is the SERVER's disclaimer that a band is a
 *     descriptive statistic and not a forecast; it renders verbatim, always;
 *   * a truncated trajectory says so at the top of its own tab, because its
 *     last desk group may be incomplete;
 *   * the runtime board has no degradation wrapper, so a 500 renders as an
 *     error and NOT as an empty table.
 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ChevronDown, ChevronRight, ScissorsLineDashed } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import { PanelTabStrip, type PanelTabDef } from '@/components/PanelTabs'
import { InfoTip } from '@/components/InfoTip'
import { cn } from '@/lib/cn'
import { humanizeId } from '@/lib/deskNames'
import {
  fetchAnalystRuntime,
  fetchBandTrajectory,
  fetchDeskBaselines,
} from '@/lib/api'
import type {
  AnalystRuntimeRow,
  BandTrajectoryResponse,
  DeskBaselineBoard,
  DeskBaselineRow,
  DeskTrajectory,
  TrajectoryPoint,
} from '@/lib/api'
import {
  NOT_RECORDED,
  RUNTIME_WINDOW_CHOICES,
  TRAJECTORY_DAY_CHOICES,
  avgSecondsLabel,
  bandLabel,
  bandTone,
  baselineCountsLine,
  baselineNote,
  baselineRowFacts,
  baselineStateMessage,
  baselineBoardState,
  boardErrorText,
  confidenceLabel,
  deviationDirection,
  deviationLabel,
  formatMetric,
  insufficientHistoryLabel,
  maxSecondsLabel,
  nonSuccessLabel,
  orderedBaselineRows,
  orderedTrajectoryDimensions,
  pointTitle,
  relTime,
  runtimeErrorText,
  runtimeTotalsLine,
  runtimeWindowLabel,
  seriesLabel,
  trajectorySummaryLine,
  trajectoryTotals,
  truncationWarning,
  type BandTone,
  type DeviationDirection,
} from '@/lib/evalBoards'
import type { PanelProps } from '@/types'

type BoardTab = 'baselines' | 'trajectory' | 'runtime'

const TABS: readonly PanelTabDef[] = [
  { id: 'baselines', label: 'Desk baselines' },
  { id: 'trajectory', label: 'Band trajectory' },
  { id: 'runtime', label: 'Analyst runtime' },
]

const BASELINE_EXPLAIN =
  'A desk-baseline band is a DESCRIPTIVE statistic over our own substrate: the median of the ' +
  'desk’s own recent history, widened by a robust sigma. It is not a forecast, not a ' +
  'prediction and carries no skill claim — a "current" outside the band means today looks ' +
  'unlike our own recent record, and nothing more.'

const TRUNCATION_EXPLAIN =
  'The server caps how many scorecard rows it scans. When the cap is hit the LAST desk group ' +
  'returned may be missing points, so its strip is shorter than the desk’s real history.'

const RUNTIME_EXPLAIN =
  'This board has no server-side degradation wrapper: its siblings answer a read failure with ' +
  'an all-defaults 200, this one answers with a real 500. An empty table here would mean "no ' +
  'analyst ran", which is a claim — so a failed read renders as an error instead.'

// Deviation direction → tone. `unknown` is a MUTED honest tone, never a
// severity colour: an unrecognised wire value is not a finding.
const DEVIATION_TONE: Record<DeviationDirection, string> = {
  above: 'border-rose-500/40 bg-rose-500/15 text-rose-300',
  below: 'border-sky-500/40 bg-sky-500/15 text-sky-300',
  within: 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300',
  unknown: 'border-line bg-surf-2 text-ink-3',
}

// Band tone → banded-cell colour. `insufficient` renders as an unbanded
// surface, not as a low severity.
const BAND_CELL: Record<BandTone, string> = {
  good: 'border-emerald-500/40 bg-emerald-500/20 text-emerald-300',
  watch: 'border-amber-500/40 bg-amber-500/20 text-amber-300',
  elevated: 'border-orange-500/40 bg-orange-500/20 text-orange-300',
  high: 'border-rose-500/40 bg-rose-500/20 text-rose-300',
  critical: 'border-red-500/50 bg-red-500/25 text-red-200',
  insufficient: 'border-line bg-surf-3 text-ink-3',
}

export default function EvalBoardsPanel({ registration }: PanelProps) {
  const [tab, setTab] = useState<BoardTab>('baselines')
  const [deviatingOnly, setDeviatingOnly] = useState(false)
  const [days, setDays] = useState(14)
  const [windowHours, setWindowHours] = useState(24)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  const baselines = useQuery({
    queryKey: ['eval_desk_baselines', deviatingOnly],
    queryFn: () => fetchDeskBaselines({ deviatingOnly }),
    enabled: tab === 'baselines',
    refetchInterval: 180_000,
  })

  const trajectory = useQuery({
    queryKey: ['eval_band_trajectory', days],
    queryFn: () => fetchBandTrajectory({ days }),
    enabled: tab === 'trajectory',
    refetchInterval: 180_000,
  })

  const runtime = useQuery({
    queryKey: ['eval_analyst_runtime', windowHours],
    queryFn: () => fetchAnalystRuntime({ windowHours }),
    enabled: tab === 'runtime',
    refetchInterval: 180_000,
  })

  const active = tab === 'baselines' ? baselines : tab === 'trajectory' ? trajectory : runtime

  return (
    <PanelChrome
      registration={registration}
      subtitle={subtitleFor(tab, {
        baselines: baselines.data,
        trajectory: trajectory.data,
        runtime: runtime.data,
        days,
        windowHours,
      })}
      onRefresh={() => void active.refetch()}
      actions={
        <div className="flex items-center gap-2">
          <PanelTabStrip
            tabs={TABS}
            active={tab}
            onChange={(id) => setTab(id as BoardTab)}
            ariaLabel="Eval board"
            testIdPrefix="eval-boards-tab"
          />
          {tab === 'baselines' && (
            <button
              type="button"
              onClick={() => setDeviatingOnly((v) => !v)}
              data-testid="eval-boards-deviating-toggle"
              className={cn(
                'rounded border px-2 py-0.5 text-label',
                deviatingOnly
                  ? 'border-line-strong bg-surf-3 text-ink-1'
                  : 'border-line text-ink-3 hover:text-ink-1',
              )}
            >
              deviating only
            </button>
          )}
          {tab === 'trajectory' && (
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              data-testid="eval-boards-days"
              title="The server validates days in [1, 90] and answers a 400 outside it — it does not clamp."
              className="rounded border border-line bg-surf-1 px-1.5 py-0.5 text-label text-ink-2"
            >
              {TRAJECTORY_DAY_CHOICES.map((d) => (
                <option key={d} value={d}>
                  {d}d
                </option>
              ))}
            </select>
          )}
          {tab === 'runtime' && (
            <select
              value={windowHours}
              onChange={(e) => setWindowHours(Number(e.target.value))}
              data-testid="eval-boards-window"
              className="rounded border border-line bg-surf-1 px-1.5 py-0.5 text-label text-ink-2"
            >
              {RUNTIME_WINDOW_CHOICES.map((h) => (
                <option key={h} value={h}>
                  {h}h
                </option>
              ))}
            </select>
          )}
        </div>
      }
    >
      {tab === 'baselines' && (
        <BaselinesTab
          query={baselines}
          expanded={expanded}
          onToggle={(id) => setExpanded((p) => ({ ...p, [id]: !p[id] }))}
        />
      )}
      {tab === 'trajectory' && <TrajectoryTab query={trajectory} days={days} />}
      {tab === 'runtime' && <RuntimeTab query={runtime} windowHours={windowHours} />}
    </PanelChrome>
  )
}

/** The subtitle never counts a board that has not answered yet — an unread
 *  board reports that it is unread, not a zero. */
function subtitleFor(
  tab: BoardTab,
  ctx: {
    baselines: DeskBaselineBoard | undefined
    trajectory: BandTrajectoryResponse | undefined
    runtime: AnalystRuntimeRow[] | undefined
    days: number
    windowHours: number
  },
): string {
  if (tab === 'baselines') {
    if (!ctx.baselines) return 'reading the desk baselines… · descriptive baseline, never a forecast'
    return `${baselineCountsLine(ctx.baselines)} · descriptive baseline, never a forecast`
  }
  if (tab === 'trajectory') {
    if (!ctx.trajectory) return `${ctx.days}d · reading the band trajectory…`
    const t = trajectoryTotals(ctx.trajectory)
    return `${ctx.days}d · ${t.points} banded points across ${t.desks} desks`
  }
  if (!ctx.runtime) return `${ctx.windowHours}h window · reading analyst runtimes…`
  return `${runtimeWindowLabel(ctx.runtime, ctx.windowHours)} · ${runtimeTotalsLine(ctx.runtime)}`
}

// ---------------------------------------------------------------------------
// Shared little pieces
// ---------------------------------------------------------------------------

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-label uppercase tracking-wider text-ink-3">{label}</div>
      <div className="mt-0.5">{children}</div>
    </div>
  )
}

function Loading({ testId, what }: { testId: string; what: string }) {
  return (
    <div className="text-body text-ink-3" data-testid={testId}>
      loading {what}…
    </div>
  )
}

function LoadError({ testId, text }: { testId: string; text: string }) {
  return (
    <div
      className="flex items-start gap-1.5 rounded border border-rose-500/40 bg-rose-500/10 p-2 text-body text-rose-300"
      data-testid={testId}
    >
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
      <span>{text}</span>
    </div>
  )
}

/** A null statistic, rendered as an absence. Never a 0. */
function Absent({ testId }: { testId?: string }) {
  return (
    <span className="italic text-ink-3" data-testid={testId}>
      {NOT_RECORDED}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Tab 1 — desk baselines
// ---------------------------------------------------------------------------

function BaselinesTab({
  query,
  expanded,
  onToggle,
}: {
  query: { data?: DeskBaselineBoard; isLoading: boolean; error: unknown }
  expanded: Record<string, boolean>
  onToggle: (id: string) => void
}) {
  if (query.isLoading) return <Loading testId="eval-boards-baselines-loading" what="the desk baselines" />
  if (query.error != null) {
    return (
      <LoadError
        testId="eval-boards-baselines-error"
        text={boardErrorText(query.error, 'Desk baselines')}
      />
    )
  }
  const board = query.data
  if (!board) return null

  const state = baselineBoardState(board)
  const note = baselineNote(board)
  const rows = orderedBaselineRows(board.rows)

  return (
    <div className="space-y-2">
      {/* The SERVER's disclaimer, verbatim — the whole reason a band here
          cannot be read as a prediction. */}
      <div
        className="rounded border border-amber-500/40 bg-amber-500/10 p-2 text-body text-amber-300"
        data-testid="eval-boards-baselines-note"
      >
        <InfoTip text={BASELINE_EXPLAIN} testId="eval-boards-baselines-note-tip">
          <span className="text-label uppercase tracking-wider">
            note from the server{note.verbatim ? '' : ' (absent — this wording is ours)'}
          </span>
        </InfoTip>
        <p className="mt-0.5 whitespace-pre-wrap">{note.text}</p>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-label text-ink-3">
        <span data-testid="eval-boards-baselines-counts">{baselineCountsLine(board)}</span>
        <span data-testid="eval-boards-baselines-computed">
          computed {board.computed_at ? `${board.computed_at} (${relTime(board.computed_at)})` : 'never'}
        </span>
      </div>

      {state === 'unavailable' && (
        <div
          className="rounded border border-line-strong bg-surf-1 p-2 text-body text-ink-2"
          data-testid="eval-boards-baselines-unavailable"
        >
          {baselineStateMessage('unavailable')}
        </div>
      )}

      {state === 'empty' && (
        <div className="text-body text-ink-3" data-testid="eval-boards-baselines-empty">
          {baselineStateMessage('empty')}
        </div>
      )}

      {state === 'ready' && (
        <div className="space-y-1" data-testid="eval-boards-baselines-rows">
          {rows.map((row) => (
            <BaselineRow
              key={`${row.desk_id}:${row.metric}`}
              row={row}
              expanded={Boolean(expanded[`${row.desk_id}:${row.metric}`])}
              onToggle={() => onToggle(`${row.desk_id}:${row.metric}`)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function BaselineRow({
  row,
  expanded,
  onToggle,
}: {
  row: DeskBaselineRow
  expanded: boolean
  onToggle: () => void
}) {
  const id = `${row.desk_id}:${row.metric}`
  const direction = deviationDirection(row)
  const thin = insufficientHistoryLabel(row)

  return (
    <div
      className={cn(
        'rounded border border-line bg-surf-1',
        // A thin row is visibly discounted — it is never presented as a finding.
        thin && 'opacity-60',
      )}
      data-testid={`eval-boards-baseline-row-${id}`}
    >
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-start gap-2 px-2 py-1.5 text-left hover:bg-surf-2"
        data-testid={`eval-boards-baseline-toggle-${id}`}
      >
        {expanded ? (
          <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden />
        ) : (
          <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden />
        )}
        <span className="min-w-0 flex-1">
          <span className="text-body text-ink-1">{humanizeId(row.desk_id)}</span>{' '}
          <span className="font-mono text-label text-ink-3">{row.desk_id}</span>
          <span className="block text-label text-ink-2">
            {row.metric} · current {formatMetric(row.current)} · band {bandLabel(row)}
          </span>
        </span>
        {thin && (
          <span
            className="shrink-0 rounded border border-line bg-surf-2 px-1.5 py-0.5 text-label text-ink-3"
            data-testid={`eval-boards-baseline-thin-${id}`}
            title={thin}
          >
            {thin}
          </span>
        )}
        <span
          className={cn(
            'shrink-0 rounded border px-1.5 py-0.5 text-label',
            DEVIATION_TONE[direction],
          )}
          data-testid={`eval-boards-baseline-deviation-${id}`}
        >
          {deviationLabel(row)}
        </span>
        <span
          className="shrink-0 text-label text-ink-3"
          data-testid={`eval-boards-baseline-sample-${id}`}
        >
          {row.active_days}/{row.sample_days} active days
        </span>
      </button>

      {expanded && (
        <div className="border-t border-line px-2 py-2">
          <Section label="Wire fields">
            <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-body">
              {baselineRowFacts(row).map((f) => (
                <div key={f.key} className="contents">
                  <dt className="text-ink-3">{f.key}</dt>
                  <dd className="min-w-0 break-words font-mono text-ink-2">{f.value}</dd>
                </div>
              ))}
            </dl>
          </Section>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Tab 2 — band trajectory
// ---------------------------------------------------------------------------

function TrajectoryTab({
  query,
  days,
}: {
  query: { data?: BandTrajectoryResponse; isLoading: boolean; error: unknown }
  days: number
}) {
  if (query.isLoading) return <Loading testId="eval-boards-trajectory-loading" what="the band trajectory" />
  if (query.error != null) {
    return (
      <LoadError
        testId="eval-boards-trajectory-error"
        text={boardErrorText(query.error, 'Band trajectory')}
      />
    )
  }
  const resp = query.data
  if (!resp) return null

  const truncation = truncationWarning(resp)
  const totals = trajectoryTotals(resp)

  return (
    <div className="space-y-2">
      {truncation && (
        <div
          className="flex items-start gap-1.5 rounded border border-amber-500/40 bg-amber-500/10 p-2 text-body text-amber-300"
          data-testid="eval-boards-trajectory-truncated"
        >
          <ScissorsLineDashed className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <InfoTip text={TRUNCATION_EXPLAIN} testId="eval-boards-truncated-tip">
            <span>{truncation}</span>
          </InfoTip>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 text-label text-ink-3">
        <span data-testid="eval-boards-trajectory-summary">{trajectorySummaryLine(resp)}</span>
        <span data-testid="eval-boards-trajectory-window">
          {days}d window · server now {resp.server_now}
        </span>
      </div>

      {totals.points === 0 ? (
        <div className="text-body text-ink-3" data-testid="eval-boards-trajectory-empty">
          No banded points in this window — the scan ran and returned nothing, which is a
          measured emptiness rather than a failed read.
        </div>
      ) : (
        <div className="space-y-2" data-testid="eval-boards-trajectory-desks">
          {resp.desks.map((desk) => (
            <TrajectoryDesk key={desk.target_id} desk={desk} />
          ))}
        </div>
      )}
    </div>
  )
}

function TrajectoryDesk({ desk }: { desk: DeskTrajectory }) {
  const dimensions = orderedTrajectoryDimensions(desk)
  return (
    <div
      className="rounded border border-line bg-surf-1 px-2 py-1.5"
      data-testid={`eval-boards-trajectory-desk-${desk.target_id}`}
    >
      <div className="text-body text-ink-1">
        {humanizeId(desk.target_id)}{' '}
        <span className="font-mono text-label text-ink-3">{desk.target_id}</span>
      </div>
      <div className="mt-1 space-y-1">
        {dimensions.map(([dimension, points]) => (
          <div key={dimension} className="flex flex-wrap items-center gap-2">
            <span className="w-48 shrink-0 truncate text-label text-ink-2" title={dimension}>
              {humanizeId(dimension)}
            </span>
            <div
              className="flex flex-wrap items-center gap-0.5"
              data-testid={`eval-boards-trajectory-strip-${desk.target_id}-${dimension}`}
            >
              {points.map((p) => (
                <BandCell key={p.scorecard_row_id} point={p} />
              ))}
            </div>
            <span
              className="text-label text-ink-3"
              data-testid={`eval-boards-trajectory-series-${desk.target_id}-${dimension}`}
            >
              {seriesLabel(points)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** One banded point. Flagged faithfulness gets a visible ring + marker — the
 *  band alone would hide that the judgement under it was questioned. */
function BandCell({ point }: { point: TrajectoryPoint }) {
  return (
    <span
      title={pointTitle(point)}
      data-testid={`eval-boards-trajectory-point-${point.scorecard_row_id}`}
      className={cn(
        'inline-flex min-w-[3.5rem] items-center justify-center gap-0.5 rounded border px-1 py-0.5 text-label',
        BAND_CELL[bandTone(point.band)],
        point.faithfulness_flagged && 'ring-1 ring-rose-400',
      )}
    >
      <span className="truncate">{point.band}</span>
      {point.effective_confidence == null ? (
        <span className="italic text-ink-3" title="effective confidence was never recorded — an absence, not a 0">
          ·—
        </span>
      ) : (
        <span className="opacity-80">·{confidenceLabel(point.effective_confidence)}</span>
      )}
      {point.faithfulness_flagged && (
        <span
          aria-label="faithfulness flagged"
          data-testid={`eval-boards-trajectory-flagged-${point.scorecard_row_id}`}
        >
          ⚑
        </span>
      )}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Tab 3 — analyst runtime
// ---------------------------------------------------------------------------

function RuntimeTab({
  query,
  windowHours,
}: {
  query: { data?: AnalystRuntimeRow[]; isLoading: boolean; error: unknown }
  windowHours: number
}) {
  if (query.isLoading) return <Loading testId="eval-boards-runtime-loading" what="analyst runtimes" />
  // No degradation wrapper server-side: an empty table here would assert "no
  // analyst ran". A failed read says it failed.
  if (query.error != null) {
    return (
      <LoadError testId="eval-boards-runtime-error" text={runtimeErrorText(query.error)} />
    )
  }
  const rows = query.data
  if (!rows) return null

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-label text-ink-3">
        {/* `window_hours` is echoed on every row — stated ONCE, here. */}
        <InfoTip text={RUNTIME_EXPLAIN} testId="eval-boards-runtime-tip">
          <span
            className="rounded border border-line bg-surf-2 px-1.5 py-0.5 text-ink-2"
            data-testid="eval-boards-runtime-window"
          >
            {runtimeWindowLabel(rows, windowHours)}
          </span>
        </InfoTip>
        <span data-testid="eval-boards-runtime-totals">{runtimeTotalsLine(rows)}</span>
      </div>

      {rows.length === 0 ? (
        <div className="text-body text-ink-3" data-testid="eval-boards-runtime-empty">
          No analyst runs in this window — the board was read successfully and reported none.
        </div>
      ) : (
        <table className="w-full text-body" data-testid="eval-boards-runtime-table">
          <thead>
            <tr className="text-label uppercase tracking-wider text-ink-3">
              <th className="px-1 py-1 text-left font-normal">analyst</th>
              <th className="px-1 py-1 text-right font-normal">runs (n)</th>
              <th className="px-1 py-1 text-left font-normal">mean</th>
              <th className="px-1 py-1 text-left font-normal">max</th>
              <th className="px-1 py-1 text-left font-normal">non-success</th>
              <th className="px-1 py-1 text-left font-normal">last run</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.analyst_id}
                className="border-t border-line align-top"
                data-testid={`eval-boards-runtime-row-${row.analyst_id}`}
              >
                <td className="px-1 py-1">
                  <span className="text-ink-1">{humanizeId(row.analyst_id)}</span>{' '}
                  <span className="font-mono text-label text-ink-3">{row.analyst_id}</span>
                </td>
                <td className="px-1 py-1 text-right font-mono text-ink-2">{row.runs}</td>
                <td className="px-1 py-1 text-ink-2" data-testid={`eval-boards-runtime-avg-${row.analyst_id}`}>
                  {row.avg_seconds == null ? <Absent /> : avgSecondsLabel(row)}
                </td>
                <td className="px-1 py-1 text-ink-2" data-testid={`eval-boards-runtime-max-${row.analyst_id}`}>
                  {row.max_seconds == null ? <Absent /> : maxSecondsLabel(row)}
                </td>
                <td
                  className={cn(
                    'px-1 py-1',
                    row.non_success > 0 ? 'text-rose-300' : 'text-ink-2',
                  )}
                  data-testid={`eval-boards-runtime-nonsuccess-${row.analyst_id}`}
                >
                  {nonSuccessLabel(row)}
                </td>
                <td className="px-1 py-1 text-ink-3">{relTime(row.last_run_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
