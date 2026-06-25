import { describe, it, expect } from 'vitest'
import {
  buildEntityGeoPoints,
  buildGeoPoints,
  countByCountry,
  entityGeo,
  extractGeo,
  type GeoEntity,
  type GeoFinding,
  type GeoSignal,
} from './geoPoints'

describe('extractGeo', () => {
  it('reads data.geo.{lat,lon} with null country when absent', () => {
    expect(extractGeo({ data: { geo: { lat: 12.3, lon: -45.6 } } })).toEqual({
      lat: 12.3,
      lon: -45.6,
      country: null,
      country_iso2: null,
    })
  })
  it('carries country + country_iso2 when present', () => {
    expect(
      extractGeo({ data: { geo: { lat: 1, lon: 2, country: 'Russia', country_iso2: 'RU' } } }),
    ).toEqual({ lat: 1, lon: 2, country: 'Russia', country_iso2: 'RU' })
  })
  it('rejects missing / non-finite / 0,0 sentinels', () => {
    expect(extractGeo(null)).toBeNull()
    expect(extractGeo({})).toBeNull()
    expect(extractGeo({ data: {} })).toBeNull()
    expect(extractGeo({ data: { geo: { lat: 'x', lon: 1 } } })).toBeNull()
    expect(extractGeo({ data: { geo: { lat: 0, lon: 0 } } })).toBeNull()
  })
})

describe('buildGeoPoints', () => {
  const signals: GeoSignal[] = [
    { id: 's1', title: 'sig with geo', data: { geo: { lat: 1, lon: 2 } } },
    { id: 's2', title: 'sig no geo', data: {} },
  ]

  it('emits a marker per geocoded signal', () => {
    const pts = buildGeoPoints(signals, [])
    expect(pts).toHaveLength(1)
    expect(pts[0]).toMatchObject({ id: 's1', kind: 'signal', lat: 1, lon: 2 })
  })

  it('uses a finding own geo and carries severity', () => {
    const findings: GeoFinding[] = [
      { id: 'f1', title: 'own geo', severity: 'critical', data: { geo: { lat: 10, lon: 20 } } },
    ]
    const pts = buildGeoPoints([], findings)
    const fp = pts.find((p) => p.id === 'f1')
    expect(fp).toMatchObject({ kind: 'finding', lat: 10, lon: 20, severity: 'critical' })
  })

  it('falls back to an upstream signal geo when finding has none', () => {
    const findings: GeoFinding[] = [
      { id: 'f2', title: 'derived geo', severity: 'high', data: {}, derived_from: ['s2', 's1'] },
    ]
    const pts = buildGeoPoints(signals, findings)
    const fp = pts.find((p) => p.id === 'f2')
    // s2 has no geo, s1 does → inherits s1's
    expect(fp).toMatchObject({ kind: 'finding', lat: 1, lon: 2, severity: 'high' })
  })

  it('drops findings with no own and no upstream geo', () => {
    const findings: GeoFinding[] = [{ id: 'f3', title: 'no geo', data: {}, derived_from: ['s2'] }]
    const pts = buildGeoPoints(signals, findings)
    expect(pts.find((p) => p.id === 'f3')).toBeUndefined()
  })

  it('carries source_id + country on signals and inherits both onto findings', () => {
    const srcSignals: GeoSignal[] = [
      {
        id: 's1',
        title: 'sig',
        source_id: 'source.aljazeera.world',
        data: { geo: { lat: 1, lon: 2, country: 'Russia', country_iso2: 'RU' } },
      },
    ]
    const findings: GeoFinding[] = [
      { id: 'f1', title: 'derived', severity: 'high', data: {}, derived_from: ['s1'] },
    ]
    const pts = buildGeoPoints(srcSignals, findings)
    expect(pts.find((p) => p.id === 's1')).toMatchObject({
      source_id: 'source.aljazeera.world',
      country: 'Russia',
      country_iso2: 'RU',
    })
    // The finding inherits the upstream signal's geo AND its source_id.
    expect(pts.find((p) => p.id === 'f1')).toMatchObject({
      kind: 'finding',
      source_id: 'source.aljazeera.world',
      country_iso2: 'RU',
    })
  })
})

