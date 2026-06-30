/**
 * ContestedBadge — the contested-claim surface (Holes-B Wave 5, #101).
 *
 * Makes the already-populated `fact_contention` sidecar VISIBLE on the fact /
 * Why provenance view AND on the findings-backed Claims panel. It looks the
 * dispute up by EITHER:
 *
 *   * `factId` — a true `facts.id` (e.g. a lineage node whose `row_kind` is
 *     `fact`). Precise: `GET /api/v1/contention?fact_id=…` → 0 or 1 group; OR
 *   * `subject` — a lower-cased subject string (e.g. a finding statement). The
 *     Claims/findings views render FINDINGS, which carry NO real `facts.id`,
 *     so the only handle there is the subject: `?subject=<lowercased>` → 0..N
 *     groups (one per disputed predicate); we surface the first LIVE one.
 *
 * When the claim is contested it renders:
 *
 *   * a small "Contested" badge (or "Contested — no winner" when the arbiter
 *     ABSTAINED on a near-tie), and
 *   * a per-value SUPPORT PANEL: each competing VALUE cluster with its
 *     distinct-source count, its share of the group's source-credibility, the
 *     arbiter score, and a surfaced-winner flag — so the operator sees WHY one
 *     value was surfaced over the others (or why none was).
 *
 * Self-contained: it owns its own query, renders nothing when the claim is not
 * contested (the common case → zero visual noise), and reads through the pure,
 * unit-tested `@/lib/contentionModel` — no DOM math here. Read-only: it never
 * mutates a fact, a group, or a marker. A LOOKUP FAILURE (5xx) is NOT silently
 * masked as "uncontested" — it shows a subtle, non-intrusive affordance.
 *
 * INTEGRATION: in the Why provenance trail, drop `<ContestedBadge factId={…} />`
 * on a lineage node with `row_kind === 'fact'`; in the Claims panel (findings,
 * no fact id), use `<ContestedBadge subject={claim.statement} />`.
 */
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, AlertCircle } from 'lucide-react'
import { apiGet, ApiError } from '@/lib/api'
import { cn } from '@/lib/cn'
import {
  contentionForFact,
  contentionForSubject,
  badgeLabel,
  type ContentionPage,
  type ContentionView,
  type ContentionValueView,
} from '@/lib/contentionModel'

interface ContestedBadgeProps {
  /** The `facts.id` whose dispute to surface (precise lookup). Mutually
   *  exclusive with `subject`; `factId` wins if both are given. */
  factId?: string
  /** The claim subject to surface a dispute for, when no fact id is in hand
   *  (e.g. a finding statement). Lower-cased before the `?subject=` lookup. */
  subject?: string
  /** Render the per-value support panel inline (default true). When false,
   *  only the compact badge shows (e.g. in a dense list). */
  showPanel?: boolean
  className?: string
}

function pct(share: number): string {
  return `${Math.round(share * 100)}%`
}

/** One competing-value row in the support panel. */
function ValueRow({ v }: { v: ContentionValueView }) {
  return (
    <li
      className={cn(
        'flex items-center justify-between gap-2 rounded px-2 py-1 text-[11px]',
        v.surfacedWinner ? 'bg-amber-500/10 ring-1 ring-amber-500/30' : 'bg-surface-100',
        v.isJunk && 'opacity-50',
      )}
      data-testid="contention-value-row"
    >
      <span className="flex min-w-0 items-center gap-1.5">
        <span className="truncate font-medium text-slate-200" title={v.valueKey}>
          {v.valueKey}
        </span>
        {v.surfacedWinner && (
          <span className="shrink-0 rounded-full bg-amber-500/20 px-1.5 text-[9px] font-semibold uppercase tracking-wide text-amber-300">
            surfaced
          </span>
        )}
        {v.isJunk && (
          <span
            className="shrink-0 rounded-full bg-slate-700 px-1.5 text-[9px] uppercase text-slate-400"
            title={v.junkReason ?? 'junk-gated'}
          >
            junk
          </span>
        )}
      </span>
      <span className="flex shrink-0 items-center gap-2 tabular-nums text-slate-400">
        <span title="distinct sources">{v.distinctSourceCount} src</span>
        <span title="source-credibility share">{pct(v.credibilityShare)}</span>
        {v.arbiterScore !== null && (
          <span title="arbiter Q·C·R·F score">{v.arbiterScore.toFixed(2)}</span>
        )}
      </span>
    </li>
  )
}

