/**
 * Status bar — the HONESTY FOOTER, plus the console's own small chrome.
 *
 * ## What it says now
 *
 * The bar used to count the CONSOLE: the deployment mode, how many panel kinds
 * were registered, when the registry list last refreshed. The registered-panel
 * count is gone. In its place stands a standing, unprompted statement of how
 * much of the intelligence the reader is reading was actually covered — four
 * figures, each over one of our own objects and each sourced from one live
 * route (`lib/coverageFooter.ts` holds the arithmetic and the wording):
 *
 *     sources 38/57 firing · 12m ago   gaps 5 typed · 1m ago
 *     judge 0.83 faithfulness · 4m ago   grader 44/187 desks graded · 7h ago
 *
 * Each figure carries its unit; each carries its own as-of (relative here, and
 * absolute in its `title` beside every denominator and caveat); an absent
 * reading prints `unmeasured` rather than a zero; and ONE CLICK on a figure
 * opens the panel that owns it, so a number in the chrome is never a number you
 * cannot go and argue with.
 *
 * ## Why these four reads are cheap
 *
 * Every query key here is the key a panel already uses — the System Status
 * acquisition section, the Morning Read GAPS band, the Judge Stats panel's
 * default window, the Eval Scorecard's roster — so when that panel is open the
 * footer adds no request at all, and when it is not the footer's own poll is
 * the slower of the two. The absence read is `enabled` only when the wall is
 * scoped to exactly one desk, because `/v3/absence` answers one desk at a time
 * and a world scope has no count to give.
 */

import { FileDown } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import type { Mode, PanelKind } from '@/types'
import { cn } from '@/lib/cn'
import { PreferencesControls } from '@/components/density/PreferencesControls'
import { useDebugMode } from '@/lib/debugMode'
import { useExportBasket } from '@/state/exportBasket'
import { useScope } from '@/state/scope'
import {
  apiGet,
  fetchJudgeStats,
  getSystemSourceFiring,
  type JudgeStatsResponse,
  type SourceFiringRow,
} from '@/lib/api'
import type { GraderRoster } from '@/lib/graderRosterModel'
import type { AbsenceResponse } from '@/lib/absenceModel'
import {
  COVERAGE_FIGURE_PANEL,
  absenceFigure,
  graderFigure,
  judgeFigure,
  sourcesFigure,
  type CoverageFigure,
} from '@/lib/coverageFooter'

/** The judge window the footer states. Matches the route's own default unit. */
const JUDGE_WINDOW_DAYS = 7
/** The footer's own poll. The owning panels poll faster; react-query takes the
 *  shorter interval whenever one of them is also observing the same key. */
const FOOTER_POLL_MS = 300_000

export interface StatusBarProps {
  mode: Mode
  authenticated: boolean
  lastRefresh: Date | null
  errorText?: string | null
  /** Open the Report Export panel (the basket chip's click target). */
  onOpenExport?: () => void
  /** Open the panel that owns a coverage figure (the figure's click target). */
  onOpenPanel?: (kind: PanelKind) => void
}

/** A10 — the persistent basket count; hidden at zero so the chrome stays lean. */
function ExportBasketChip({ onOpen }: { onOpen?: () => void }) {
  const count = useExportBasket((s) => s.items.length)
  if (count === 0) return null
  return (
    <button
      type="button"
      onClick={onOpen}
      className="inline-flex items-center gap-1 rounded border border-line px-1.5 py-px text-ink-2 hover:text-ink-1"
      title="open Report Export (collection basket)"
      data-testid="statusbar-export-chip"
    >
      <FileDown className="h-3 w-3" aria-hidden />
      export: {count}
    </button>
  )
}

/**
 * One figure. The value and its as-of are both VISIBLE — a footer that hid the
 * as-of behind a hover would be stating a number with no time on it — and the
 * whole sentence rides in the `title`.
 */
function Figure({
  figure,
  onOpen,
}: {
  figure: CoverageFigure
  onOpen?: (kind: PanelKind) => void
}) {
  const panel = COVERAGE_FIGURE_PANEL[figure.id]
  return (
    <button
      type="button"
      data-testid={`coverage-${figure.id}`}
      data-unmeasured={figure.unmeasured ? 'true' : 'false'}
      title={figure.title}
      onClick={() => onOpen?.(panel)}
      className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-sm px-1 hover:bg-surf-2 hover:text-ink-1"
    >
      <span className="text-ink-3">{figure.label}</span>
      <span className={cn(figure.unmeasured ? 'italic text-ink-3' : 'text-ink-1')}>
        {figure.value}
      </span>
      {figure.asOf && <span className="text-ink-3">· {figure.asOf}</span>}
    </button>
  )
}

