/**
 * The subject geometry contract (UI-B4/map; UI-D1 §2.4; C-1a §2, §6.5).
 *
 * Pinned here: a subject's place comes from what its DESCRIPTOR declares —
 * never from an ISO code parsed out of its id; `fit_ref` resolves through a
 * declared polygon to its bounds; the profile's `map.geometry: none` answers
 * `no_map_surface` by name; and a seed loses its map tile without losing the
 * tiles that hung off it.
 */
import { describe, expect, it } from 'vitest'
import { profileUiFrom, PROFILE_UI } from './profileUi'
import {
  cameraFor,
  fitRef,
  geometryFor,
  geometryIndexFrom,
  isMapSurfaceKind,
  mapSurfaceDeclared,
  NO_MAP_SURFACE,
  noMapSurfaceReason,
  parseDeclaredGeometry,
  placementsForProfile,
  withoutMapSurface,
} from './subjectGeometry'
import noneFixture from '@/test/profileNone.fixture.json'

const IRAN_THEATRE = [
  [44.0, 25.0],
  [63.0, 25.0],
  [63.0, 40.0],
  [44.0, 40.0],
  [44.0, 25.0],
]

const ROWS = [
  { descriptor_id: 'country_g20_us', body: { scope: { domain: 'geo', geo: ['US'] } } },
  { descriptor_id: 'thematic_iran_war', body: { scope: { domain: 'thematic', geo: ['IR', 'IL', 'IQ'] } } },
  {
    descriptor_id: 'theatre_gulf',
    body: { scope: { domain: 'thematic', geo: [], geometry: { type: 'Polygon', coordinates: [IRAN_THEATRE] } } },
  },
  {
    descriptor_id: 'port_rotterdam',
    body: { scope: { domain: 'entity', geo: ['NL'], geometry: { type: 'Point', coordinates: [4.4, 51.9] } } },
  },
  { descriptor_id: 'regime_ofac_russia', body: { scope: { domain: 'thematic', geo: [] } } },
  // Declares a code the gazetteer cannot place (alpha-3).
  { descriptor_id: 'odd_alpha3', body: { scope: { domain: 'geo', geo: ['USA'] } } },
  { not_a_row: true },
]

const INDEX = geometryIndexFrom(ROWS)
const ISO2 = PROFILE_UI // the geopolitics export: geometry iso2
const DECLARED = profileUiFrom({ ...JSON.parse(JSON.stringify(noneFixture)), map: { geometry: 'declared', title: 'Theatre' } })
const NONE = profileUiFrom(noneFixture)

describe('the profile decides whether there is a map surface', () => {
  it('the geopolitics export declares iso2; the none fixture declares none', () => {
    expect(ISO2.map.geometry).toBe('iso2')
    expect(mapSurfaceDeclared(ISO2)).toBe(true)
    expect(NONE.map.geometry).toBe('none')
    expect(mapSurfaceDeclared(NONE)).toBe(false)
  })

  it('names the refusal no_map_surface, with the profile that declared it', () => {
    expect(noMapSurfaceReason(NONE)).toBe(`${NO_MAP_SURFACE}: the sanctions_export_control profile declares map.geometry none`)
    expect(geometryFor({ kind: 'target', id: 'country_g20_us' }, INDEX, NONE)).toEqual({
      kind: 'none',
      reason: noMapSurfaceReason(NONE),
    })
  })

  it('the map surface is the survivor and the per-subject kind Table B folds into it', () => {
    expect(isMapSurfaceKind('v4.map')).toBe(true)
    expect(isMapSurfaceKind('target.map')).toBe(true)
    expect(isMapSurfaceKind('system.timeline')).toBe(false)
    expect(isMapSurfaceKind(undefined)).toBe(false)
  })
})