/** The contested-claim presentation, shared by both lookup paths. */
function ContestedView({
  view,
  showPanel,
  className,
}: {
  view: ContentionView
  showPanel: boolean
  className?: string
}) {
  return (
    <div className={cn('flex flex-col gap-1', className)} data-testid="contested-badge">
      <span
        className={cn(
          'inline-flex w-fit items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold',
          view.abstained
            ? 'bg-rose-500/15 text-rose-300 ring-1 ring-rose-500/30'
            : 'bg-amber-500/15 text-amber-300 ring-1 ring-amber-500/30',
        )}
        title={
          view.abstained
            ? `Competing values on ${view.subjectKey} ${view.predicateKey} disagree and the arbiter surfaced no winner — treat as unresolved.`
            : `Competing values on ${view.subjectKey} ${view.predicateKey}; the arbiter surfaced a winner.`
        }
      >
        <AlertTriangle className="h-3 w-3" aria-hidden />
        {badgeLabel(view)}
      </span>

      {showPanel && view.values.length > 0 && (
        <ul className="flex flex-col gap-0.5" data-testid="contention-support-panel">
          {view.values.map((v) => (
            <ValueRow key={v.valueKey} v={v} />
          ))}
        </ul>
      )}
    </div>
  )
}

export default function ContestedBadge({
  factId,
  subject,
  showPanel = true,
  className,
}: ContestedBadgeProps) {
  // factId wins when both are supplied (the precise lookup).
  const byFact = Boolean(factId)
  const subjectKey = subject?.trim().toLowerCase() ?? ''
  const enabled = byFact ? Boolean(factId) : Boolean(subjectKey)

  const { data, error } = useQuery<ContentionPage>({
    queryKey: byFact ? ['contention-for-fact', factId] : ['contention-for-subject', subjectKey],
    enabled,
    // No retry: a 404 (uncontested) is expected and a 5xx should surface
    // promptly rather than spin — the default `retry: 1` would do neither well.
    retry: false,
    queryFn: () =>
      byFact
        ? apiGet<ContentionPage>(`/contention?fact_id=${encodeURIComponent(factId!)}`)
        : apiGet<ContentionPage>(`/contention?subject=${encodeURIComponent(subjectKey)}`),
  })

  // Distinguish "uncontested" from "lookup failed":
  //  - 404 ⇒ the fact/subject is not contested → render nothing (correct, the
  //    common case);
  //  - any other error (5xx / network) ⇒ DO NOT masquerade as uncontested —
  //    show a subtle, non-intrusive affordance so a genuinely-contested claim
  //    is never silently hidden by a transient failure.
  if (error) {
    if (error instanceof ApiError && error.status === 404) return null
    return (
      <span
        className={cn(
          'inline-flex w-fit items-center gap-1 text-[10px] text-slate-500',
          className,
        )}
        title={`Contested-claim lookup failed${
          error instanceof ApiError ? ` (HTTP ${error.status})` : ''
        } — disputes can't be shown right now.`}
        data-testid="contested-badge-error"
      >
        <AlertCircle className="h-3 w-3" aria-hidden />
        contention unavailable
      </span>
    )
  }

  const view = byFact ? contentionForFact(data) : contentionForSubject(data)
  if (!view || !view.isLive) return null

  return <ContestedView view={view} showPanel={showPanel} className={className} />
}
