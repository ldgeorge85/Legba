import { describe, expect, it } from 'vitest'
import { isTileVisible, rectChanged, MIN_TILE_PX, type TileRect } from './tileOverlayGeometry'

const R = (w: number, h: number): TileRect => ({ left: 10, top: 20, width: w, height: h })

describe('isTileVisible', () => {
  const base = {
    connected: true,
    hasOffsetParent: true,
    rect: R(800, 600),
    dockviewVisible: undefined as boolean | undefined,
    forceHidden: false,
  }

  it('shows a laid-out, connected, on-tab tile', () => {
    expect(isTileVisible(base)).toBe(true)
  })

  it('hides a detached anchor', () => {
    expect(isTileVisible({ ...base, connected: false })).toBe(false)
  })

  it('hides a display:none tile (no offsetParent → Dockview tabbed-behind)', () => {
    expect(isTileVisible({ ...base, hasOffsetParent: false })).toBe(false)
  })

  it('hides a collapsed 0×0 tile', () => {
    expect(isTileVisible({ ...base, rect: R(0, 0) })).toBe(false)
    expect(isTileVisible({ ...base, rect: R(800, 1) })).toBe(false)
  })

  it('respects the dockview visibility signal when present', () => {
    expect(isTileVisible({ ...base, dockviewVisible: false })).toBe(false)
    expect(isTileVisible({ ...base, dockviewVisible: true })).toBe(true)
  })

  it('ignores an absent (undefined) dockview signal', () => {
    expect(isTileVisible({ ...base, dockviewVisible: undefined })).toBe(true)
  })

  it('honours a panel-level force-hide even when everything else says visible', () => {
    expect(isTileVisible({ ...base, forceHidden: true })).toBe(false)
  })

  it('treats exactly MIN_TILE_PX as visible', () => {
    expect(isTileVisible({ ...base, rect: R(MIN_TILE_PX, MIN_TILE_PX) })).toBe(true)
  })
})

describe('rectChanged', () => {
  it('is false for identical rects', () => {
    expect(rectChanged(R(800, 600), R(800, 600))).toBe(false)
  })

  it('ignores sub-epsilon jitter', () => {
    expect(rectChanged({ left: 10, top: 20, width: 800, height: 600 }, { left: 10.2, top: 20.1, width: 800.3, height: 600 })).toBe(false)
  })

  it('detects a real move', () => {
    expect(rectChanged(R(800, 600), { left: 40, top: 20, width: 800, height: 600 })).toBe(true)
  })

  it('detects a real resize', () => {
    expect(rectChanged(R(800, 600), R(801, 600))).toBe(true)
    expect(rectChanged(R(800, 600), R(800, 640))).toBe(true)
  })
})
