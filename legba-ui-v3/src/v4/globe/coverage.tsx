/**
 * The coverage strip under the Board's map: one row saying what the map can
 * vouch for. Of the places that hold a subject, how many had a read in the
 * window (covered), how many were looked at and could not be seen (a source
 * is declared but nothing was read), and how many are not covered at all (no
 * source declared). The figures are the Blanks layer's own (`prep.ts`), so
 * the strip and the hatching on the map can never disagree.
 *
 * A figure is printed only once the reads behind it have answered; until then
 * the strip says what it is waiting on, never a zero.
 */
import { LegendMark } from './chrome'
import type { GlobeLayerId } from './catalog'
import type { PrepOutput } from './prep'

export type MapCoverage =
  | { state: 'read'; places: number; covered: number; looked: number; uncovered: number }
  | { state: 'unread' | 'off' | 'unreadable' }

/** The strip's figures from the map's prepared layers. */
export function mapCoverage(
  out: PrepOutput | null,
  reads: { reads: unknown; subjects: unknown },
  on: Pick<Record<GlobeLayerId, boolean>, 'blanks'>,
  unreadable: Partial<Record<GlobeLayerId, true>>,
): MapCoverage {
  if (!on.blanks) return { state: 'off' }
  if (unreadable.blanks) return { state: 'unreadable' }
  if (!out || reads.reads == null || reads.subjects == null) return { state: 'unread' }
  const places = Object.keys(out.countries).length
  const looked = out.counts.blanksDense
  const uncovered = out.counts.blanksSparse
  return { state: 'read', places, covered: Math.max(0, places - looked - uncovered), looked, uncovered }
}

const WAITING: Record<Exclude<MapCoverage['state'], 'read'>, string> = {
  unread: 'reading what was covered…',
  off: 'not shown: the Blanks layer is off',
  unreadable: 'what was covered cannot be read right now',
}

export function CoverageStrip({ coverage }: { coverage: MapCoverage | null }) {
  const c = coverage ?? { state: 'unread' as const }
  return (
    <div
      className="flex h-8 shrink-0 items-center gap-4 overflow-hidden whitespace-nowrap border-t border-line bg-surf-1 px-3 text-label text-ink-2"
      data-testid="coverage-strip"
      data-state={c.state}
      title="Of the places on the map that hold a subject: read in the window; looked at and could not be seen (a source is declared, nothing was read); not covered (no source declared)."
    >
      <span className="uppercase tracking-[0.06em] text-ink-3">Coverage</span>
      {c.state !== 'read' ? (
        <span className="italic text-ink-3" data-testid="coverage-strip-waiting">
          {WAITING[c.state]}
        </span>
      ) : (
        <>
          <span data-testid="coverage-strip-covered">
            <span className="font-mono tabular-nums text-ink-1">{c.covered}</span> of {c.places} places read
          </span>
          <span className="flex items-center gap-1.5" data-testid="coverage-strip-looked">
            <LegendMark mark="hatch-dense" />
            <span className="font-mono tabular-nums text-ink-1">{c.looked}</span> looked, could not see
          </span>
          <span className="flex items-center gap-1.5" data-testid="coverage-strip-uncovered">
            <LegendMark mark="hatch-sparse" />
            <span className="font-mono tabular-nums text-ink-1">{c.uncovered}</span> not covered
          </span>
        </>
      )}
    </div>
  )
}
