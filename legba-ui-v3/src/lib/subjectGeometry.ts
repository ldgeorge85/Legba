/**
 * subjectGeometry — THE SUBJECT GEOMETRY CONTRACT (UI-B4/map; UI-D1 §2.4, §8 W8;
 * C-1a §2 `map`, §6.5).
 *
 * The map used to know one shape of subject: a country desk, whose ISO code it
 * PARSED OUT OF THE TARGET ID (`country_g20_us` → `US`) and looked up in a
 * gazetteer. That is the exemplar's id convention leaking into the platform: a
 * subject called `regime_ofac_russia`, `lane_hormuz` or `counterparty_acme`
 * has no code in its id, and should not need one. This module replaces the
 * parse with a contract:
 *
 *   1. **The profile decides whether there is a map at all.** `profile_ui.json`
 *      carries `map.geometry ∈ {iso2, declared, none}` (PROFILE-1, read by
 *      `lib/profileUi.ts`). `none` means the instance draws no map: no stance
 *      seeds the map surface ({@link placementsForProfile}), the `＋` catalog
 *      does not list it, and `open_panel(map)` / `map(...)` are refused by
 *      name, `no_map_surface` (the one executor, `lib/applyAction.ts`).
 *
 *   2. **The SUBJECT declares where it is — as data.** {@link geometryFor} reads
 *      the subject's target descriptor (`GET /registry/descriptors?family=
 *      target`, the same read the Target Registry panel makes): its
 *      `scope.geo` codes (the geo domain's declared ISO-3166 codes) and, where
 *      a descriptor carries one, a declared `scope.geometry` (a GeoJSON Point,
 *      Polygon or MultiPolygon). Never an ISO code parsed from an id.
 *
 *   3. **`fit_ref` resolves through it** ({@link cameraFor}): a point flies to
 *      itself, a polygon or a multi-country subject fits its bounds, a single
 *      code flies to its gazetteer centroid, and a subject with no declared
 *      geometry leaves the camera where it is — `{kind: 'none', reason}` says
 *      why, and nothing moves to (0, 0).
 *
 * Every map renderer (`v4/globe/`, `v4/world/MapLibreWorldMap.tsx`) reads
 * the same contract through {@link useSubjectCamera}.
 *
 * DECLARED GAP (docs/SEAMS.md #66): the target schema (`data/schemas/target.py`,
 * `extra="forbid"`) does not admit a `scope.geometry` yet, so today every
 * declared geometry is an ISO-code list. The point / polygon path is real code
 * over real data the moment a descriptor can carry one; nothing here invents
 * a shape.
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import { COUNTRY_BY_ISO2 } from '@/lib/countryGeo'
import { SUBJECT_ROSTER_QUERY_KEY, SUBJECT_ROSTER_ROUTE } from '@/lib/deskNames'
import { PROFILE_UI, type ProfileUi } from '@/lib/profileUi'
import { useScope } from '@/state/scope'
import { useSelection } from '@/state/selection'

// ---------------------------------------------------------------------------
// 1. Whether the instance has a map surface at all.
// ---------------------------------------------------------------------------

/** The refusal's name (C-1a §2): a profile that declares no map. */
export const NO_MAP_SURFACE = 'no_map_surface'

export type ProfileGeometry = ProfileUi['map']['geometry']

/**
 * The map surface kinds: the survivor (`v4.map`) and the per-subject kind
 * UI-D1 Table B folds into it (`target.map` → `v4.map {subject}`). Either one
 * is "the map" for the purpose of the refusal.
 */
export const MAP_SURFACE_KINDS: readonly string[] = ['v4.map', 'target.map']

/** True when the profile declares a geometry the map can draw. */
export function mapSurfaceDeclared(ui: ProfileUi = PROFILE_UI): boolean {
  return ui.map.geometry !== 'none'
}

