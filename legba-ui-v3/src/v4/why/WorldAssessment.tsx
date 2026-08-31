/**
 * WorldAssessment — the `world_assessor` reading surface (v4 / The Why).
 *
 * This panel is PURELY the world read. It always queries
 * `analyst_id=world_assessor`; nothing about the current selection changes what
 * it fetches. It used to switch to the selected desk's `country_composition`
 * whenever a target was selected, which — since a desk is selected nearly
 * always — meant the tab named "World Assessment" effectively never showed the
 * world. That desk arm is gone: per-desk detail is the Inspector's job, and
 * this surface answers exactly one question, always the same one.
 *
 * Presentation mirrors the Journal panel (`panels/system/Journal.tsx`): the
 * LATEST run rendered in full at the top with its produced_at stated plainly
 * ("World read — 2026-08-04 12:00Z"), and prior runs beneath as a collapsed,
 * browsable history — click one to swap it into the main reading column, with
 * a one-click return to the latest. Markdown + citations render through the
 * shared reading kit (`CitedProse` inside `CitedAssessment`, one verification
 * dialect via `VerdictBadge`), and every run offers a client-side Download
 * (.md / print→PDF).
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { formatDistanceToNow } from 'date-fns'
import { Globe, Download, Printer } from 'lucide-react'
import { apiGet } from '@/lib/api'
import { selectRow } from '@/state/selection'
import type { WorldAssessment as WorldAssessmentT } from '@/v4/why/types'
import CitedAssessment from '@/components/inspector/CitedAssessment'
import { extractCitations, type Citation } from '@/lib/citationsModel'
import { downloadReportMarkdown, printReportPdf, type ReportDoc } from '@/lib/reportDownload'
import { SEVERITY_COLOR } from '@/v4/world/types'

// Re-export the shared markdown map from its own module so existing importers of
// `MD_COMPONENTS` from this path keep working (it moved to break an import cycle).
export { MD_COMPONENTS } from '@/lib/markdownComponents'

const ASSESSOR_ID = 'world_assessor'

/** How many runs to pull: the current read plus enough supersedes to make the
 *  history list worth browsing (the world read is a 12:00Z daily cadence). */
const RUN_LIMIT = 12

/** Minimal view of a `/findings` row — only the fields we project from. */
interface FindingRow {
  id: string
  title?: string | null
  body?: string | null
  severity?: string | null
  confidence?: number | null
  analyst_id?: string | null
  produced_at: string
  data?: unknown
  payload?: unknown
  /** Faithfulness-verify block — a top-level `/findings` sibling of `data`
   *  (null on a legacy/unverified row). Carried through so the citation
   *  chips' hover cards can show per-claim judge verdicts (P1-8). */
  verification?: Record<string, unknown> | null
}

interface FindingsResponse {
  data: FindingRow[]
}

/**
 * Severity → hex. CHANNEL A, the one warm ramp (v4/world/types.ts), with the
 * two extra rungs the unit severities use mapped onto it — same re-key as
 * `CountryUnitsAssessment` (UI_HOLISTIC_DESIGN_2026-08-24 §5.4 #1).
 */
const SEVERITY_HEX: Record<string, string> = {
  critical: SEVERITY_COLOR.critical,
  high: SEVERITY_COLOR.high,
  elevated: '#cf8324', // the interpolated rung between high and medium
  moderate: SEVERITY_COLOR.medium,
  medium: SEVERITY_COLOR.medium,
  low: SEVERITY_COLOR.low,
}

/** Coerce the finding's payload into a plain object. */
function asPayload(row: FindingRow): Record<string, unknown> {
  const raw = row.data ?? row.payload
  if (raw && typeof raw === 'object') return raw as Record<string, unknown>
  if (typeof raw === 'string') {
    try {
      const parsed = JSON.parse(raw)
      if (parsed && typeof parsed === 'object') return parsed as Record<string, unknown>
    } catch {
      return { body: raw }
    }
  }
  return {}
}

function firstString(...vals: unknown[]): string {
  for (const v of vals) {
    if (typeof v === 'string' && v.trim() !== '') return v
  }
  return ''
}

function firstNumber(...vals: unknown[]): number | null {
  for (const v of vals) {
    if (typeof v === 'number' && Number.isFinite(v)) return v
  }
  return null
}

