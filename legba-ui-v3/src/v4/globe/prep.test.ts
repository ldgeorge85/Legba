import { describe, expect, it } from 'vitest'
import { prepare, ringStep, seriesStatus, slimWorld, svgCountryPaths, washStep, type PrepInput, type PrepSeries, type World } from './prep'
import { GLOBE_CAPS, GLOBE_LAYERS, defaultGlobeLayers, globeLayersFromWorld } from './catalog'
import { subjectsFromRoster } from './data'

const sq = (x: number, y: number, d = 4) => [[[x, y], [x + d, y], [x + d, y + d], [x, y + d], [x, y]]]

const RAW = {
  type: 'FeatureCollection',
  features: [
    { type: 'Feature', geometry: { type: 'Polygon', coordinates: sq(40, 10) }, properties: { ISO_A2: 'YE', NAME: 'Yemen', LABEL_X: 42, LABEL_Y: 12, MIN_LABEL: 3 } },
    { type: 'Feature', geometry: { type: 'Polygon', coordinates: sq(50, 20) }, properties: { ISO_A2: '-99', ISO_A2_EH: 'OM', NAME: 'Oman', LABEL_X: 52, LABEL_Y: 22, MIN_LABEL: 4 } },
    { type: 'Feature', geometry: { type: 'Polygon', coordinates: sq(0, 0) }, properties: { ISO_A2: 'GH', NAME: 'Ghana', LABEL_X: 2, LABEL_Y: 2, MIN_LABEL: 4 } },
  ],
}
const WORLD: World = slimWorld(RAW)
const W = { startMs: Date.parse('2026-11-13T00:00:00Z'), endMs: Date.parse('2026-11-14T00:00:00Z') }
const at = (iso: string) => Date.parse(iso)

function input(over: Partial<PrepInput> = {}): PrepInput {
  return {
    window: W,
    nowMs: at('2026-11-14T00:00:00Z'),
    subjects: [
      { id: 's_ye', name: 'Yemen', geo: ['YE'], hasSources: true },
      { id: 's_om', name: 'Oman', geo: ['om'], hasSources: true },
      { id: 's_gh', name: 'Ghana', geo: ['GH'], hasSources: false },
      { id: 's_none', name: 'Nowhere', geo: [], hasSources: true },
    ],
    changes: [
      { id: 'c1', targetId: 's_ye', atMs: at('2026-11-13T05:00:00Z') },
      { id: 'c2', targetId: 's_ye', atMs: at('2026-11-13T06:00:00Z') },
      { id: 'c3', targetId: 's_ye', atMs: at('2026-11-13T07:00:00Z') },
      { id: 'c4', targetId: 's_om', atMs: at('2026-11-10T07:00:00Z') }, // outside the window
      { id: 'c5', targetId: null, atMs: at('2026-11-13T07:00:00Z') },
    ],
    reads: [{ targetId: 's_ye', atMs: at('2026-11-13T03:00:00Z') }],
    points: null,
    incidents: null,
    forecasts: null,
    series: null,
    scopeSubject: null,
    ...over,
  }
}

