/**
 * Geo-point extraction for the Target Map (UI-3 / Tier B).
 *
 * MapLibre needs WebGL and can't run under jsdom, so the geo-extraction
 * logic is factored here — pure, deterministic, unit-testable. The Map
 * panel only does the rendering; this module decides WHICH rows become
 * markers and where.
 *
 * Geo lives in the geocode-filter populated `data.geo.{lat,lon}` payload
 * (see `legba.data.filters.geocode`). A finding may carry its own
 * `data.geo`; if not, we fall back to the geo of an upstream signal it
 * was derived from (best-effort, one geo per finding).
 *
 * Entity-geo backfill (`buildEntityGeoPoints`): the NER pipeline classes
 * country mentions as the generic `entity` class with no geo, so countries
 * never reach the map. We resolve a recognized country name to a centroid
 * (see `lib/countryGeo`) so those entities place as `location` markers.
 */

import { resolveCountry } from './countryGeo'

export interface GeoSignal {
  id: string
  title: string
  source_id?: string | null
  data?: Record<string, unknown> | null
}

export interface GeoFinding {
  id: string
  title: string
  severity?: string | null
  source_id?: string | null
  data?: Record<string, unknown> | null
  derived_from?: string[]
}

export interface GeoPoint {
  id: string
  lat: number
  lon: number
  title: string
  kind: 'signal' | 'finding' | 'entity'
  /** Present for findings — drives the severity color overlay. */
  severity?: string | null
  /**
   * Provenance-on-hover payload: the source the row came from (signals
   * carry their own `source_id`; findings inherit it from the upstream
   * signal whose geo they reused).
   */
  source_id?: string | null
  /** Display country name from `data.geo.country` (best-effort). */
  country?: string | null
  /** ISO-2 country code from `data.geo.country_iso2` — the count key. */
  country_iso2?: string | null
}

/** A `{lat,lon}` plus the geocode-filter country metadata it carries. */
export interface GeoFix {
  lat: number
  lon: number
  country: string | null
  country_iso2: string | null
}

/** Extract a geo fix from a row's `data.geo`, or null. */
export function extractGeo(
  row: { data?: Record<string, unknown> | null } | null | undefined,
): GeoFix | null {
  const data = row?.data
  if (!data || typeof data !== 'object') return null
  const geo = (data as Record<string, unknown>).geo as
    | Record<string, unknown>
    | undefined
  if (!geo || typeof geo !== 'object') return null
  const lat = Number((geo as Record<string, unknown>).lat)
  const lon = Number((geo as Record<string, unknown>).lon)
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null
  if (lat === 0 && lon === 0) return null
  const country = (geo as Record<string, unknown>).country
  const iso2 = (geo as Record<string, unknown>).country_iso2
  return {
    lat,
    lon,
    country: typeof country === 'string' && country ? country : null,
    country_iso2: typeof iso2 === 'string' && iso2 ? iso2 : null,
  }
}

/**
 * Build the full marker set from signals + findings.
 *
 * - Every geocoded signal becomes a `signal` marker.
 * - Every finding becomes a `finding` marker using its own `data.geo`,
 *   falling back to the first upstream signal's geo. Findings carry
 *   `severity` for the color overlay.
 */
export function buildGeoPoints(
  signals: GeoSignal[],
  findings: GeoFinding[],
): GeoPoint[] {
  const out: GeoPoint[] = []
  const signalsById = new Map<string, GeoSignal>()

  for (const s of signals) {
    signalsById.set(s.id, s)
    const g = extractGeo(s)
    if (g) {
      out.push({
        id: s.id,
        lat: g.lat,
        lon: g.lon,
        title: s.title,
        kind: 'signal',
        source_id: s.source_id ?? null,
        country: g.country,
        country_iso2: g.country_iso2,
      })
    }
  }

  for (const f of findings) {
    // Prefer the finding's own geo, else inherit from an upstream signal —
    // and inherit that signal's source_id alongside the borrowed geo so the
    // provenance-on-hover payload stays honest.
    let g = extractGeo(f)
    let inheritedSource = f.source_id ?? null
    if (!g) {
      for (const upstream of f.derived_from ?? []) {
        const src = signalsById.get(upstream)
        const sg = extractGeo(src)
        if (sg) {
          g = sg
          inheritedSource = src?.source_id ?? inheritedSource
          break
        }
      }
    }
    if (!g) continue
    out.push({
      id: f.id,
      lat: g.lat,
      lon: g.lon,
      title: f.title,
      kind: 'finding',
      severity: f.severity ?? null,
      source_id: inheritedSource,
      country: g.country,
      country_iso2: g.country_iso2,
    })
  }

  return out
}

