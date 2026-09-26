/**
 * UnitCorrectnessBadge — the per-unit CORRECTNESS number, beside the read (G2).
 *
 * A composable element, not a panel and not a route: it mounts wherever a
 * bounded unit's read is already rendered (the country desk cards, the cited
 * assessment card) and needs only the pair that keys the measurement —
 * `(analystId, targetId)`.
 *
 * WHAT IT RENDERS, and the three states it can be in:
 *
 *   * a row exists  → the server-composed badge line, verbatim:
 *     `correctness 90.9% (10/11) · coverage 24.4% (n=45) · as of 2026-09-16
 *     · single-family`. Clicking it opens the per-claim ledger — every
 *     family's label, the adjudicated outcome, the decisive spans — which is
 *     the surface on which a disputed share is re-argued claim by claim.
 *   * no row, and the country has no current reference → a muted `no
 *     reference` / `reference stale` chip. An honest absence, not a score.
 *   * no row, and the reference IS current → NOTHING. The grader simply has
 *     not reached this unit yet, and an empty space is the truthful render.
 *
 * What it will never do is show a number it does not have. A fabricated 100%
 * beside an ungraded read is the exact failure this whole track exists to make
 * impossible, so every path above ends in real data or in nothing.
 *
 * Mirrors `UnitEvalBadge`: self-fetching, memoised at the model layer so eight
 * desk badges share one request, and silent on a fetch failure.
 */
import { useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, Scale } from 'lucide-react'
import { InfoTip } from '@/components/InfoTip'
import { cn } from '@/lib/cn'
import {
  CORRECTNESS_EXPLAIN,
  REFERENCE_STATE_LABEL,
  decisiveSpans,
  familyLabels,
  fetchUnitCorrectness,
  findUnitCorrectness,
  orderLedger,
  type UnitCorrectness,
  type UnitCorrectnessClaim,
  type UnitCorrectnessPage,
} from '@/lib/unitCorrectnessModel'

/** Adjudicated outcome → its chip classes. Never colour alone: the word is
 *  always printed, so the label survives a monochrome or colour-blind read. */
const LABEL_CLASS: Record<string, string> = {
  contradicts: 'bg-rose-950/40 text-rose-300',
  contains: 'bg-emerald-950/40 text-emerald-300',
  split: 'bg-amber-950/40 text-amber-300',
  unparseable: 'bg-surf-1 text-ink-3',
  silent: 'bg-surf-1 text-ink-3',
}

function ClaimRow({ claim }: { claim: UnitCorrectnessClaim }) {
  const spans = decisiveSpans(claim)
  const labels = familyLabels(claim)
  return (
    <li
      className="rounded border border-slate-800 bg-slate-900/40 px-2 py-1.5"
      data-testid="correctness-claim-row"
      data-adjudicated={claim.adjudicated}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <span
          className={cn(
            'rounded px-1.5 py-0.5 text-label',
            LABEL_CLASS[claim.adjudicated] ?? 'bg-surf-1 text-ink-3',
          )}
        >
          {claim.adjudicated}
        </span>
        {/* Every family's label, UNPOOLED — a family that read the claim
            differently is visible rather than averaged away. */}
        {labels.map(({ family, label }) => (
          <span key={family} className="text-label text-ink-3">
            {family}: {label}
          </span>
        ))}
        {claim.single_family && (
          <span className="text-label italic text-ink-3">single-family</span>
        )}
      </div>
      <p className="mt-1 text-xs text-ink-2">{claim.claim_text}</p>
      {spans.map(({ family, span }) => (
        <p
          key={family}
          className="mt-1 border-l-2 border-slate-700 pl-2 text-xs italic text-ink-3"
          data-testid="correctness-decisive-span"
        >
          {family}: “{span}”
        </p>
      ))}
    </li>
  )
}

export interface UnitCorrectnessBadgeProps {
  /** The bounded unit's analyst id (a desk id, or `country_composition`). */
  analystId?: string | null
  /** The country target the unit was graded on. Both are required — the
   *  measurement is keyed on the PAIR, and one without the other is not a
   *  lookup, it is a guess. */
  targetId?: string | null
  className?: string
}

