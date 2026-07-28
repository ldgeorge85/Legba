import { beforeEach, describe, expect, it } from 'vitest'
import {
  addWatch,
  circleRing,
  haversineKm,
  isNear,
  loadWatchLocations,
  makeWatch,
  nearbyCountByWatch,
  nearestWatch,
  persistWatchLocations,
  pointsNearWatches,
  removeWatch,
  setWatchRadius,
  type WatchLocation,
} from './watchLocations'

function watch(partial: Partial<WatchLocation>): WatchLocation {
  return {
    id: 'w1',
    label: 'test',
    lat: 0,
    lon: 0,
    radiusKm: 250,
    createdAt: '2026-07-27T00:00:00Z',
    ...partial,
  }
}

describe('watchLocations — haversine', () => {
  it('is ~0 for identical points', () => {
    expect(haversineKm({ lat: 40, lon: -74 }, { lat: 40, lon: -74 })).toBeCloseTo(0, 6)
  })

  it('matches a known great-circle distance (London↔Paris ≈ 344km)', () => {
    const km = haversineKm({ lat: 51.5074, lon: -0.1278 }, { lat: 48.8566, lon: 2.3522 })
    expect(km).toBeGreaterThan(330)
    expect(km).toBeLessThan(360)
  })
})

describe('watchLocations — proximity', () => {
  const baghdad = { lat: 33.3, lon: 44.4 }
  const w = watch({ id: 'iq', lat: 33.3, lon: 44.4, radiusKm: 300 })

  it('isNear respects the radius', () => {
    expect(isNear(w, baghdad)).toBe(true)
    // ~900km away (Tehran) — outside a 300km radius
    expect(isNear(w, { lat: 35.7, lon: 51.4 })).toBe(false)
  })

  it('nearestWatch returns the closest covering watch, else null', () => {
    // `near` centre is ~8km from baghdad; `far` centre is ~500km away but its
    // huge radius still covers baghdad — the closer CENTRE must win.
    const near = watch({ id: 'near', lat: 33.35, lon: 44.45, radiusKm: 500 })
    const far = watch({ id: 'far', lat: 30.0, lon: 40.0, radiusKm: 5000 })
    const res = nearestWatch(baghdad, [far, near])
    expect(res?.watch.id).toBe('near')
    expect(res && res.km < 20).toBe(true)
    // a point outside every radius resolves to null
    expect(nearestWatch({ lat: -80, lon: 0 }, [near])).toBeNull()
  })

  it('pointsNearWatches returns the id-set brushed onto proximate signals', () => {
    const pts = [
      { id: 'a', lat: 33.3, lon: 44.4 }, // in
      { id: 'b', lat: 0, lon: 0 }, // out
      { id: 'c', lat: 33.35, lon: 44.45 }, // in
    ]
    const near = pointsNearWatches([w], pts)
    expect([...near].sort()).toEqual(['a', 'c'])
    // no watches → empty set (never brushes)
    expect(pointsNearWatches([], pts).size).toBe(0)
  })

  it('nearbyCountByWatch tallies proximate points per watch', () => {
    const wa = watch({ id: 'a', lat: 0, lon: 0, radiusKm: 300 })
    const wb = watch({ id: 'b', lat: 50, lon: 50, radiusKm: 300 })
    const pts = [
      { lat: 0.5, lon: 0.5 },
      { lat: 1, lon: 1 },
      { lat: 50.1, lon: 50.1 },
    ]
    expect(nearbyCountByWatch([wa, wb], pts)).toEqual({ a: 2, b: 1 })
  })
})

describe('watchLocations — circleRing', () => {
  it('is a closed ring whose vertices sit ~radius from the centre', () => {
    const center = { lat: 20, lon: 10 }
    const ring = circleRing(center, 200, 32)
    expect(ring.length).toBe(33) // steps + 1
    expect(ring[0]).toEqual(ring[ring.length - 1]) // closed
    for (const [lon, lat] of ring) {
      const km = haversineKm(center, { lat, lon })
      expect(km).toBeGreaterThan(150)
      expect(km).toBeLessThan(260)
    }
  })
})

describe('watchLocations — list ops', () => {
  it('addWatch replaces by id and removeWatch drops by id', () => {
    let list: WatchLocation[] = []
    list = addWatch(list, watch({ id: 'x' }))
    list = addWatch(list, watch({ id: 'x', label: 'updated' }))
    expect(list).toHaveLength(1)
    expect(list[0].label).toBe('updated')
    list = removeWatch(list, 'x')
    expect(list).toHaveLength(0)
  })

  it('setWatchRadius updates + clamps to [1,5000]', () => {
    const list = [watch({ id: 'x', radiusKm: 250 })]
    expect(setWatchRadius(list, 'x', 500)[0].radiusKm).toBe(500)
    expect(setWatchRadius(list, 'x', 99999)[0].radiusKm).toBe(5000)
    expect(setWatchRadius(list, 'x', 0)[0].radiusKm).toBe(1)
  })

  it('makeWatch mints a valid watch, defaulting the label to coords', () => {
    const w = makeWatch('', 12.34, 56.78, 400)
    expect(w.label).toBe('12.34, 56.78')
    expect(w.radiusKm).toBe(400)
    expect(w.id).toMatch(/^w_/)
  })
})

describe('watchLocations — persistence', () => {
  beforeEach(() => localStorage.clear())

  it('round-trips through localStorage and drops malformed rows', () => {
    const w = makeWatch('Baghdad', 33.3, 44.4)
    persistWatchLocations([w])
    expect(loadWatchLocations()).toEqual([w])

    localStorage.setItem(
      'legba.map.watch_locations',
      JSON.stringify([w, { id: 'bad', lat: 'nope' }]),
    )
    expect(loadWatchLocations()).toEqual([w]) // malformed dropped
  })

  it('returns [] when nothing is stored', () => {
    expect(loadWatchLocations()).toEqual([])
  })
})
