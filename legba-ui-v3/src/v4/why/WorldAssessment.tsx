/**
 * WorldAssessment — the world_assessor one-pager surface (v4 / The Why).
 *
 * The `world_assessor` analyst (registered separately, Wave-2 B1) emits Findings
 * with `analyst_id === 'world_assessor'`; every ~6h it synthesizes a single
 * narrative one-pager. We read the findings feed, filter client-side to that
 * analyst (the `analyst_id` query param may not be honoured everywhere), pick the
 * newest by `produced_at`, project it into the shared {@link WorldAssessment}
 * shape, and render it as a calm centered reading column with a dark-styled
 * markdown body.
 *
 * Markdown: react-markdown + remark-gfm, themed via an explicit `components` map
 * (the project does NOT enable @tailwindcss/typography, so `prose` classes would
 * be inert — we style each element directly instead).
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { Components } from 'react-markdown'
import { formatDistanceToNow } from 'date-fns'
import { Globe } from 'lucide-react'
import { apiGet } from '@/lib/api'
import { selectRow, useSelection } from '@/state/selection'
import type { WorldAssessment as WorldAssessmentT } from '@/v4/why/types'
import { CountryUnitsAssessment } from '@/v4/why/CountryUnitsAssessment'
import CitedAssessment from '@/components/inspector/CitedAssessment'
import { extractCitations, type Citation } from '@/lib/citationsModel'

const ASSESSOR_ID = 'world_assessor'
// P3 — the per-country VERIFIED composition (the product for a selected country).
// Replaces the RETIRED `country_assessor` monolith, whose output is stale/undated.
const COUNTRY_COMPOSITION_ID = 'country_composition'

/** Minimal view of a `/findings` row — only the fields we project from. The
 *  feed may also carry a `payload` alias for `data`, so we accept either. */
interface FindingRow {
  id: string
  title?: string | null
  body?: string | null
  severity?: string | null
  analyst_id?: string | null
  produced_at: string
  data?: unknown
  payload?: unknown
}

interface FindingsResponse {
  data: FindingRow[]
}

/** Severity → hex from the v4 ramp (tailwind.config severity.*). */
const SEVERITY_HEX: Record<string, string> = {
  critical: '#ff5555',
  high: '#ff9955',
  medium: '#ffdd55',
  low: '#55ff55',
}

/** Coerce the finding's payload into a plain object, whether it arrives as an
 *  already-parsed dict, a JSON string, or under `data` / `payload`. */
function asPayload(row: FindingRow): Record<string, unknown> {
  const raw = row.data ?? row.payload
  if (raw && typeof raw === 'object') return raw as Record<string, unknown>
  if (typeof raw === 'string') {
    try {
      const parsed = JSON.parse(raw)
      if (parsed && typeof parsed === 'object') return parsed as Record<string, unknown>
    } catch {
      // Not JSON — treat the string itself as the body below.
      return { body: raw }
    }
  }
  return {}
}

/** Pick the first non-empty string among the candidates. */
function firstString(...vals: unknown[]): string {
  for (const v of vals) {
    if (typeof v === 'string' && v.trim() !== '') return v
  }
  return ''
}

/** WorldAssessment + the composition's citation list (extracted from the
 *  finding's `data.citations` envelope so the one-pager can render CITED prose). */
type ProjectedAssessment = WorldAssessmentT & { citations: Citation[] }

/** Project the newest world_assessor finding into the WorldAssessment shape. */
function projectAssessment(row: FindingRow): ProjectedAssessment {
  const payload = asPayload(row)
  return {
    id: row.id,
    title: firstString(payload.title, row.title) || 'World Assessment',
    summary: firstString(
      payload.summary,
      payload.body,
      payload.assessment,
      payload.text,
      row.body,
    ),
    severity: row.severity ?? undefined,
    producedAt: Date.parse(row.produced_at),
    // P1-T3 — the composition cites sub-claim findings with `[[ref:N]]` ordinal
    // markers; the list lives at `<envelope>.data.citations`. Empty for an
    // uncited/legacy row (the card then renders prose plainly, no fabrication).
    citations: extractCitations(payload),
  }
}

/** Dark-theme element map for the markdown body (replaces the absent `prose`).
 *  Exported so the Inspector renders finding/assessment reports identically. */