export function UnitCorrectnessBadge({
  analystId,
  targetId,
  className,
}: UnitCorrectnessBadgeProps) {
  const [page, setPage] = useState<UnitCorrectnessPage | null>(null)
  const [open, setOpen] = useState(false)
  const [ledger, setLedger] = useState<UnitCorrectnessClaim[] | null>(null)

  useEffect(() => {
    let cancelled = false
    if (!analystId || !targetId) {
      setPage(null)
      return
    }
    fetchUnitCorrectness(targetId)
      .then((p) => {
        if (!cancelled) setPage(p)
      })
      .catch(() => {
        if (!cancelled) setPage(null)
      })
    return () => {
      cancelled = true
    }
  }, [analystId, targetId])

  // The ledger is pulled only when the drawer is actually opened — the badge
  // needs the shares, the drawer needs the claims, and most readers never ask.
  useEffect(() => {
    let cancelled = false
    if (!open || !analystId || !targetId || ledger !== null) return
    fetchUnitCorrectness(targetId, true)
      .then((p) => {
        if (cancelled) return
        setLedger(findUnitCorrectness(p, analystId)?.claims ?? [])
      })
      .catch(() => {
        if (!cancelled) setLedger([])
      })
    return () => {
      cancelled = true
    }
  }, [open, analystId, targetId, ledger])

  const row: UnitCorrectness | null = findUnitCorrectness(page, analystId)
  // Read defensively: a route that answered with something other than a
  // correctness page (a proxy error body, a stubbed fetch in a sibling test)
  // must render NOTHING, never throw inside a read the operator is trying to
  // look at. The badge is an annotation; it may not take the page down.
  const refState = page?.reference?.state

  if (!row) {
    // No number for this unit. Say why ONLY when the reason is the reference
    // itself; otherwise render nothing at all rather than an empty chip.
    if (!refState || refState === 'current') return null
    return (
      <InfoTip
        text={CORRECTNESS_EXPLAIN}
        className={cn(
          'inline-flex items-center gap-1 rounded bg-surf-1 px-1.5 py-0.5 text-label text-ink-3',
          className,
        )}
        testId="unit-correctness-absent"
      >
        <Scale className="h-3 w-3" aria-hidden />
        {REFERENCE_STATE_LABEL[refState]}
      </InfoTip>
    )
  }

  const ordered = orderLedger(ledger)

  return (
    <span className={cn('inline-flex flex-col items-start gap-1', className)}>
      <span className="inline-flex items-center gap-1">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="inline-flex items-center gap-1 rounded bg-surf-1 px-1.5 py-0.5 text-label text-accent-info hover:underline"
          data-testid="unit-correctness-badge"
          data-analyst={row.analyst_id}
        >
          {open ? (
            <ChevronDown className="h-3 w-3" aria-hidden />
          ) : (
            <ChevronRight className="h-3 w-3" aria-hidden />
          )}
          <Scale className="h-3 w-3" aria-hidden />
          {/* Composed server-side; rendered verbatim, never reformatted. */}
          {row.badge}
        </button>
        <InfoTip
          text={CORRECTNESS_EXPLAIN}
          className="text-label text-ink-3"
          testId="unit-correctness-explain"
        >
          ?
        </InfoTip>
      </span>

      {open && (
        <div
          className="mt-1 w-full space-y-2"
          data-testid="unit-correctness-ledger"
        >
          <div className="text-label uppercase tracking-wider text-ink-3">
            {row.n_claims} claim{row.n_claims === 1 ? '' : 's'} · graded against{' '}
            {row.reference?.builder ?? 'a reference'}
            {row.reference?.window_start && row.reference?.window_end
              ? ` (${row.reference.window_start.slice(0, 10)} → ${row.reference.window_end.slice(0, 10)})`
              : ''}
            {(row.reference?.thin_dimensions?.length ?? 0) > 0 && (
              <span data-testid="correctness-thin-dimensions">
                {' '}· thin: {row.reference.thin_dimensions.join(', ')}
              </span>
            )}
          </div>
          {ledger === null ? (
            <div className="text-xs text-ink-3">loading the ledger…</div>
          ) : ordered.length === 0 ? (
            <div className="text-xs text-ink-3" data-testid="correctness-no-claims">
              No per-claim ledger was recorded for this number.
            </div>
          ) : (
            <ul className="space-y-1">
              {ordered.map((c) => (
                <ClaimRow key={c.id} claim={c} />
              ))}
            </ul>
          )}
        </div>
      )}
    </span>
  )
}
