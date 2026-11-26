/**
 * The point, as a region shows it (`state/scope.ts` holds the point;
 * `lib/point.ts` lists the regions that follow it).
 *
 *   A region that FOLLOWS the point narrows to its subject and says so, with
 *   a "Show all" that undoes the narrowing for this region only:
 *       Only Burkina Faso. Show all
 *   and, once shown in full, says it is not filtered, with the way back:
 *       (not filtered) Only Burkina Faso
 *
 *   A region that does not follow the point carries the plain label
 *   "(not filtered)" while a point is set (`RegionPoint`), so a fleet-wide
 *   list is never read as the subject's own.
 */
import { useState, type ReactNode } from 'react'
import { humanizeId, subjectName } from '@/lib/deskNames'
import { followsPoint } from '@/lib/point'
import { usePointSubject, type PointSubject } from '@/state/scope'

/** The point's subject as words: its label, else its declared name. */
export function pointLabel(p: PointSubject): string {
  return subjectName(p.id) ?? (p.label && p.label !== p.id ? p.label : humanizeId(p.id))
}

export interface PointFilterState {
  point: PointSubject | null
  /** True when the region narrows to the point now. */
  filtered: boolean
  showAll: () => void
  filterTo: () => void
}

/**
 * A region's narrowing to the point. "Show all" holds for the subject it was
 * pressed on: a new point narrows the region again.
 */
export function usePointFilter(): PointFilterState {
  const point = usePointSubject()
  const [showAllFor, setShowAllFor] = useState<string | null>(null)
  const filtered = !!point && showAllFor !== point.id
  return {
    point,
    filtered,
    showAll: () => setShowAllFor(point?.id ?? null),
    filterTo: () => setShowAllFor(null),
  }
}

const LINK = 'text-ink-3 underline decoration-dotted underline-offset-2 hover:text-ink-1'

/** The line under a region's head that says whether it is narrowed to the point. */
export function PointFilterLine({ state, testId }: { state: PointFilterState; testId: string }) {
  const { point, filtered } = state
  if (!point) return null
  const name = pointLabel(point)
  return (
    <p className="text-label text-ink-2" data-testid={testId} data-filtered={filtered ? '1' : '0'} data-point={point.id}>
      {filtered ? (
        <>
          Only <span className="text-ink-1">{name}</span>.{' '}
          <button type="button" className={LINK} onClick={state.showAll} data-testid={`${testId}-show-all`}>
            Show all
          </button>
        </>
      ) : (
        <>
          <span className="text-ink-3" data-testid={`${testId}-not-filtered`}>
            (not filtered)
          </span>{' '}
          <button type="button" className={LINK} onClick={state.filterTo} data-testid={`${testId}-only`}>
            Only {name}
          </button>
        </>
      )}
    </p>
  )
}

/** A region that does not follow the point: "(not filtered)" over it while a point is set. */
export function RegionPoint({ viewId, children }: { viewId: string; children: ReactNode }) {
  const point = usePointSubject()
  if (!point || followsPoint(viewId)) return <>{children}</>
  return (
    <div className="flex h-full w-full flex-col" data-point-region={viewId}>
      <p
        className="shrink-0 border-b border-line-subtle px-3 py-0.5 text-label text-ink-3"
        data-testid="region-not-filtered"
        title={`This region shows everything, not only ${pointLabel(point)}`}
      >
        (not filtered)
      </p>
      <div className="min-h-0 flex-1">{children}</div>
    </div>
  )
}