/** One country bucket for the per-country count breakdown box. */
export interface CountryCount {
  /** ISO-2 code, or 'UNK' when the geocoder left it unset. */
  iso2: string
  /** Display name (the most-recent non-null country seen for this iso2). */
  name: string
  signals: number
  findings: number
  total: number
}

/**
 * Aggregate points into per-country counts (signals + findings), sorted by
 * total descending. Drives the Map's count-breakdown box. Points with no
 * ISO-2 fall into an 'UNK' bucket so the totals reconcile with the map.
 */
export function countByCountry(points: GeoPoint[]): CountryCount[] {
  const byIso = new Map<string, CountryCount>()
  for (const p of points) {
    const iso2 = p.country_iso2 ?? 'UNK'
    let bucket = byIso.get(iso2)
    if (!bucket) {
      bucket = { iso2, name: p.country ?? iso2, signals: 0, findings: 0, total: 0 }
      byIso.set(iso2, bucket)
    }
    if (p.country && bucket.name === iso2) bucket.name = p.country
    if (p.kind === 'signal') bucket.signals += 1
    else bucket.findings += 1
    bucket.total += 1
  }
  return [...byIso.values()].sort(
    (a, b) => b.total - a.total || a.name.localeCompare(b.name),
  )
}

// ---------------------------------------------------------------------------
// Entity-geo backfill — country entities → map markers.
// ---------------------------------------------------------------------------

/** An entity-graph node (subset of the `/entities` row we need for geo). */
export interface GeoEntity {
  id: string
  canonical_name: string
  entity_class: string
  /** Set when the backend gazetteer already resolved geo; preferred over the
   *  name fallback below. */
  geo_lat?: number | null
  geo_lon?: number | null
  geo_country?: string | null
}

/** An entity marker — like {@link GeoPoint} but tagged with the entity id. */
export interface EntityGeoPoint extends GeoPoint {
  kind: 'entity'
  entity_id: string
  entity_class: string
}

/**
 * Resolve ONE entity's geo, preferring the backend-resolved `geo_lat/geo_lon`
 * and falling back to a country-name centroid (the NER-classes-countries-as-
 * `entity` fix). Returns null when the entity is neither geo-tagged nor a
 * recognized country.
 */
export function entityGeo(e: GeoEntity): GeoFix | null {
  const lat = Number(e.geo_lat)
  const lon = Number(e.geo_lon)
  if (Number.isFinite(lat) && Number.isFinite(lon) && !(lat === 0 && lon === 0)) {
    return { lat, lon, country: e.geo_country ?? null, country_iso2: null }
  }
  // Name fallback: a recognized country → its centroid + iso2.
  const fix = resolveCountry(e.geo_country) ?? resolveCountry(e.canonical_name)
  if (fix) return { lat: fix.lat, lon: fix.lon, country: fix.name, country_iso2: fix.iso2 }
  return null
}

/**
 * Build map markers from entity-graph nodes. Only entities that resolve to a
 * geo (backend-tagged OR a recognized country name) become markers, so the map
 * picks up the country mentions the NER pipeline left geo-less.
 */
export function buildEntityGeoPoints(entities: GeoEntity[]): EntityGeoPoint[] {
  const out: EntityGeoPoint[] = []
  for (const e of entities) {
    const g = entityGeo(e)
    if (!g) continue
    out.push({
      id: `entity:${e.id}`,
      entity_id: e.id,
      entity_class: e.entity_class,
      lat: g.lat,
      lon: g.lon,
      title: e.canonical_name,
      kind: 'entity',
      country: g.country,
      country_iso2: g.country_iso2,
    })
  }
  return out
}
