/**
 * THE FRAME'S GRID (U-6) — the numbers the owner's screenshots were missing:
 * a type step on big screens, a bounded reading measure, panels that take the
 * rest, and columns that appear only when the width affords them.
 */
import { describe, expect, it } from 'vitest'
import {
  ASK_BAR_REM,
  CHAT_DEFAULT_SHARE,
  CHAT_MAX_SHARE,
  centreMeasurePx,
  clampChatShare,
  columnsFor,
  frameGrid,
  placeRegions,
  RAIL_OPEN_FROM,
  rootPxFor,
} from './frameGrid'

describe('the grid at the four reference widths', () => {
  it('1366: 16 px root, rail shut, a 576 px centre, one panel column', () => {
    expect(frameGrid(1366)).toEqual({ rootPx: 16, railPx: 48, centrePx: 576, panelsPx: 742, columns: 1, minColumnPx: 480 })
  })
  it('1920: 16 px root, rail open, a 768 px centre, one panel column', () => {
    expect(frameGrid(1920)).toEqual({ rootPx: 16, railPx: 240, centrePx: 768, panelsPx: 912, columns: 1, minColumnPx: 480 })
  })
  it('2560: 18 px root, an 864 px centre, two panel columns', () => {
    expect(frameGrid(2560)).toEqual({ rootPx: 18, railPx: 270, centrePx: 864, panelsPx: 1426, columns: 2, minColumnPx: 540 })
  })
  it('3840: 24 px root (1.5× the text), a 1152 px centre, three panel columns', () => {
    expect(frameGrid(3840)).toEqual({ rootPx: 24, railPx: 360, centrePx: 1152, panelsPx: 2328, columns: 3, minColumnPx: 720 })
  })
})

describe('the rules behind the numbers', () => {
  it('the rail starts shut below 1680 px of width and open from it', () => {
    expect(RAIL_OPEN_FROM).toBe(1680)
    expect(frameGrid(1366).railPx).toBe(48)
    expect(frameGrid(1600).railPx).toBe(48)
    expect(frameGrid(1679).railPx).toBe(48)
    expect(frameGrid(1680).railPx).toBe(240)
  })

  it('the type steps up at 2200 and 3000 px of width', () => {
    expect([1366, 2199, 2200, 2999, 3000, 5120].map(rootPxFor)).toEqual([16, 16, 18, 18, 24, 24])
  })

  it('the centre grows modestly and never runs unbounded (36–48 rem)', () => {
    for (const w of [800, 1366, 1600, 1920, 2560, 3840, 7680]) {
      const root = rootPxFor(w)
      const c = centreMeasurePx(w)
      expect(c, String(w)).toBeGreaterThanOrEqual(36 * root)
      expect(c, String(w)).toBeLessThanOrEqual(48 * root)
    }
  })

  it('no dead band: rail + centre + panels is the whole width', () => {
    for (const w of [1366, 1920, 2560, 3840]) {
      const g = frameGrid(w)
      expect(g.railPx + g.centrePx + g.panelsPx).toBe(w)
      // and every column is at least the minimum width
      expect(g.panelsPx / g.columns).toBeGreaterThanOrEqual(g.minColumnPx)
    }
  })

  it('columns: one below twice the minimum, never more than three', () => {
    expect(columnsFor(959, 16)).toBe(1)
    expect(columnsFor(960, 16)).toBe(2)
    expect(columnsFor(1440, 16)).toBe(3)
    expect(columnsFor(9999, 16)).toBe(3)
    expect(columnsFor(10, 16)).toBe(1)
  })
})

describe('placing a page\'s regions into columns', () => {
  const two = [
    { id: 'a', weight: 2 },
    { id: 'b', weight: 1 },
  ]
  it('one column: the lead on top with 60 % of the height, the rest under it', () => {
    expect(placeRegions(two, 1)).toEqual([
      { id: 'a', column: 0, row: 0, widthShare: 1, heightShare: 0.6 },
      { id: 'b', column: 0, row: 1, widthShare: 1, heightShare: 0.4 },
    ])
  })
  it('two or three columns: one region each, widths by weight — never an empty column', () => {
    const p2 = placeRegions(two, 2)
    expect(p2.map((p) => [p.id, p.column, p.row])).toEqual([
      ['a', 0, 0],
      ['b', 1, 0],
    ])
    expect(p2[0].widthShare).toBeCloseTo(2 / 3)
    expect(placeRegions(two, 3)).toEqual(p2)
  })
})

describe('the chat divider', () => {
  it('defaults to about half; ranges from the ask bar to nearly the full height', () => {
    expect(CHAT_DEFAULT_SHARE).toBe(0.5)
    expect(clampChatShare(0.99, 800)).toBe(CHAT_MAX_SHARE)
    expect(clampChatShare(0, 800)).toBeCloseTo((ASK_BAR_REM * 16) / 800)
    expect(clampChatShare(Number.NaN, 800)).toBe(CHAT_DEFAULT_SHARE)
  })
})