export function isMapSurfaceKind(kind: string | null | undefined): boolean {
  return !!kind && MAP_SURFACE_KINDS.includes(kind)
}

/** The refusal reason, by name, with the profile that declared it. */
export function noMapSurfaceReason(ui: ProfileUi = PROFILE_UI): string {
  return `${NO_MAP_SURFACE}: the ${ui.profile} profile declares map.geometry none`
}

/** A placement in a stance seed or a mission (the `PresetPlacement` shape). */
export interface MapPlacement {
  kind: string
  position?: { referencePanel: string; direction: string }
}

/**
 * Drop every map-surface placement from a seed and RE-ANCHOR what hung off it:
 * a placement positioned against a removed tile takes that tile's own position
 * (transitively), so the arrangement closes over the gap rather than handing
 * Dockview a reference to a tile that never opens. An anchor (no position) that
 * is removed promotes its first dependent to anchor.
 */
export function withoutMapSurface<P extends MapPlacement>(placements: readonly P[]): P[] {
  const removed = new Map<string, P['position'] | undefined>()
  const out: P[] = []
  for (const p of placements) {
    let position = p.position
    // Follow the chain: a reference to a removed tile becomes its position.
    for (let guard = 0; position && removed.has(position.referencePanel) && guard < 16; guard++) {
      const inherited = removed.get(position.referencePanel)
      position = inherited ? { ...inherited, direction: position.direction === 'within' ? inherited.direction : position.direction } : undefined
    }
    if (isMapSurfaceKind(p.kind)) {
      removed.set(p.kind, position)
      continue
    }
    if (position === p.position) out.push(p)
    else if (position) out.push({ ...p, position })
    else {
      const { position: _dropped, ...rest } = p
      void _dropped
      out.push(rest as P)
    }
  }
  return out
}

/** The placements this profile can mount: unchanged, or without the map surface. */
export function placementsForProfile<P extends MapPlacement>(
  placements: readonly P[],
  ui: ProfileUi = PROFILE_UI,
): P[] {
  return mapSurfaceDeclared(ui) ? [...placements] : withoutMapSurface(placements)
}

// ---------------------------------------------------------------------------
// 2. The subject's declared geometry.
// ---------------------------------------------------------------------------

/** A subject (or record) the camera is asked to fit. */
export interface SubjectRef {
  kind: string
  id: string
}

/** What a subject's descriptor declares, read as data. */
export interface DeclaredGeometry {
  /** `scope.geo` — ISO-3166 codes as the descriptor declares them. */
  geo: string[]
  /** `scope.geometry` — a GeoJSON geometry, when the descriptor carries one. */
  geometry: unknown
}

export type SubjectGeometryIndex = ReadonlyMap<string, DeclaredGeometry>

/** `[lon, lat]` — GeoJSON order. */
export type LonLat = [number, number]

export type SubjectGeometry =
  | { kind: 'iso2'; codes: string[]; from: 'scope.geo' }
  | { kind: 'point'; coordinates: LonLat; from: 'scope.geometry' }
  | { kind: 'polygon'; rings: LonLat[][]; from: 'scope.geometry' }
  | { kind: 'none'; reason: string }

function obj(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

/**
 * Index a `GET /registry/descriptors?family=target` listing by descriptor id.
 * Reads `body.scope.geo` and `body.scope.geometry`; a row that is not the
 * route's shape is skipped, never guessed at.
 */
export function geometryIndexFrom(rows: unknown): SubjectGeometryIndex {
  const out = new Map<string, DeclaredGeometry>()
  if (!Array.isArray(rows)) return out
  for (const r of rows) {
    const row = obj(r)
    const id = typeof row.descriptor_id === 'string' ? row.descriptor_id : null
    if (!id) continue
    const scope = obj(obj(row.body).scope)
    const geo = Array.isArray(scope.geo) ? scope.geo.filter((g): g is string => typeof g === 'string') : []
    out.set(id, { geo, geometry: scope.geometry ?? null })
  }
  return out
}

function lonLat(v: unknown): LonLat | null {
  if (!Array.isArray(v) || v.length < 2) return null
  const [lon, lat] = v
  if (typeof lon !== 'number' || typeof lat !== 'number') return null
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) return null
  if (lon < -180 || lon > 180 || lat < -90 || lat > 90) return null
  return [lon, lat]
}

