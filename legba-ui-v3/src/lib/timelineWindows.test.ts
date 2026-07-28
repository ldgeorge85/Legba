/**
 * Tests for timelineWindows (P4-4) — the `system.timeline` validity-window
 * panel's pure shaping: open-vs-closed windows, lanes, domain, zoom/pan,
 * visibility, x-projection, and supersession-chain edges. No DOM.
 */
import { describe, it, expect } from 'vitest'
import {
  LANE,
  MIN_ZOOM_MS,
  isVisible,
  itemColor,
  kindTallies,
  panDomain,
  shapeItem,
  shapeItems,
  supersessionEdges,
  timeDomain,
  visibleItems,
  xOf,
  zoomDomain,
  type TimelineItem,
} from './timelineWindows'

const NOW = Date.parse('2026-07-24T12:00:00Z')
const DAY = 86_400_000

function item(partial: Partial<TimelineItem> & Pick<TimelineItem, 'id' | 'kind'>): TimelineItem {
  return {
    label: partial.label ?? partial.id,
    start: partial.start ?? new Date(NOW - DAY).toISOString(),
    end: partial.end ?? null,
    ...partial,
  }
}

describe('shapeItem', () => {
  it('resolves an OPEN window (end=null) to now + flags open', () => {
    const s = shapeItem(
      item({ id: 'a', kind: 'fact', start: new Date(NOW - 2 * DAY).toISOString(), end: null }),
      NOW,
    )!
    expect(s.open).toBe(true)
    expect(s.endMs).toBe(NOW)
    expect(s.startMs).toBe(NOW - 2 * DAY)
    expect(s.lane).toBe(LANE.fact)
  })

  it('carries a CLOSED window end verbatim', () => {
    const end = new Date(NOW - DAY).toISOString()
    const s = shapeItem(item({ id: 'b', kind: 'finding', start: new Date(NOW - 3 * DAY).toISOString(), end }), NOW)!
    expect(s.open).toBe(false)
    expect(s.endMs).toBe(Date.parse(end))
  })

  it('drops a row whose start is unparseable (never guessed onto the axis)', () => {
    expect(shapeItem(item({ id: 'x', kind: 'fact', start: 'not-a-date' }), NOW)).toBeNull()
  })

  it('clamps a pathological end<start to a zero-width bar', () => {
    const s = shapeItem(
      item({ id: 'c', kind: 'fact', start: new Date(NOW).toISOString(), end: new Date(NOW - DAY).toISOString() }),
      NOW,
    )!
    expect(s.endMs).toBe(s.startMs)
  })
})

describe('shapeItems', () => {
  it('shapes valid rows and drops unplaceable ones, order preserved', () => {
    const out = shapeItems(
      [
        item({ id: 'a', kind: 'situation' }),
        item({ id: 'bad', kind: 'fact', start: 'nope' }),
        item({ id: 'b', kind: 'finding' }),
      ],
      NOW,
    )
    expect(out.map((s) => s.id)).toEqual(['a', 'b'])
  })
})

describe('itemColor', () => {
  it('colors a finding by severity when known', () => {
    const s = shapeItem(item({ id: 'f', kind: 'finding', severity: 'critical' }), NOW)!
    expect(itemColor(s)).toBe('#ef4444')
  })
  it('falls back to the kind color for a finding with no/unknown severity', () => {
    const s = shapeItem(item({ id: 'f', kind: 'finding', severity: null }), NOW)!
    expect(itemColor(s)).toBe('#fbbf24')
  })
  it('uses the kind color for facts + situations', () => {
    expect(itemColor(shapeItem(item({ id: 'x', kind: 'fact' }), NOW)!)).toBe('#34d399')
    expect(itemColor(shapeItem(item({ id: 'y', kind: 'situation' }), NOW)!)).toBe('#fb7185')
  })
})

describe('timeDomain', () => {
  it('is undefined for no items', () => {
    expect(timeDomain([])).toBeUndefined()
  })
  it('spans min-start to max-end with padding', () => {
    const shaped = shapeItems(
      [
        item({ id: 'a', kind: 'fact', start: new Date(NOW - 5 * DAY).toISOString(), end: new Date(NOW - 4 * DAY).toISOString() }),
        item({ id: 'b', kind: 'finding', start: new Date(NOW - 2 * DAY).toISOString(), end: null }),
      ],
      NOW,
    )
    const d = timeDomain(shaped)!
    expect(d[0]).toBeLessThan(NOW - 5 * DAY)
    expect(d[1]).toBeGreaterThan(NOW)
  })
})