describe('the globe prep', () => {
  it('slims the outline and reads ISO_A2_EH where Natural Earth says -99', () => {
    expect(WORLD.features.map((f) => f.properties.iso)).toEqual(['YE', 'OM', 'GH'])
  })

  it('washes a subject by its changes in the window, and hatches what was not read or not covered', () => {
    const out = prepare(input(), WORLD)
    expect(out.wash.features.map((f) => [f.properties.iso, f.properties.step, f.properties.count])).toEqual([['YE', 2, 3]])
    expect(out.counts.changes).toBe(3) // the change with no subject and the one outside the window are not counted
    const hatch = Object.fromEntries(out.hatch.features.map((f) => [f.properties.iso, f.properties.kind]))
    expect(hatch).toEqual({ OM: 'dense', GH: 'sparse' })
    expect(out.countries.YE.subjects).toEqual([{ id: 's_ye', name: 'Yemen', changes: 3 }])
    expect(out.subjectCenters.s_ye.center).toEqual([42, 12])
    expect(out.subjectCenters.s_none).toBeUndefined()
  })

  it('hatches "not covered" only where the roster SAYS no source (roll 86: the summary roster said nothing, every place was hatched)', () => {
    const subjects = subjectsFromRoster([
      { descriptor_id: 's_ye', body: { scope: { geo: ['YE'] }, declared_sources: 3 } },
      { descriptor_id: 's_om', body: { scope: { geo: ['OM'] } } },
      { descriptor_id: 's_gh', body: { scope: { geo: ['GH'] }, declared_sources: 0 } },
    ])
    expect(subjects.map((s) => [s.id, s.hasSources])).toEqual([['s_ye', true], ['s_om', null], ['s_gh', false]])
    const out = prepare(input({ subjects, reads: null }), WORLD)
    const hatch = Object.fromEntries(out.hatch.features.map((f) => [f.properties.iso, f.properties.kind]))
    expect(hatch).toEqual({ GH: 'sparse' })
    // the whole body's list still answers
    expect(subjectsFromRoster([{ descriptor_id: 'x', body: { sources: [{ id: 'a' }] } }])[0].hasSources).toBe(true)
  })

  it('a layer that has not been read draws nothing and claims no zero', () => {
    const out = prepare(input({ changes: null, reads: null }), WORLD)
    expect(out.wash.features).toEqual([])
    expect(out.counts.changes).toBeNull()
    // only the subject that declares no source is hatched (a declaration, not a read)
    expect(out.hatch.features.map((f) => f.properties.iso)).toEqual(['GH'])
  })

  it('caps events after the window cut, newest first, and says what it cut from', () => {
    const points = Array.from({ length: GLOBE_CAPS.events + 50 }, (_, i) => ({
      id: `e${i}`, kind: 'event' as const, lat: 10, lon: 40, ts: W.startMs + i * 1000, title: 't', severity: null, lifecycle: 'active',
    }))
    points.push({ id: 'old', kind: 'event', lat: 10, lon: 40, ts: W.startMs - 1, title: 't', severity: null, lifecycle: 'active' })
    const out = prepare(input({ points }), WORLD)
    expect(out.counts.pointsTotal).toBe(GLOBE_CAPS.events + 50)
    expect(out.counts.pointsDrawn).toBe(GLOBE_CAPS.events)
    expect(out.points.features[0].properties.id).toBe(`e${GLOBE_CAPS.events + 49}`)
  })

  it('places forecasts at their first placed subject and counts the unplaced', () => {
    const forecasts = [
      { id: 'f1', question: 'q', startMs: at('2026-11-01T00:00:00Z'), dueMs: at('2026-11-15T00:00:00Z'), subjectKeys: ['s_none', 's_ye'] },
      { id: 'f2', question: 'q', startMs: at('2026-11-01T00:00:00Z'), dueMs: at('2026-12-31T00:00:00Z'), subjectKeys: ['lane_x'] },
    ]
    const out = prepare(input({ forecasts }), WORLD)
    expect(out.forecasts.features.map((f) => [f.properties.id, f.properties.subject, f.properties.heavy])).toEqual([['f1', 's_ye', 1]])
    expect(out.counts.forecastsUnplaced).toBe(1)
    expect(out.forecasts.features[0].properties.step).toBe(ringStep(forecasts[0].startMs, forecasts[0].dueMs, at('2026-11-14T00:00:00Z')))
  })

  it('places series by the gazetteer, labels at most the cap, and lists the unplaced', () => {
    const series: PrepSeries[] = Array.from({ length: 28 }, (_, i) => ({ subject: `chokepoint${i + 1}`, name: null, latest: 10, pct: 100 - i * 3, revised: false, day: '2026-11-08', values: [] }))
    series.push({ subject: 'nowhere9', name: 'Somewhere', latest: 1, pct: 50, revised: false, day: '2026-11-08', values: [] })
    const out = prepare(input({ series }), WORLD)
    expect(out.seriesGlyphs).toHaveLength(GLOBE_CAPS.seriesGlyphs)
    expect(out.seriesGlyphs.length + out.seriesDots.features.length).toBe(28)
    expect(out.seriesGlyphs[0].pct).toBe(100 - 27 * 3) // the largest move first
    expect(out.counts.seriesUnplaced).toEqual(['Somewhere'])
  })

  it('cuts every layer to the inset subject', () => {
    const out = prepare(input({ scopeSubject: 's_ye', incidents: [
      { id: 'i1', lat: 12, lon: 43, ts: W.startMs + 5, recordedMs: W.startMs, title: 'x', place: null, coasts: ['YE'], sourceId: null },
      { id: 'i2', lat: -40, lon: -60, ts: W.startMs + 5, recordedMs: W.startMs, title: 'x', place: null, coasts: [], sourceId: null },
    ] }), WORLD)
    expect(Object.keys(out.countries)).toEqual(['YE'])
    expect(out.incidents.features.map((f) => f.properties.id)).toEqual(['i1'])
    expect(out.home).toEqual({ center: [42, 12], zoom: 3 })
  })

  it('the encodings', () => {
    expect([0, 1, 2, 3, 5, 6, 40].map(washStep)).toEqual([0, 1, 1, 2, 2, 3, 3])
    expect([null, 40, 70, 80, 100, 120].map(seriesStatus)).toEqual(['none', 'critical', 'warn', 'none', 'good', 'none'])
    expect(ringStep(0, 100, 50)).toBe(6)
    expect(ringStep(0, 100, 500)).toBe(12)
    expect(svgCountryPaths(WORLD, 360, 144)[0].d.startsWith('M220.0')).toBe(true)
  })
})

describe('the globe catalogue', () => {
  it('lists at most seven rows, at most four on at rest, each with a source and a legend or a route it needs', () => {
    expect(GLOBE_LAYERS.length).toBeLessThanOrEqual(7)
    expect(Object.values(defaultGlobeLayers()).filter(Boolean).length).toBeLessThanOrEqual(4)
    for (const l of GLOBE_LAYERS) {
      expect(l.source.length).toBeGreaterThan(0)
      expect(l.legend.length > 0 || !!l.unavailable).toBe(true)
    }
  })

  it('follows a Consult map step through the world layers it is bound to', () => {
    const rest = defaultGlobeLayers()
    const next = globeLayersFromWorld({ signals: true, findings: false, situations: false, entities: false, events: true }, rest)
    expect(next.events).toBe(true)
    expect(next.at_sea).toBe(true)
    expect(next.change).toBe(false)
    expect(next.series).toBe(rest.series) // not drivable: unchanged
    expect(next.drift).toBe(false)
  })
})