function ring(v: unknown): LonLat[] | null {
  if (!Array.isArray(v)) return null
  const pts = v.map(lonLat)
  return pts.length >= 3 && pts.every((p): p is LonLat => p !== null) ? pts : null
}

/** A declared GeoJSON geometry → a point or a polygon, or null when it is not one. */
export function parseDeclaredGeometry(v: unknown): SubjectGeometry | null {
  const g = obj(v)
  if (g.type === 'Point') {
    const c = lonLat(g.coordinates)
    return c ? { kind: 'point', coordinates: c, from: 'scope.geometry' } : null
  }
  if (g.type === 'Polygon' && Array.isArray(g.coordinates)) {
    const rings = g.coordinates.map(ring)
    return rings.length > 0 && rings.every((r): r is LonLat[] => r !== null)
      ? { kind: 'polygon', rings, from: 'scope.geometry' }
      : null
  }
  if (g.type === 'MultiPolygon' && Array.isArray(g.coordinates)) {
    const rings: LonLat[][] = []
    for (const poly of g.coordinates) {
      if (!Array.isArray(poly)) return null
      for (const r of poly) {
        const parsed = ring(r)
        if (!parsed) return null
        rings.push(parsed)
      }
    }
    return rings.length > 0 ? { kind: 'polygon', rings, from: 'scope.geometry' } : null
  }
  return null
}

function iso2Geometry(declared: DeclaredGeometry): SubjectGeometry | null {
  const codes = [...new Set(declared.geo.map((c) => c.toUpperCase()))].filter((c) => COUNTRY_BY_ISO2[c])
  return codes.length > 0 ? { kind: 'iso2', codes, from: 'scope.geo' } : null
}

/**
 * The geometry a subject DECLARES, under this profile.
 *
 * Resolution order follows the profile's own geometry: an `iso2` profile reads
 * `scope.geo` first, a `declared` profile reads `scope.geometry` first, and
 * each falls back to the other (both are the descriptor's own data). A record
 * that is not a subject has no declared geometry here — the map places records
 * from their own coordinates before it asks this contract.
 */
export function geometryFor(
  ref: SubjectRef | null | undefined,
  index: SubjectGeometryIndex | null,
  ui: ProfileUi = PROFILE_UI,
): SubjectGeometry {
  if (!mapSurfaceDeclared(ui)) return { kind: 'none', reason: noMapSurfaceReason(ui) }
  if (!ref) return { kind: 'none', reason: 'nothing is selected or in scope' }
  if (ref.kind !== 'target') {
    return { kind: 'none', reason: `a ${ref.kind} carries no declared subject geometry` }
  }
  if (!index) return { kind: 'none', reason: 'the subject roster has not been read yet' }
  const declared = index.get(ref.id)
  if (!declared) return { kind: 'none', reason: `no registered descriptor declares ${ref.id}` }
  const shape = parseDeclaredGeometry(declared.geometry)
  const codes = iso2Geometry(declared)
  const first = ui.map.geometry === 'declared' ? shape ?? codes : codes ?? shape
  if (first) return first
  if (declared.geo.length > 0) {
    return {
      kind: 'none',
      reason: `${ref.id} declares ${declared.geo.join(', ')}, which the gazetteer cannot place`,
    }
  }
  return { kind: 'none', reason: `${ref.id} declares no geometry in its scope` }
}

// ---------------------------------------------------------------------------
// 3. The camera: what `fit_ref` (and a selection) resolves to.
// ---------------------------------------------------------------------------