describe('geometryFor — what the subject DECLARES, read as data', () => {
  it('an iso2 subject resolves through its scope.geo codes', () => {
    expect(geometryFor({ kind: 'target', id: 'country_g20_us' }, INDEX, ISO2)).toEqual({
      kind: 'iso2',
      codes: ['US'],
      from: 'scope.geo',
    })
  })

  it('NEVER parses an ISO code out of an id: an unregistered country-shaped id has no geometry', () => {
    const g = geometryFor({ kind: 'target', id: 'country_g20_br' }, INDEX, ISO2)
    expect(g.kind).toBe('none')
    expect(g).toMatchObject({ reason: 'no registered descriptor declares country_g20_br' })
  })

  it('a subject that declares nothing says so — it is not placed by its name', () => {
    const g = geometryFor({ kind: 'target', id: 'regime_ofac_russia' }, INDEX, ISO2)
    expect(g).toEqual({ kind: 'none', reason: 'regime_ofac_russia declares no geometry in its scope' })
    expect(cameraFor(g)).toBeNull()
  })

  it('a declared code the gazetteer cannot place is stated, not guessed', () => {
    expect(geometryFor({ kind: 'target', id: 'odd_alpha3' }, INDEX, ISO2)).toMatchObject({
      kind: 'none',
      reason: expect.stringContaining('declares USA'),
    })
  })

  it('before the roster is read there is no geometry yet — and a record is not a subject', () => {
    expect(geometryFor({ kind: 'target', id: 'country_g20_us' }, null, ISO2)).toMatchObject({
      kind: 'none',
      reason: 'the subject roster has not been read yet',
    })
    expect(geometryFor({ kind: 'signal', id: 's-1' }, INDEX, ISO2).kind).toBe('none')
    expect(geometryFor(null, INDEX, ISO2).kind).toBe('none')
  })

  it('a `declared` profile reads scope.geometry first; an iso2 profile reads the codes first', () => {
    const ref = { kind: 'target', id: 'port_rotterdam' }
    expect(geometryFor(ref, INDEX, DECLARED)).toEqual({ kind: 'point', coordinates: [4.4, 51.9], from: 'scope.geometry' })
    expect(geometryFor(ref, INDEX, ISO2)).toEqual({ kind: 'iso2', codes: ['NL'], from: 'scope.geo' })
  })

  it('a malformed declared geometry is not a geometry', () => {
    expect(parseDeclaredGeometry({ type: 'Point', coordinates: [500, 0] })).toBeNull()
    expect(parseDeclaredGeometry({ type: 'Polygon', coordinates: [[[0, 0], [1, 1]]] })).toBeNull()
    expect(parseDeclaredGeometry({ type: 'LineString', coordinates: [[0, 0], [1, 1]] })).toBeNull()
    expect(parseDeclaredGeometry(null)).toBeNull()
  })
})

describe('fit_ref resolves through the declared geometry (C-1a §6.5)', () => {
  it('a declared POLYGON fits its bounds', () => {
    const { geometry, camera } = fitRef({ kind: 'target', id: 'theatre_gulf' }, INDEX, DECLARED)
    expect(geometry.kind).toBe('polygon')
    expect(camera).toEqual({ kind: 'bounds', west: 44, south: 25, east: 63, north: 40 })
  })

  it('the same polygon is the fallback under an iso2 profile when the subject declares no codes', () => {
    expect(fitRef({ kind: 'target', id: 'theatre_gulf' }, INDEX, ISO2).camera).toEqual({
      kind: 'bounds',
      west: 44,
      south: 25,
      east: 63,
      north: 40,
    })
  })

  it('a MultiPolygon fits the bounds of every ring', () => {
    const g = parseDeclaredGeometry({
      type: 'MultiPolygon',
      coordinates: [[IRAN_THEATRE], [[[-10, -10], [-5, -10], [-5, -5], [-10, -10]]]],
    })
    expect(g && cameraFor(g)).toEqual({ kind: 'bounds', west: -10, south: -10, east: 63, north: 40 })
  })

  it('one code flies to its centroid; several codes fit their bounds', () => {
    expect(fitRef({ kind: 'target', id: 'country_g20_us' }, INDEX, ISO2).camera).toMatchObject({ kind: 'center' })
    expect(fitRef({ kind: 'target', id: 'thematic_iran_war' }, INDEX, ISO2).camera).toMatchObject({ kind: 'bounds' })
  })

  it('a point flies to itself', () => {
    expect(fitRef({ kind: 'target', id: 'port_rotterdam' }, INDEX, DECLARED).camera).toEqual({
      kind: 'center',
      lon: 4.4,
      lat: 51.9,
    })
  })
})

describe('a seed without its map tile', () => {
  const SEED = [
    { kind: 'system.navigator' },
    { kind: 'system.graph_walk', position: { referencePanel: 'system.navigator', direction: 'below' } },
    { kind: 'v4.map', position: { referencePanel: 'system.graph_walk', direction: 'right' } },
    { kind: 'system.timeline', position: { referencePanel: 'v4.map', direction: 'right' } },
    { kind: 'system.consult', position: { referencePanel: 'v4.map', direction: 'within' } },
  ]

  it('drops the map and re-anchors what hung off it onto the map\'s own position', () => {
    expect(withoutMapSurface(SEED)).toEqual([
      { kind: 'system.navigator' },
      { kind: 'system.graph_walk', position: { referencePanel: 'system.navigator', direction: 'below' } },
      { kind: 'system.timeline', position: { referencePanel: 'system.graph_walk', direction: 'right' } },
      { kind: 'system.consult', position: { referencePanel: 'system.graph_walk', direction: 'right' } },
    ])
  })

  it('an anchoring map promotes its first dependent to anchor', () => {
    expect(
      withoutMapSurface([
        { kind: 'v4.map' },
        { kind: 'system.timeline', position: { referencePanel: 'v4.map', direction: 'within' } },
      ]),
    ).toEqual([{ kind: 'system.timeline' }])
  })

  it('an iso2 profile keeps every seed exactly as declared', () => {
    expect(placementsForProfile(SEED, ISO2)).toEqual(SEED)
    expect(placementsForProfile(SEED, NONE).some((p) => p.kind === 'v4.map')).toBe(false)
  })
})
