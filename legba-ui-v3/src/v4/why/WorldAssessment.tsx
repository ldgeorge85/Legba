/**
 * WorldAssessment — the reading surface for a composition (v4 / The Why).
 *
 * WORLD mode (no selection): the `world_assessor` one-pager — the composed,
 * verified world view — as a calm centered reading column.
 *
 * DESK mode (a country selected): the desk INTELLIGENCE CARD (S7-T3) — reads
 * top-to-bottom as a finished product: banded score + delta → BLUF → the
 * verified composition (expanded) → the per-desk bounded UNIT cards → related →
 * history (older/superseded runs collapsed).
 *
 * Both render markdown + citations through the shared reading kit (`CitedProse`
 * inside `CitedAssessment`, one verification dialect via `VerdictBadge`), and
 * both offer a client-side Download (.md / print→PDF).
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { formatDistanceToNow } from 'date-fns'
import { Globe, Download, Printer } from 'lucide-react'
import { apiGet } from '@/lib/api'
import { selectRow, useSelection } from '@/state/selection'
import type { WorldAssessment as WorldAssessmentT } from '@/v4/why/types'
import { CountryUnitsAssessment } from '@/v4/why/CountryUnitsAssessment'
import CitedAssessment from '@/components/inspector/CitedAssessment'
import { InfoTip } from '@/components/InfoTip'
import { extractCitations, type Citation } from '@/lib/citationsModel'
import { stripCitationMarkers, stripMarkdown, unwrapEnvelope } from '@/lib/proseText'
import { downloadReportMarkdown, printReportPdf, type ReportDoc } from '@/lib/reportDownload'

// U-5 — the desk card's own honest-absence / delta tokens, explained in place
// (this card had NO explainer at all before — not even the Inspector's `?`).
const UNBANDED_EXPLAIN =
  'No severity/confidence band has been computed for this desk yet — an ' +
  'honest absence (nothing to show), not an error.'
const CONF_EXPLAIN =
  "This desk's composition confidence — the same likelihood-style probability " +
  "VerdictBadge's L chip reports, rolled up for the whole desk read."
const CONF_DELTA_EXPLAIN =
  "How much this desk's composition confidence moved since its previous run " +
  '(▲ up / ▼ down) — a trend signal, not a new measurement.'

// Re-export the shared markdown map from its own module so existing importers of
// `MD_COMPONENTS` from this path keep working (it moved to break an import cycle).
export { MD_COMPONENTS } from '@/lib/markdownComponents'

const ASSESSOR_ID = 'world_assessor'
// P3 — the per-country VERIFIED composition (the product for a selected country).
const COUNTRY_COMPOSITION_ID = 'country_composition'

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

/** Severity → hex from the v4 ramp (tailwind.config severity.*). */
const SEVERITY_HEX: Record<string, string> = {
  critical: '#ff5555',
  high: '#ff9955',
  elevated: '#ffbb55',
  moderate: '#ffdd55',
  medium: '#ffdd55',
  low: '#55ff55',
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

/** The one-line BLUF: the first sentence of the body, markdown + citation
 *  markers stripped. Honest empty when the body has none. */
function extractBluf(md: string): string {
  const plain = stripCitationMarkers(stripMarkdown(unwrapEnvelope(md ?? '')))
    .replace(/\s+/g, ' ')
    .trim()
  if (!plain) return ''
  const m = plain.match(/^.*?[.!?](\s|$)/)
  const first = (m ? m[0] : plain).trim()
  return first.length > 260 ? `${first.slice(0, 260)}…` : first
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

/** Severity → a muted banded headline (never the loud severity ramp itself). */
const SEVERITY_BAND: Record<string, { label: string; tone: string }> = {
  critical: { label: 'Critical', tone: 'border-red-800 bg-red-950/50 text-red-200' },
  high: { label: 'High', tone: 'border-rose-800 bg-rose-950/40 text-rose-200' },
  elevated: { label: 'Elevated', tone: 'border-orange-800 bg-orange-950/40 text-orange-200' },
  moderate: { label: 'Moderate', tone: 'border-amber-800 bg-amber-950/40 text-amber-200' },
  medium: { label: 'Moderate', tone: 'border-amber-800 bg-amber-950/40 text-amber-200' },
  low: { label: 'Low', tone: 'border-emerald-800 bg-emerald-950/40 text-emerald-200' },
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

function EmptyState({ targetId }: { targetId?: string | null }) {
  return (
    <div className="mx-auto w-full max-w-3xl px-6 py-8" data-testid="world-assessment-empty">
      <div className="flex flex-col items-center gap-3 rounded-lg border border-line bg-surface-100 px-6 py-10 text-center">
        <Globe className="h-7 w-7 text-ink-3" aria-hidden />
        <div className="text-sm font-medium text-ink-2">
          {targetId ? `No assessment yet for ${targetId}` : 'No world assessment yet'}
        </div>
        <div className="max-w-md text-xs leading-relaxed text-ink-3">
          The{' '}
          <span className="font-mono text-ink-2">
            {targetId ? 'country_composition' : 'world_assessor'}
          </span>{' '}
          synthesizes one every ~6h.
        </div>
      </div>
    </div>
  )
}

/**
 * DESK INTELLIGENCE CARD — the product read for a selected country. Banded score
 * + delta → BLUF → composition (expanded) → unit cards → related → history.
 */
function DeskIntelligenceCard({
  targetId,
  current,
  history,
  isLoading,
}: {
  targetId: string
  current: ProjectedAssessment | null
  history: ProjectedAssessment[]
  isLoading: boolean
}) {
  const scope = `${targetId} · desk intelligence card`
  const band = current?.severity ? SEVERITY_BAND[current.severity] : undefined
  // Confidence delta vs the previous composition run (honest — omitted when
  // there is no prior run or either run lacks a confidence).
  const prev = history[0] ?? null
  const delta =
    current?.confidence != null && prev?.confidence != null
      ? current.confidence - prev.confidence
      : null
  const bluf = current ? extractBluf(current.summary) : ''

  return (
    <div className="mx-auto w-full max-w-3xl px-6 py-8" data-testid="desk-intelligence-card">
      {/* 1 · Banded headline + score + delta */}
      <header className="border-b border-line pb-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-label uppercase tracking-wider text-ink-3">{scope}</div>
            <h1 className="mt-1 truncate text-xl font-semibold text-ink-1">
              {current?.title ?? targetId}
            </h1>
          </div>
          {current && <DownloadControls doc={toReportDoc(current, scope)} />}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs" data-testid="desk-band">
          {band ? (
            <span className={`rounded border px-2 py-0.5 font-medium ${band.tone}`}>{band.label}</span>
          ) : (
            <InfoTip
              text={UNBANDED_EXPLAIN}
              className="rounded border border-line bg-surf-3 px-2 py-0.5 text-ink-2"
              testId="desk-unbanded"
            >
              unbanded
            </InfoTip>
          )}
          {current?.confidence != null && (
            <InfoTip
              text={`${CONF_EXPLAIN} This read: ${(current.confidence * 100).toFixed(0)}%.`}
              className="font-mono text-ink-2"
              testId="desk-confidence"
            >
              conf {(current.confidence * 100).toFixed(0)}%
            </InfoTip>
          )}
          {delta != null && Math.abs(delta) >= 0.005 && (
            <InfoTip
              text={CONF_DELTA_EXPLAIN}
              className={`font-mono ${delta > 0 ? 'text-emerald-400' : 'text-rose-400'}`}
              testId="desk-delta"
            >
              {delta > 0 ? '▲' : '▼'} {Math.abs(delta * 100).toFixed(0)}%
            </InfoTip>
          )}
          {current && Number.isFinite(current.producedAt) && (
            <span className="text-ink-3">as of {formatDistanceToNow(current.producedAt)} ago</span>
          )}
          {current && (
            <button
              type="button"
              onClick={() => selectRow('finding', current.id, current.title, { origin: 'desk-card' })}
              className="ml-auto shrink-0 rounded border border-line px-2 py-0.5 text-ink-2 hover:border-line-strong hover:text-ink-1"
              data-testid="desk-trace"
              title="Trace this composition's provenance / inputs in The Why"
            >
              Trace the flow →
            </button>
          )}
        </div>
      </header>

      {/* 2 · BLUF */}
      {bluf && (
        <div className="mt-4 rounded-lg border border-line bg-surf-1 p-3" data-testid="desk-bluf">
          <div className="text-label uppercase tracking-wider text-ink-3">BLUF</div>
          <p className="mt-1 text-sm leading-relaxed text-ink-1">{bluf}</p>
        </div>
      )}

      {/* 3 · Composition (expanded) — the verified synthesis, the headline product. */}
      <section className="mt-6" data-testid="desk-composition">
        <div className="mb-2 text-label uppercase tracking-wider text-ink-3">
          Verified composition · country_composition
        </div>
        {current ? (
          current.summary.trim() !== '' ? (
            <CitedAssessment
              text={current.summary}
              citations={current.citations}
              verification={current.verification}
              confidence={current.confidence}
              analystId={COUNTRY_COMPOSITION_ID}
            />
          ) : (
            <p className="text-sm text-ink-3">This synthesis was published without a written summary.</p>
          )
        ) : (
          <p className="text-sm text-ink-3" data-testid="desk-composition-pending">
            {isLoading ? 'Loading the composition…' : `No verified composition for ${targetId} yet.`}
          </p>
        )}
      </section>

      {/* 4 · The per-desk bounded UNIT cards. */}
      <section className="mt-8 border-t border-line pt-5" data-testid="desk-units">
        <CountryUnitsAssessment targetId={targetId} />
      </section>

      {/* 5 · Related — the evidence breadth backing this composition. */}
      {current && current.citations.length > 0 && (
        <section className="mt-8 border-t border-line pt-5" data-testid="desk-related">
          <div className="text-label uppercase tracking-wider text-ink-3">Related</div>
          <div className="mt-1 text-xs text-ink-2">
            This read rests on{' '}
            <span className="text-ink-1">{current.citations.length}</span> verified sub-claim
            {current.citations.length === 1 ? '' : 's'} — hover a{' '}
            <span className="font-mono text-accent-info">[[ref:N]]</span> chip above to inspect each,
            or trace the full flow.
          </div>
        </section>
      )}

      {/* 6 · History — older / superseded composition runs, collapsed. */}
      {history.length > 0 && (
        <section className="mt-8 border-t border-line pt-5" data-testid="desk-history">
          <details>
            <summary className="cursor-pointer text-label uppercase tracking-wider text-ink-3">
              History · {history.length} superseded run{history.length === 1 ? '' : 's'}
            </summary>
            <ul className="mt-2 space-y-1">
              {history.map((h) => (
                <li key={h.id}>
                  <button
                    type="button"
                    onClick={() => selectRow('finding', h.id, h.title, { origin: 'desk-history' })}
                    className="flex w-full items-baseline gap-2 rounded px-1 py-0.5 text-left text-xs text-ink-2 hover:bg-surf-1 hover:text-ink-1"
                    data-testid="desk-history-row"
                  >
                    {h.severity && (
                      <span
                        className="inline-block h-2 w-2 shrink-0 rounded-full"
                        style={{ background: SEVERITY_HEX[h.severity] ?? '#8892a0' }}
                        aria-hidden
                      />
                    )}
                    <span className="truncate">{h.title}</span>
                    {Number.isFinite(h.producedAt) && (
                      <span className="ml-auto shrink-0 text-ink-3">
                        {formatDistanceToNow(h.producedAt)} ago
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          </details>
        </section>
      )}
    </div>
  )
}

export default function WorldAssessment() {
  const selection = useSelection((s) => s.selection)
  const targetId = selection?.kind === 'target' ? selection.id : null
  const assessorId = targetId ? COUNTRY_COMPOSITION_ID : ASSESSOR_ID
  const queryUrl = targetId
    ? `/findings?analyst_id=${COUNTRY_COMPOSITION_ID}&target_id=${encodeURIComponent(targetId)}&limit=5`
    : `/findings?analyst_id=${ASSESSOR_ID}&limit=5`

  const { data, isLoading, error } = useQuery<FindingsResponse>({
    queryKey: targetId ? ['assessment-findings', 'country', targetId] : ['world-assessment-findings'],
    refetchInterval: 5 * 60_000,
    queryFn: () => apiGet<FindingsResponse>(queryUrl),
  })

  // Newest-first list of this assessor's runs → current + collapsed history.
  const runs = useMemo<ProjectedAssessment[]>(() => {
    const rows = (data?.data ?? []).filter((r) => r.analyst_id === assessorId)
    rows.sort((a, b) => Date.parse(b.produced_at) - Date.parse(a.produced_at))
    return rows.map(projectAssessment)
  }, [data, assessorId])

  const assessment = runs[0] ?? null
  const history = runs.slice(1)

  // DESK mode — the Intelligence Card (units carry their own loading, so it
  // renders independent of the composition query state).
  if (targetId) {
    return (
      <DeskIntelligenceCard
        targetId={targetId}
        current={assessment}
        history={history}
        isLoading={isLoading}
      />
    )
  }

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

  if (!assessment) return <EmptyState targetId={targetId} />

  const hasTime = Number.isFinite(assessment.producedAt)
  const scope = 'world_assessor · composed, verified world view'

  return (
    <article className="mx-auto w-full max-w-3xl px-6 py-8" data-testid="world-assessment">
      <header className="mb-5 border-b border-line pb-4">
        <div className="text-label uppercase tracking-wider text-ink-3" data-testid="world-assessment-scope">
          world_assessor finding · one producer
        </div>
        <div className="mt-1 text-xs leading-relaxed text-ink-3" data-testid="world-assessment-framing">
          The composed, verified world view, synthesized over the per-country
          compositions &mdash; live now.
        </div>
        <div className="mt-1 flex items-start justify-between gap-3">
          <h1 className="text-xl font-semibold text-ink-1">{assessment.title}</h1>
          <div className="flex shrink-0 items-center gap-1">
            <DownloadControls doc={toReportDoc(assessment, scope)} />
            <button
              type="button"
              onClick={() => selectRow('finding', assessment.id, assessment.title, { origin: 'assessment' })}
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
              as of {formatDistanceToNow(assessment.producedAt)} ago
            </span>
          )}
        </div>
      </header>

      {assessment.summary.trim() !== '' ? (
        <div className="text-sm text-ink-2" data-testid="world-assessment-body">
          <CitedAssessment
            text={assessment.summary}
            citations={assessment.citations}
            verification={assessment.verification}
            confidence={assessment.confidence}
            analystId={ASSESSOR_ID}
          />
        </div>
      ) : (
        <p className="text-sm text-ink-3" data-testid="world-assessment-nobody">
          This assessment was published without a written summary.
        </p>
      )}
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
    queryFn: () => apiGet<FindingsResponse>('/findings?analyst_id=world_assessor&limit=5'),
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