export type CameraTarget =
  | { kind: 'center'; lon: number; lat: number }
  | { kind: 'bounds'; west: number; south: number; east: number; north: number }

function bounds(points: LonLat[]): CameraTarget | null {
  if (points.length === 0) return null
  let west = Infinity
  let south = Infinity
  let east = -Infinity
  let north = -Infinity
  for (const [lon, lat] of points) {
    west = Math.min(west, lon)
    east = Math.max(east, lon)
    south = Math.min(south, lat)
    north = Math.max(north, lat)
  }
  if (west === east && south === north) return { kind: 'center', lon: west, lat: south }
  return { kind: 'bounds', west, south, east, north }
}

/** A declared geometry → where the camera goes; null leaves it where it is. */
export function cameraFor(g: SubjectGeometry): CameraTarget | null {
  switch (g.kind) {
    case 'point':
      return { kind: 'center', lon: g.coordinates[0], lat: g.coordinates[1] }
    case 'polygon':
      return bounds(g.rings.flat())
    case 'iso2':
      return bounds(
        g.codes.flatMap((c) => {
          const fix = COUNTRY_BY_ISO2[c]
          return fix ? [[fix.lon, fix.lat] as LonLat] : []
        }),
      )
    case 'none':
      return null
  }
}

/** `fit_ref` in one call: the declared geometry and the camera it implies. */
export function fitRef(
  ref: SubjectRef | null | undefined,
  index: SubjectGeometryIndex | null,
  ui: ProfileUi = PROFILE_UI,
): { geometry: SubjectGeometry; camera: CameraTarget | null } {
  const geometry = geometryFor(ref, index, ui)
  return { geometry, camera: cameraFor(geometry) }
}

// ---------------------------------------------------------------------------
// React: the index read and the selection-driven subject the renderers fit.
// ---------------------------------------------------------------------------

/** The subject roster's query (`lib/deskNames.ts`) — same key, same route,
 *  one request; the summary weight carries `scope.geo` and `scope.geometry`. */
export const SUBJECT_INDEX_QUERY_KEY = SUBJECT_ROSTER_QUERY_KEY
const SUBJECT_INDEX_ROUTE = SUBJECT_ROSTER_ROUTE

/** Every registered subject's declared geometry; null until the roster is read. */
export function useSubjectGeometryIndex(): SubjectGeometryIndex | null {
  const declared = mapSurfaceDeclared()
  const q = useQuery<unknown>({
    queryKey: SUBJECT_INDEX_QUERY_KEY,
    queryFn: () => apiGet<unknown>(SUBJECT_INDEX_ROUTE),
    enabled: declared,
    staleTime: 300_000,
  })
  return useMemo(() => (q.data === undefined ? null : geometryIndexFrom(q.data)), [q.data])
}

/**
 * The subject the camera should fit for the current selection: the selected
 * subject, or — when the selection is a record the map does not itself hold —
 * the wall scope's single subject. No selection, no subject: the camera follows
 * the selection (design §3.1), never a bare scope change.
 */
export function useFocusedSubject(): SubjectRef | null {
  const selection = useSelection((s) => s.selection)
  const scope = useScope((s) => s.scope)
  const ids = scope?.members.targetIds ?? []
  const id = !selection
    ? null
    : selection.kind === 'target'
      ? selection.id
      : ids.length === 1
        ? ids[0]
        : null
  return useMemo(() => (id ? { kind: 'target', id } : null), [id])
}

/** The renderers' shared hook: the focused subject, its geometry, its camera. */
export function useSubjectCamera(): {
  subject: SubjectRef | null
  geometry: SubjectGeometry
  camera: CameraTarget | null
  index: SubjectGeometryIndex | null
} {
  const index = useSubjectGeometryIndex()
  const subject = useFocusedSubject()
  return useMemo(() => {
    const { geometry, camera } = fitRef(subject, index)
    return { subject, geometry, camera, index }
  }, [subject, index])
}