describe('countByCountry', () => {
  it('buckets by iso2, splits signals/findings, sorts by total desc', () => {
    const signals: GeoSignal[] = [
      { id: 's1', title: 'a', data: { geo: { lat: 1, lon: 1, country: 'Russia', country_iso2: 'RU' } } },
      { id: 's2', title: 'b', data: { geo: { lat: 2, lon: 2, country: 'Russia', country_iso2: 'RU' } } },
      { id: 's3', title: 'c', data: { geo: { lat: 3, lon: 3, country: 'Brazil', country_iso2: 'BR' } } },
    ]
    const findings: GeoFinding[] = [
      { id: 'f1', title: 'd', data: { geo: { lat: 1, lon: 1, country: 'Russia', country_iso2: 'RU' } } },
    ]
    const counts = countByCountry(buildGeoPoints(signals, findings))
    expect(counts[0]).toMatchObject({ iso2: 'RU', name: 'Russia', signals: 2, findings: 1, total: 3 })
    expect(counts[1]).toMatchObject({ iso2: 'BR', signals: 1, findings: 0, total: 1 })
  })

  it('falls back to an UNK bucket when iso2 is missing', () => {
    const signals: GeoSignal[] = [{ id: 's1', title: 'a', data: { geo: { lat: 5, lon: 5 } } }]
    const counts = countByCountry(buildGeoPoints(signals, []))
    expect(counts).toHaveLength(1)
    expect(counts[0]).toMatchObject({ iso2: 'UNK', signals: 1, total: 1 })
  })
})

describe('entityGeo', () => {
  it('prefers the backend-resolved geo_lat/geo_lon', () => {
    const e: GeoEntity = {
      id: 'e1',
      canonical_name: 'Some City',
      entity_class: 'location',
      geo_lat: 48.85,
      geo_lon: 2.35,
      geo_country: 'France',
    }
    expect(entityGeo(e)).toMatchObject({ lat: 48.85, lon: 2.35, country: 'France' })
  })

  it('falls back to a country-name centroid for un-geocoded country entities', () => {
    // NER classed this country mention as generic `entity` with no geo.
    const e: GeoEntity = { id: 'e2', canonical_name: 'Brazil', entity_class: 'entity' }
    const g = entityGeo(e)
    expect(g).not.toBeNull()
    expect(g!.country_iso2).toBe('BR')
    expect(g!.country).toBe('Brazil')
  })

  it('returns null for a non-country entity with no geo', () => {
    expect(entityGeo({ id: 'e3', canonical_name: 'Acme Corp', entity_class: 'organization' })).toBeNull()
  })

  it('rejects the 0,0 sentinel and falls back to the name', () => {
    const e: GeoEntity = { id: 'e4', canonical_name: 'France', entity_class: 'entity', geo_lat: 0, geo_lon: 0 }
    const g = entityGeo(e)
    expect(g!.country_iso2).toBe('FR')
  })
})

describe('buildEntityGeoPoints', () => {
  it('emits a marker only for geo-resolvable entities', () => {
    const entities: GeoEntity[] = [
      { id: 'e1', canonical_name: 'Brazil', entity_class: 'entity' }, // country fallback
      { id: 'e2', canonical_name: 'Acme Corp', entity_class: 'organization' }, // dropped
      { id: 'e3', canonical_name: 'Paris', entity_class: 'location', geo_lat: 48.85, geo_lon: 2.35 },
    ]
    const pts = buildEntityGeoPoints(entities)
    expect(pts).toHaveLength(2)
    const br = pts.find((p) => p.entity_id === 'e1')!
    expect(br).toMatchObject({ kind: 'entity', country_iso2: 'BR', title: 'Brazil' })
    expect(br.id).toBe('entity:e1')
    expect(pts.find((p) => p.entity_id === 'e2')).toBeUndefined()
    expect(pts.find((p) => p.entity_id === 'e3')).toMatchObject({ kind: 'entity', lat: 48.85 })
  })
})
