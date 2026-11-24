/**
 * subjectTitle — a bound view's tab title (X4/D4; moved here from the retired
 * `lib/subjectPlacement.ts` when the U-2 shell replaced its column planner
 * with "an open replaces content in its home region").
 */
import { humanizeId } from '@/lib/deskNames'
import { SUBJECT_READ_KIND } from '@/lib/subjectNoun'

/**
 * The subject page is titled by the subject's NAME (`Brazil`, from what the
 * subject declares — `humanizeId` reads the roster and falls back to a
 * humanized id, never a raw one); every other bound read keeps its panel title
 * and gains the name (`Map · Brazil`); an analyst-axis view keeps the id (a
 * unit's id is its name).
 */
export function boundTileTitle(
  kind: string,
  defaultTitle: string,
  recordId: string,
  axis: 'target' | 'analyst',
): string {
  if (axis !== 'target') return `${defaultTitle} · ${recordId}`
  const name = humanizeId(recordId)
  return kind === SUBJECT_READ_KIND ? name : `${defaultTitle} · ${name}`
}
