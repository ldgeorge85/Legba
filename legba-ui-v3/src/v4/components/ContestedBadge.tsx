/**
 * ContestedBadge — the contested-claim surface (Holes-B Wave 5, #101).
 *
 * Makes the already-populated `fact_contention` sidecar VISIBLE on the fact /
 * Why provenance view. Given a `factId` (a `facts.id`) it fetches the dispute
 * that fact belongs to (`GET /api/v1/contention?fact_id=…` → 0 or 1 group) and,
 * when the fact is contested, renders:
 *
 *   * a small "Contested" badge (or "Contested — no winner" when the arbiter
 *     ABSTAINED on a near-tie), and
 *   * a per-value SUPPORT PANEL: each competing value with its distinct-source
 *     count, its share of the group's source-credibility, the arbiter score,
 *     and a surfaced-winner flag — so the operator sees WHY one value was
 *     surfaced over the others (or why none was).
 *
 * Self-contained: it owns its own query, renders nothing when the fact is not
 * contested (the common case), and reads through the pure, unit-tested
 * `@/lib/contentionModel` — no DOM math here. Read-only: it never mutates a
 * fact, a group, or a marker.
 *
 * INTEGRATION: drop `<ContestedBadge factId={fact.id} />` next to a rendered
 * fact line / claim row in the Why provenance view (or the Claims panel). A
 * `subject`/`predicate` variant can fetch `?subject=` instead when no fact id
 * is in hand; here we use the precise `fact_id` lookup.
 */
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import { apiGet, ApiError } from '@/lib/api'
import { cn } from '@/lib/cn'
import {
  contentionForFact,
  badgeLabel,
  type ContentionPage,
  type ContentionValueView,
} from '@/lib/contentionModel'

interface ContestedBadgeProps {
  /** The `facts.id` whose dispute to surface. */
  factId: string
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

export default function ContestedBadge({
  factId,
  showPanel = true,
  className,
}: ContestedBadgeProps) {
  const { data, error } = useQuery<ContentionPage>({
    queryKey: ['contention-for-fact', factId],
    enabled: Boolean(factId),
    queryFn: () =>
      apiGet<ContentionPage>(`/contention?fact_id=${encodeURIComponent(factId)}`),
  })

  // A 404 / uncontested fact is the normal case — render nothing.
  if (error) {
    if (error instanceof ApiError && error.status === 404) return null
    return null
  }

  const view = contentionForFact(data)
  if (!view || !view.isLive) return null

  return (
    <div
      className={cn('flex flex-col gap-1', className)}
      data-testid="contested-badge"
    >
      <span
        className={cn(
          'inline-flex w-fit items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold',
          view.abstained
            ? 'bg-rose-500/15 text-rose-300 ring-1 ring-rose-500/30'
            : 'bg-amber-500/15 text-amber-300 ring-1 ring-amber-500/30',
        )}
        title={
          view.abstained
            ? 'Sources disagree and the arbiter surfaced no winner — treat as unresolved.'
            : `Sources disagree on ${view.subjectKey} ${view.predicateKey}; the arbiter surfaced a winner.`
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