/**
 * The coverage line. Owns its four reads; every fold and every sentence lives
 * in `lib/coverageFooter.ts`, so what the footer is allowed to claim is
 * argued with in a unit test rather than inside a component.
 */
function CoverageLine({ onOpenPanel }: { onOpenPanel?: (kind: PanelKind) => void }) {
  // `/v3/absence` answers ONE desk. A scope that names exactly one target is
  // the only case with a count to give; anything else states why it has none.
  const scope = useScope((s) => s.scope)
  const scopeTargetId =
    scope && scope.kind === 'target'
      ? scope.id
      : scope && scope.members.targetIds.length === 1
        ? scope.members.targetIds[0]
        : null

  const sources = useQuery<SourceFiringRow[]>({
    queryKey: ['system-status', 'source-firing'],
    queryFn: getSystemSourceFiring,
    refetchInterval: FOOTER_POLL_MS,
    retry: false,
  })

  const absence = useQuery<AbsenceResponse>({
    queryKey: ['morning-judgment', 'absence', scopeTargetId],
    queryFn: () =>
      apiGet<AbsenceResponse>(`/v3/absence?scope=${encodeURIComponent(scopeTargetId as string)}`),
    enabled: scopeTargetId != null,
    staleTime: Infinity,
    retry: false,
  })

  const judge = useQuery<JudgeStatsResponse>({
    queryKey: ['judge-stats', JUDGE_WINDOW_DAYS],
    queryFn: () => fetchJudgeStats({ days: JUDGE_WINDOW_DAYS }),
    refetchInterval: FOOTER_POLL_MS,
    retry: false,
  })

  const grader = useQuery<GraderRoster>({
    queryKey: ['eval-grader-roster'],
    queryFn: () => apiGet<GraderRoster>('/v3/eval/grader_roster?nights=7'),
    refetchInterval: FOOTER_POLL_MS,
    retry: false,
  })

  const figures: CoverageFigure[] = [
    sourcesFigure(sources.data, sources.isError),
    absenceFigure({
      scopeTargetId,
      scopeLabel: scope?.label ?? null,
      res: absence.data,
      failed: absence.isError,
    }),
    judgeFigure(judge.data, judge.isError),
    graderFigure(grader.data, grader.isError),
  ]

  return (
    <div
      data-testid="coverage-line"
      className="flex min-w-0 items-center gap-2 overflow-x-auto"
    >
      {figures.map((f) => (
        <Figure key={f.id} figure={f} onOpen={onOpenPanel} />
      ))}
    </div>
  )
}

export function StatusBar({
  mode,
  authenticated,
  lastRefresh,
  errorText,
  onOpenExport,
  onOpenPanel,
}: StatusBarProps) {
  // The mode badge is developer plumbing; keep it under debug chrome (item 5).
  // The registered-panel counter it used to sit beside is GONE — the footer's
  // job is what the intelligence covered, not what the console loaded.
  const debug = useDebugMode()
  return (
    <footer className="flex h-7 items-center justify-between gap-3 border-t border-line bg-surf-base px-3 text-label text-ink-2">
      <div className="flex min-w-0 items-center gap-3">
        {debug && (
          <span
            className={cn(
              'shrink-0 rounded px-1.5 py-px text-label uppercase tracking-wider',
              mode === 'personal' && 'bg-accent-info/20 text-accent-info',
              mode === 'above_ai' && 'bg-accent-warning/20 text-accent-warning',
              mode === 'cis' && 'bg-accent-ok/20 text-accent-ok',
            )}
          >
            mode: {mode}
          </span>
        )}
        <CoverageLine onOpenPanel={onOpenPanel} />
      </div>
      <div className="flex shrink-0 items-center gap-3">
        <ExportBasketChip onOpen={onOpenExport} />
        {errorText && <span className="max-w-md truncate text-accent-critical">{errorText}</span>}
        {lastRefresh && <span>refreshed {lastRefresh.toLocaleTimeString()}</span>}
        <span>{authenticated ? 'auth: ok' : 'auth: dev'}</span>
        <PreferencesControls />
      </div>
    </footer>
  )
}