describe('zoomDomain', () => {
  // Domains are ms-scale (> MIN_ZOOM_MS) so the clamp doesn't apply.
  it('zooms in about the center (factor<1) keeping the midpoint', () => {
    const [lo, hi] = zoomDomain([0, 100_000], 0.5, 0.5)
    expect(lo).toBe(25_000)
    expect(hi).toBe(75_000)
  })
  it('zooms about the left edge with pivot 0', () => {
    const [lo, hi] = zoomDomain([0, 100_000], 0.5, 0)
    expect(lo).toBe(0)
    expect(hi).toBe(50_000)
  })
  it('never collapses below MIN_ZOOM_MS', () => {
    const [lo, hi] = zoomDomain([0, 10_000], 0.0001, 0.5)
    expect(hi - lo).toBe(MIN_ZOOM_MS)
  })
})

describe('panDomain', () => {
  it('shifts by delta preserving width', () => {
    expect(panDomain([10, 30], 5)).toEqual([15, 35])
  })
})

describe('isVisible / visibleItems', () => {
  const shaped = shapeItems(
    [
      item({ id: 'inside', kind: 'fact', start: new Date(NOW - 2 * DAY).toISOString(), end: new Date(NOW - DAY).toISOString() }),
      item({ id: 'before', kind: 'fact', start: new Date(NOW - 10 * DAY).toISOString(), end: new Date(NOW - 9 * DAY).toISOString() }),
      item({ id: 'overlap', kind: 'fact', start: new Date(NOW - 4 * DAY).toISOString(), end: new Date(NOW - DAY).toISOString() }),
    ],
    NOW,
  )
  const domain: [number, number] = [NOW - 3 * DAY, NOW]
  it('keeps items overlapping the domain, drops those entirely before it', () => {
    expect(isVisible(shaped[1], domain)).toBe(false)
    const ids = visibleItems(shaped, domain).map((s) => s.id)
    expect(ids).toContain('inside')
    expect(ids).toContain('overlap')
    expect(ids).not.toContain('before')
  })
})

describe('xOf', () => {
  it('maps domain edges to plot edges and clamps outside', () => {
    expect(xOf(0, [0, 100], 200)).toBe(0)
    expect(xOf(100, [0, 100], 200)).toBe(200)
    expect(xOf(50, [0, 100], 200)).toBe(100)
    expect(xOf(-50, [0, 100], 200)).toBe(0) // clamped left
    expect(xOf(150, [0, 100], 200)).toBe(200) // clamped right
  })
})

describe('supersessionEdges', () => {
  it('links a superseded item to its replacement when both are in view', () => {
    const shaped = shapeItems(
      [
        item({ id: 'old', kind: 'finding', superseded_by: 'new' }),
        item({ id: 'new', kind: 'finding' }),
      ],
      NOW,
    )
    const edges = supersessionEdges(shaped)
    expect(edges).toHaveLength(1)
    expect(edges[0].from.id).toBe('old')
    expect(edges[0].to.id).toBe('new')
  })
  it('draws no floating edge for a dangling pointer (replacement off-window)', () => {
    const shaped = shapeItems([item({ id: 'old', kind: 'finding', superseded_by: 'gone' })], NOW)
    expect(supersessionEdges(shaped)).toHaveLength(0)
  })
})

describe('kindTallies', () => {
  it('folds shown counts with the wire totals + truncation, in lane order', () => {
    const shaped = shapeItems(
      [
        item({ id: 's1', kind: 'situation' }),
        item({ id: 'f1', kind: 'finding' }),
        item({ id: 'f2', kind: 'finding' }),
      ],
      NOW,
    )
    const tallies = kindTallies(
      shaped,
      { situation: 1, finding: 5, fact: 0 },
      { situation: false, finding: true, fact: false },
    )
    expect(tallies.map((t) => t.kind)).toEqual(['situation', 'finding', 'fact'])
    const finding = tallies.find((t) => t.kind === 'finding')!
    expect(finding.shown).toBe(2)
    expect(finding.total).toBe(5)
    expect(finding.truncated).toBe(true)
  })
})