export const MD_COMPONENTS: Components = {
  h1: ({ children }) => (
    <h1 className="mb-3 mt-5 text-lg font-semibold text-slate-100 first:mt-0">{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 className="mb-2 mt-5 text-base font-semibold text-slate-100 first:mt-0">{children}</h2>
  ),
  h3: ({ children }) => (
    <h3 className="mb-2 mt-4 text-sm font-semibold text-slate-200 first:mt-0">{children}</h3>
  ),
  p: ({ children }) => <p className="mb-3 leading-relaxed text-slate-300">{children}</p>,
  ul: ({ children }) => (
    <ul className="mb-3 list-disc space-y-1 pl-5 text-slate-300 marker:text-slate-600">
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className="mb-3 list-decimal space-y-1 pl-5 text-slate-300 marker:text-slate-600">
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="text-accent-info underline decoration-dotted hover:text-blue-300"
    >
      {children}
    </a>
  ),
  strong: ({ children }) => <strong className="font-semibold text-slate-100">{children}</strong>,
  em: ({ children }) => <em className="italic text-slate-300">{children}</em>,
  blockquote: ({ children }) => (
    <blockquote className="mb-3 border-l-2 border-slate-700 pl-3 text-slate-400 italic">
      {children}
    </blockquote>
  ),
  code: ({ children }) => (
    <code className="rounded bg-surface-50 px-1 py-0.5 font-mono text-[0.85em] text-slate-200">
      {children}
    </code>
  ),
  pre: ({ children }) => (
    <pre className="mb-3 overflow-auto rounded border border-slate-800 bg-surface-300 p-3 text-xs text-slate-200">
      {children}
    </pre>
  ),
  hr: () => <hr className="my-4 border-slate-800" />,
  table: ({ children }) => (
    <div className="mb-3 overflow-auto">
      <table className="w-full border-collapse text-sm text-slate-300">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-slate-800 bg-surface-100 px-2 py-1 text-left font-medium text-slate-200">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border border-slate-800 px-2 py-1 align-top">{children}</td>
  ),
}

/** Small severity pill, colored from the v4 ramp. */
function SeverityChip({ severity }: { severity: string }) {
  const hex = SEVERITY_HEX[severity] ?? '#94a3b8' // slate-400 fallback
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border border-slate-700 bg-surface-100 px-2 py-0.5 text-[11px] leading-none text-slate-300"
      title={`severity: ${severity}`}
    >
      <span aria-hidden className="h-2 w-2 rounded-full" style={{ backgroundColor: hex }} />
      {severity}
    </span>
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
        <div className="mt-5 h-3 w-full rounded bg-surface-50" />
        <div className="h-3 w-10/12 rounded bg-surface-50" />
        <div className="h-3 w-3/4 rounded bg-surface-50" />
      </div>
    </div>
  )
}

function EmptyState({ targetId }: { targetId?: string | null }) {
  return (
    <div className="mx-auto w-full max-w-3xl px-6 py-8" data-testid="world-assessment-empty">
      <div className="flex flex-col items-center gap-3 rounded-lg border border-slate-800 bg-surface-100 px-6 py-10 text-center">
        <Globe className="h-7 w-7 text-slate-600" aria-hidden />
        <div className="text-sm font-medium text-slate-300">
          {targetId ? `No assessment yet for ${targetId}` : 'No world assessment yet'}
        </div>
        <div className="max-w-md text-xs leading-relaxed text-slate-500">
          The{' '}
          <span className="font-mono text-slate-400">
            {targetId ? 'country_composition' : 'world_assessor'}
          </span>{' '}
          synthesizes one every ~6h.
        </div>
      </div>
    </div>
  )
}