/** WorldAssessment + the composition's citations + its confidence. */
type ProjectedAssessment = WorldAssessmentT & {
  citations: Citation[]
  confidence: number | null
  verification: Record<string, unknown> | null
}

/** Project a findings row into the WorldAssessment shape. */
function projectAssessment(row: FindingRow): ProjectedAssessment {
  const payload = asPayload(row)
  return {
    id: row.id,
    title: firstString(payload.title, row.title) || 'World Assessment',
    summary: firstString(payload.summary, payload.body, payload.assessment, payload.text, row.body),
    severity: row.severity ?? undefined,
    producedAt: Date.parse(row.produced_at),
    citations: extractCitations(payload),
    confidence: firstNumber(payload.confidence, row.confidence),
    verification:
      row.verification && typeof row.verification === 'object' ? row.verification : null,
  }
}

/** Build the `.md` / print `.pdf` ReportDoc from an assessment. */
function toReportDoc(a: ProjectedAssessment, scope: string): ReportDoc {
  return {
    title: a.title,
    body: a.summary,
    producedAt: Number.isFinite(a.producedAt) ? new Date(a.producedAt).toLocaleString() : null,
    severity: a.severity ?? null,
    scope,
    citations: a.citations,
  }
}

/** The Download .md / print→PDF affordance for the current report. */
function DownloadControls({ doc }: { doc: ReportDoc }) {
  return (
    <div className="flex shrink-0 items-center gap-1" data-testid="report-download-controls">
      <button
        type="button"
        onClick={() => downloadReportMarkdown(doc)}
        className="inline-flex items-center gap-1 rounded border border-line px-2 py-1 text-xs font-medium text-ink-2 hover:border-line-strong hover:text-ink-1"
        data-testid="report-download-md"
        title="Download this report as Markdown (.md)"
      >
        <Download className="h-3.5 w-3.5" aria-hidden />
        .md
      </button>
      <button
        type="button"
        onClick={() => {
          if (!printReportPdf(doc)) downloadReportMarkdown(doc)
        }}
        className="inline-flex items-center gap-1 rounded border border-line px-2 py-1 text-xs font-medium text-ink-2 hover:border-line-strong hover:text-ink-1"
        data-testid="report-download-pdf"
        title="Print → Save as PDF (falls back to .md if pop-ups are blocked)"
      >
        <Printer className="h-3.5 w-3.5" aria-hidden />
        PDF
      </button>
    </div>
  )
}

/** `2026-08-04 12:00Z` — the cadence stamp, in UTC, so the header names the run
 *  the operator can go look up rather than a local-time paraphrase of it. */
