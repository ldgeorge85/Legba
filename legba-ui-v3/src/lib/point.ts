/**
 * point — which regions follow THE POINT (the one subject the console is
 * focused on, held in `state/scope.ts`).
 *
 * A region either follows the point (it narrows to the subject, or marks it)
 * or says plainly that it is "(not filtered)" while a point is set, so the
 * reader never takes a fleet-wide list for the subject's own. The regions
 * that follow are listed here; every other region gets the label
 * (`components/PointFilter.tsx::RegionPoint`).
 */

/**
 * The views that follow the point: they narrow to the point's subject (with
 * a way to show everything) or mark it in place.
 */
export const POINT_VIEWS: ReadonlySet<string> = new Set<string>([
  // the Board's ranked list and the full daily read narrow to the subject
  'view:board_changed',
  'v4.morning_read',
  // the map marks the subject in place; the subject field (a profile with no map) likewise
  'v4.map',
  'slot:subject_field',
  // the subject page shows the point's subject
  'subject.page',
  // the Ledger's forecasts and the Watch log narrow to it, with "Show all"
  'view:forecasts',
  'view:watch_alerts',
  // the operator feed, timeline and entity list read the same scope
  'system.findings',
  'system.timeline',
  'system.entities',
  // a record and the conversation are about what they show, not a list to filter
  'system.inspector',
  'system.consult',
])

/**
 * Does this region (a view id, or a bound instance `<view>:<record>`) follow
 * the point? A bound instance is about its own record — it names it in its
 * title — so it is never labelled as unfiltered.
 */
export function followsPoint(viewId: string): boolean {
  if (POINT_VIEWS.has(viewId)) return true
  const at = viewId.indexOf(':', viewId.indexOf('.') + 1)
  if (at > 0 && !viewId.startsWith('view:') && !viewId.startsWith('slot:')) return true
  return false
}