export default function WorldAssessment() {
  // #89 polish — the reading column FOLLOWS the selection: a selected country
  // (kind:'target') shows ITS country_composition (the P3 verified synthesis) in
  // the same column; otherwise the world_assessor composition. World mode keeps
  // the shared 'world-assessment-findings' query key (cache-shared with the
  // banner + Inspector compact teaser); country mode gets a per-target key.
  const selection = useSelection((s) => s.selection)
  const targetId = selection?.kind === 'target' ? selection.id : null
  const assessorId = targetId ? COUNTRY_COMPOSITION_ID : ASSESSOR_ID
  const queryUrl = targetId
    ? `/findings?analyst_id=${COUNTRY_COMPOSITION_ID}&target_id=${encodeURIComponent(targetId)}&limit=5`
    : `/findings?analyst_id=${ASSESSOR_ID}&limit=5`

  const { data, isLoading, error } = useQuery<FindingsResponse>({
    queryKey: targetId ? ['assessment-findings', 'country', targetId] : ['world-assessment-findings'],
    refetchInterval: 5 * 60_000, // re-poll; the assessor runs every ~6h
    // Filter SERVER-SIDE by analyst_id (+ target_id for a country). These
    // assessors emit ~1 finding per 6h, never in the recent global window, so
    // the targeted query is the only way the panel reliably finds the row.
    queryFn: () => apiGet<FindingsResponse>(queryUrl),
  })

  const assessment = useMemo<ProjectedAssessment | null>(() => {
    const rows = data?.data ?? []
    let newest: FindingRow | null = null
    for (const row of rows) {
      if (row.analyst_id !== assessorId) continue
      if (!newest || Date.parse(row.produced_at) > Date.parse(newest.produced_at)) {
        newest = row
      }
    }
    return newest ? projectAssessment(newest) : null
  }, [data, assessorId])

  // P2-T8 / P3 — for a selected COUNTRY the bounded UNITS are shown as the
  // headline; the country_composition (the verified P3 synthesis OVER those
  // units) renders in a collapsible below. Rendered independent of the
  // composition query state so the units show immediately (they carry their own
  // loading).
  if (targetId) {
    return (
      <div className="mx-auto w-full max-w-3xl px-6 py-8" data-testid="country-read-column">
        <CountryUnitsAssessment targetId={targetId} />
        {/* Expanded by default (item 2) — the verified composition is the product,
            not a click-to-reveal teaser. */}
        <details
          open
          className="mt-8 border-t border-slate-800 pt-4"
          data-testid="country-composition-synthesis"
        >
          <summary className="cursor-pointer text-label uppercase tracking-wider text-slate-500">
            Country composition (verified) · country_composition (the P3 synthesis over the units above)
          </summary>
          {assessment ? (
            <div className="mt-3">
              <div className="mb-2 flex items-start justify-between gap-3">
                <h2 className="text-lg font-semibold text-slate-200">{assessment.title}</h2>
                <button
                  type="button"
                  onClick={() => selectRow('finding', assessment.id, assessment.title, { origin: 'assessment' })}
                  className="shrink-0 rounded border border-slate-700 px-2 py-1 text-xs font-medium text-slate-300 hover:border-slate-500 hover:text-slate-100"
                  data-testid="composition-trace"
                  title="Trace this composition's provenance / inputs in The Why"
                >
                  Trace the flow →
                </button>
              </div>
              {assessment.summary.trim() !== '' ? (
                <div className="text-sm text-slate-300">
                  <CitedAssessment text={assessment.summary} citations={assessment.citations} />
                </div>
              ) : (
                <p className="text-sm text-slate-500">This synthesis was published without a written summary.</p>
              )}
            </div>
          ) : (
            <p className="mt-3 text-sm text-slate-500" data-testid="composition-pending">
              {isLoading ? 'Loading the composition…' : `No verified composition for ${targetId} yet.`}
            </p>
          )}
        </details>
      </div>
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

  return (
    <article
      className="mx-auto w-full max-w-3xl px-6 py-8"
      data-testid="world-assessment"
    >
      <header className="mb-5 border-b border-slate-800 pb-4">
        <div className="text-label uppercase tracking-wider text-slate-500" data-testid="world-assessment-scope">
          {targetId ? `${targetId} · country assessment` : 'world_assessor finding · one producer'}
        </div>
        {!targetId && (
          <div className="mt-1 text-xs leading-relaxed text-slate-500" data-testid="world-assessment-framing">
            The composed, verified world view, synthesized over the per-country
            compositions &mdash; live now.
          </div>
        )}
        <div className="mt-1 flex items-start justify-between gap-3">
          <h1 className="text-xl font-semibold text-slate-100">{assessment.title}</h1>
          {/* #89 — drop the operator into the lineage flow (ProvenanceTrail +
              LineageGraph in The Why) for this assessment's derived_from DAG. */}
          <button
            type="button"
            onClick={() =>
              selectRow('finding', assessment.id, assessment.title, { origin: 'assessment' })
            }
            className="shrink-0 rounded border border-slate-700 px-2 py-1 text-xs font-medium text-slate-300 hover:border-slate-500 hover:text-slate-100"
            data-testid="world-assessment-trace"
            title="Trace this assessment's provenance / inputs in The Why"
          >
            Trace the flow →
          </button>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
          {hasTime && (
            <span data-testid="world-assessment-asof">
              as of {formatDistanceToNow(assessment.producedAt)} ago
            </span>
          )}
          {assessment.severity && (
            <>
              {hasTime && <span aria-hidden className="text-slate-700">·</span>}
              <SeverityChip severity={assessment.severity} />
            </>
          )}
        </div>
      </header>

      {assessment.summary.trim() !== '' ? (
        <div className="text-sm text-slate-300" data-testid="world-assessment-body">
          <CitedAssessment text={assessment.summary} citations={assessment.citations} />
        </div>
      ) : (
        <p className="text-sm text-slate-500" data-testid="world-assessment-nobody">
          This assessment was published without a written summary.
        </p>
      )}
    </article>
  )
}

/**
 * Compact teaser of the latest world assessment (#89 de-dup). Reuses the SAME
 * react-query key as the full one-pager, so it shares the cache (no extra
 * fetch). Used in the Inspector empty state so the full one-pager renders in
 * exactly ONE place (The Why) instead of twice; clicking opens it as a finding.
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
      onClick={() =>
        selectRow('finding', assessment.id, assessment.title, { origin: 'inspector-assessment' })
      }
      className="w-full rounded-md border border-line bg-surf-1 px-3 py-2.5 text-left hover:border-slate-500"
      data-testid="inspector-compact-assessment"
      title="Open the latest world assessment"
    >
      <div className="text-label font-semibold uppercase tracking-wider text-ink-3">
        Latest world assessment
      </div>
      <div className="mt-1 text-body font-medium text-ink-1">{assessment.title}</div>
      <div className="mt-1 flex items-center gap-2 text-label text-ink-3">
        {hasTime && <span>as of {formatDistanceToNow(assessment.producedAt)} ago</span>}
        <span aria-hidden className="text-slate-600">·</span>
        <span className="text-sky-400">Open assessment →</span>
      </div>
    </button>
  )
}