function formatUtcStamp(ms: number): string {
  const d = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}Z`
  )
}

/**
 * Prior world reads, collapsed — the Journal's browsable-history idea in the
 * shape of a reading column: the runs this one superseded, each a click away
 * from swapping into the main view above.
 */
function RunHistory({
  runs,
  activeId,
  onSelect,
}: {
  runs: ProjectedAssessment[]
  activeId: string
  onSelect: (id: string) => void
}) {
  if (runs.length === 0) return null
  return (
    <section className="mt-8 border-t border-line pt-5" data-testid="world-assessment-history">
      <details>
        <summary className="cursor-pointer text-label uppercase tracking-wider text-ink-3">
          Prior world reads · {runs.length} superseded run{runs.length === 1 ? '' : 's'}
        </summary>
        <ul className="mt-2 space-y-1">
          {runs.map((run) => {
            const active = run.id === activeId
            return (
              <li key={run.id}>
                <button
                  type="button"
                  onClick={() => onSelect(run.id)}
                  aria-current={active ? 'true' : undefined}
                  className={`flex w-full items-baseline gap-2 rounded px-1 py-0.5 text-left text-xs ${
                    active
                      ? 'bg-surf-3 text-ink-1'
                      : 'text-ink-2 hover:bg-surf-1 hover:text-ink-1'
                  }`}
                  data-testid="world-assessment-history-row"
                  title="Read this run in the column above"
                >
                  {run.severity && (
                    <span
                      className="inline-block h-2 w-2 shrink-0 rounded-full"
                      style={{ background: SEVERITY_HEX[run.severity] ?? '#8892a0' }}
                      aria-hidden
                    />
                  )}
                  <span className="truncate">{run.title}</span>
                  {Number.isFinite(run.producedAt) && (
                    <span className="ml-auto shrink-0 font-mono text-ink-3">
                      {formatUtcStamp(run.producedAt)}
                    </span>
                  )}
                </button>
              </li>
            )
          })}
        </ul>
      </details>
    </section>
  )
}

function LoadingSkeleton() {
  return (
    <div className="mx-auto w-full max-w-3xl animate-pulse px-6 py-8" data-testid="world-assessment-loading">
      <div className="mb-3 h-6 w-2/3 rounded bg-surface-50" />
      <div className="mb-6 h-3 w-40 rounded bg-surface-50" />
      <div className="space-y-2.5">
        <div className="h-3 w-full rounded bg-surface-50" />
        <div className="h-3 w-11/12 rounded bg-surface-50" />
        <div className="h-3 w-full rounded bg-surface-50" />
        <div className="h-3 w-4/6 rounded bg-surface-50" />
      </div>
    </div>
  )
}

function EmptyState() {
  return (
    <div className="mx-auto w-full max-w-3xl px-6 py-8" data-testid="world-assessment-empty">
      <div className="flex flex-col items-center gap-3 rounded-lg border border-line bg-surface-100 px-6 py-10 text-center">
        <Globe className="h-7 w-7 text-ink-3" aria-hidden />
        <div className="text-sm font-medium text-ink-2">No world assessment yet</div>
        <div className="max-w-md text-xs leading-relaxed text-ink-3">
          The <span className="font-mono text-ink-2">world_assessor</span> synthesizes one on its
          daily cadence.
        </div>
      </div>
    </div>
  )
}

export default function WorldAssessment() {
  // Which run is in the reading column. `null` means "whatever is latest", so a
  // refetch always carries the operator forward onto the current world read;
  // picking a run out of the history pins it until they come back.
  const [pinnedRunId, setPinnedRunId] = useState<string | null>(null)

  const { data, isLoading, error } = useQuery<FindingsResponse>({
    queryKey: ['world-assessment-findings'],
    refetchInterval: 5 * 60_000,
    queryFn: () =>
      apiGet<FindingsResponse>(`/findings?analyst_id=${ASSESSOR_ID}&limit=${RUN_LIMIT}`),
  })

  // Newest-first list of world_assessor runs → the current read + its history.
  const runs = useMemo<ProjectedAssessment[]>(() => {
    const rows = (data?.data ?? []).filter((r) => r.analyst_id === ASSESSOR_ID)
    rows.sort((a, b) => Date.parse(b.produced_at) - Date.parse(a.produced_at))
    return rows.map(projectAssessment)
  }, [data])

  const latest = runs[0] ?? null
  const history = runs.slice(1)
  // A pinned run that has aged out of the fetched window falls back to the
  // latest rather than blanking the column.
  const active = (pinnedRunId ? runs.find((r) => r.id === pinnedRunId) : null) ?? latest

  if (isLoading) return <LoadingSkeleton />

  if (error instanceof Error) {
    return (
      <div className="mx-auto w-full max-w-3xl px-6 py-8" data-testid="world-assessment-error">
        <div className="rounded-lg border border-rose-900/60 bg-rose-950/30 px-4 py-3 text-sm text-rose-300">
          Couldn’t load the assessment: {error.message}
        </div>
      </div>
    )
  }

  if (!active) return <EmptyState />

  const isLatest = active.id === latest?.id
  const hasTime = Number.isFinite(active.producedAt)
  const scope = 'world_assessor · composed, verified world view'

  return (
    <article className="mx-auto w-full max-w-3xl px-6 py-8" data-testid="world-assessment">
      <header className="mb-5 border-b border-line pb-4">
        {/* The run, named plainly and in UTC — the panel always says which read
            is on screen, and it is always a WORLD read. */}
        <div
          className="flex flex-wrap items-center gap-2 text-label uppercase tracking-wider text-ink-3"
          data-testid="world-assessment-scope"
        >
          <span data-testid="world-assessment-stamp">
            World read — {hasTime ? formatUtcStamp(active.producedAt) : 'time unknown'}
          </span>
          {!isLatest && (
            <span
              className="rounded border border-amber-800/60 bg-amber-950/30 px-1.5 py-0.5 normal-case tracking-normal text-amber-200"
              data-testid="world-assessment-superseded"
            >
              superseded run
            </span>
          )}
        </div>
        <div className="mt-1 text-xs leading-relaxed text-ink-3" data-testid="world-assessment-framing">
          The composed, verified world view from{' '}
          <span className="font-mono text-ink-2">world_assessor</span>, synthesized over the
          per-country compositions. One producer, and always the world &mdash; per-desk detail
          lives in the Inspector.
        </div>
        <div className="mt-1 flex items-start justify-between gap-3">
          <h1 className="text-xl font-semibold text-ink-1">{active.title}</h1>
          <div className="flex shrink-0 items-center gap-1">
            <DownloadControls doc={toReportDoc(active, scope)} />
            <button
              type="button"
              onClick={() => selectRow('finding', active.id, active.title, { origin: 'assessment' })}
              className="rounded border border-line px-2 py-1 text-xs font-medium text-ink-2 hover:border-line-strong hover:text-ink-1"
              data-testid="world-assessment-trace"
              title="Trace this assessment's provenance / inputs in The Why"
            >
              Trace →
            </button>
          </div>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-ink-3">
          {hasTime && (
            <span data-testid="world-assessment-asof">
              as of {formatDistanceToNow(active.producedAt)} ago
            </span>
          )}
          {!isLatest && (
            <button
              type="button"
              onClick={() => setPinnedRunId(null)}
              className="rounded border border-line px-2 py-0.5 text-ink-2 hover:border-line-strong hover:text-ink-1"
              data-testid="world-assessment-back-to-latest"
              title="Return to the current world read"
            >
              ← Back to the latest read
            </button>
          )}
        </div>
      </header>

      {active.summary.trim() !== '' ? (
        <div className="text-sm text-ink-2" data-testid="world-assessment-body">
          <CitedAssessment
            text={active.summary}
            citations={active.citations}
            verification={active.verification}
            confidence={active.confidence}
            analystId={ASSESSOR_ID}
          />
        </div>
      ) : (
        <p className="text-sm text-ink-3" data-testid="world-assessment-nobody">
          This assessment was published without a written summary.
        </p>
      )}

      {/* Prior runs — collapsed, browsable, swap into the column above. */}
      <RunHistory runs={history} activeId={active.id} onSelect={setPinnedRunId} />
    </article>
  )
}

/**
 * Compact teaser of the latest world assessment (#89 de-dup). Reuses the SAME
 * react-query key as the full one-pager, so it shares the cache (no extra fetch).
 */
export function CompactWorldAssessment() {
  const { data, isLoading } = useQuery<FindingsResponse>({
    queryKey: ['world-assessment-findings'],
    refetchInterval: 5 * 60_000,
    // Same key AND same request as the full one-pager — a differing `limit`
    // behind a shared cache key would make the two disagree about the window.
    queryFn: () =>
      apiGet<FindingsResponse>(`/findings?analyst_id=${ASSESSOR_ID}&limit=${RUN_LIMIT}`),
  })
  const assessment = useMemo<WorldAssessmentT | null>(() => {
    const rows = data?.data ?? []
    let newest: FindingRow | null = null
    for (const row of rows) {
      if (row.analyst_id !== ASSESSOR_ID) continue
      if (!newest || Date.parse(row.produced_at) > Date.parse(newest.produced_at)) newest = row
    }
    return newest ? projectAssessment(newest) : null
  }, [data])

  if (isLoading) {
    return <div className="px-1 py-2 text-body text-ink-3">Loading world assessment…</div>
  }
  if (!assessment) {
    return (
      <div className="px-1 py-2 text-body text-ink-3" data-testid="inspector-no-assessment">
        Select anything to inspect. No world assessment published yet.
      </div>
    )
  }
  const hasTime = Number.isFinite(assessment.producedAt)
  return (
    <button
      type="button"
      onClick={() => selectRow('finding', assessment.id, assessment.title, { origin: 'inspector-assessment' })}
      className="w-full rounded-md border border-line bg-surf-1 px-3 py-2.5 text-left hover:border-line-strong"
      data-testid="inspector-compact-assessment"
      title="Open the latest world assessment"
    >
      <div className="text-label font-semibold uppercase tracking-wider text-ink-3">
        Latest world assessment
      </div>
      <div className="mt-1 text-body font-medium text-ink-1">{assessment.title}</div>
      <div className="mt-1 flex items-center gap-2 text-label text-ink-3">
        {hasTime && <span>as of {formatDistanceToNow(assessment.producedAt)} ago</span>}
        <span aria-hidden className="text-ink-3">·</span>
        <span className="text-sky-400">Open assessment →</span>
      </div>
    </button>
  )
}
