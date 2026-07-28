/**
 * Operator watch-locations (P4-3, feature 5) — the single-operator "watch here"
 * model.
 *
 * The operator drops a few named points ("watch here") that highlight nearby
 * signals / events on the World map — the right-sized version of "proximity to
 * my assets" (worldmonitor's asset-proximity, scaled to one operator). Persisted
 * to localStorage, exactly like the Alert Center's subscriptions (no backend
 * write surface for an operator-local watchlist). All geometry + proximity +
 * persistence lives here so it's unit-testable without a DOM.
 */

export interface WatchLocation {
  id: string
  label: string
  lat: number
  lon: number
  /** Highlight radius in kilometres. */
  radiusKm: number
  createdAt: string
}

/** The minimal geo shape the proximity tests consume. */
export interface LatLon {
  lat: number
  lon: number
}

const STORAGE_KEY = 'legba.map.watch_locations'
const EARTH_RADIUS_KM = 6371
export const DEFAULT_WATCH_RADIUS_KM = 250
/** Radius options offered in the UI (km). */
export const WATCH_RADIUS_OPTIONS: readonly number[] = [100, 250, 500, 1000]
const MAX_WATCH_LOCATIONS = 24

function toRad(deg: number): number {
  return (deg * Math.PI) / 180
}

/** Great-circle distance between two lat/lon points, in kilometres. */
export function haversineKm(a: LatLon, b: LatLon): number {
  const dLat = toRad(b.lat - a.lat)
  const dLon = toRad(b.lon - a.lon)
  const lat1 = toRad(a.lat)
  const lat2 = toRad(b.lat)
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.min(1, Math.sqrt(h)))
}

/** Is `point` within `watch`'s radius? */
export function isNear(watch: WatchLocation, point: LatLon): boolean {
  return haversineKm(watch, point) <= watch.radiusKm
}

/**
 * The nearest watch-location to a point (within its own radius) + the distance,
 * or null when there are no watches or none covers the point.
 */
export function nearestWatch(
  point: LatLon,
  watches: readonly WatchLocation[],
): { watch: WatchLocation; km: number } | null {
  let best: { watch: WatchLocation; km: number } | null = null
  for (const w of watches) {
    const km = haversineKm(w, point)
    if (km <= w.radiusKm && (best === null || km < best.km)) {
      best = { watch: w, km }
    }
  }
  return best
}

/**
 * The set of point ids within ANY watch's radius — the highlight set the map
 * brushes onto proximate signals / events.
 */
export function pointsNearWatches<T extends LatLon & { id: string }>(
  watches: readonly WatchLocation[],
  points: readonly T[],
): Set<string> {
  const out = new Set<string>()
  if (watches.length === 0) return out
  for (const p of points) {
    if (nearestWatch(p, watches) !== null) out.add(p.id)
  }
  return out
}

/** Count of proximate points per watch id (the honest "N nearby" badge). */
export function nearbyCountByWatch<T extends LatLon>(
  watches: readonly WatchLocation[],
  points: readonly T[],
): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const w of watches) counts[w.id] = 0
  for (const p of points) {
    for (const w of watches) if (isNear(w, p)) counts[w.id] += 1
  }
  return counts
}

/**
 * A closed ring of GeoJSON [lon, lat] coords approximating the `radiusKm`
 * circle around `center` — the highlight halo drawn on the map. Uses a local
 * equirectangular approximation (fine at the country/region scale these
 * watches operate at). `steps` segments.
 */
export function circleRing(
  center: LatLon,
  radiusKm: number,
  steps = 48,
): [number, number][] {
  const ring: [number, number][] = []
  const latDeg = radiusKm / 110.574 // km per degree latitude
  const lonDeg = radiusKm / (111.32 * Math.cos(toRad(center.lat)) || 1e-6)
  for (let i = 0; i <= steps; i++) {
    const theta = (i / steps) * 2 * Math.PI
    ring.push([
      center.lon + lonDeg * Math.cos(theta),
      center.lat + latDeg * Math.sin(theta),
    ])
  }
  return ring
}

// --- persistence (localStorage-guarded) ------------------------------------

function isValid(w: unknown): w is WatchLocation {
  const o = w as Record<string, unknown> | null
  return (
    !!o &&
    typeof o.id === 'string' &&
    typeof o.lat === 'number' &&
    typeof o.lon === 'number' &&
    Number.isFinite(o.lat as number) &&
    Number.isFinite(o.lon as number) &&
    typeof o.radiusKm === 'number'
  )
}

export function loadWatchLocations(): WatchLocation[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? (parsed as unknown[]).filter(isValid) : []
  } catch {
    return []
  }
}

export function persistWatchLocations(w: readonly WatchLocation[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(w))
  } catch {
    /* ignore quota / private-mode failures — the choice still applies live */
  }
}

/** Add (or replace by id), capped at {@link MAX_WATCH_LOCATIONS}. */
export function addWatch(
  list: readonly WatchLocation[],
  w: WatchLocation,
): WatchLocation[] {
  const next = list.filter((x) => x.id !== w.id)
  next.push(w)
  return next.slice(-MAX_WATCH_LOCATIONS)
}

export function removeWatch(
  list: readonly WatchLocation[],
  id: string,
): WatchLocation[] {
  return list.filter((x) => x.id !== id)
}

/** Set one watch's radius (validated), returning a new list. */
export function setWatchRadius(
  list: readonly WatchLocation[],
  id: string,
  radiusKm: number,
): WatchLocation[] {
  const r = Math.max(1, Math.min(5000, radiusKm))
  return list.map((x) => (x.id === id ? { ...x, radiusKm: r } : x))
}

/** Mint a WatchLocation with a stable-ish id + validated radius/label. */
export function makeWatch(
  label: string,
  lat: number,
  lon: number,
  radiusKm = DEFAULT_WATCH_RADIUS_KM,
): WatchLocation {
  return {
    id: `w_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`,
    label: label.trim() || `${lat.toFixed(2)}, ${lon.toFixed(2)}`,
    lat,
    lon,
    radiusKm: Math.max(1, Math.min(5000, radiusKm)),
    createdAt: new Date().toISOString(),
  }
}
