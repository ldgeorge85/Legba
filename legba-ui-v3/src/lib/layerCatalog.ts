/**
 * layerCatalog — every MAP layer, declared once, each with its APERTURE
 * (UI-B4/map; UI-D1 §8 W8 "layer catalog with aperture `i`"; the 09-26
 * research note §4 piece C).
 *
 * World Monitor's layer catalog carries a per-layer `i`. Ours is worth having
 * for one reason: a map layer is a VIEW over our own objects, and a reader who
 * sees a quiet map cannot tell "nothing happened" from "this layer cannot see
 * that". So every layer the map can draw states, behind its `i`, what it can
 * see:
 *
 *   - **reads** — the route it reads (and its row cap, where it has one);
 *   - **source layers** — which of the six declared source layers
 *     (`docs/DATA_SOURCES.md`) can reach it;
 *   - **window** — the temporal window it is cut to (the one shared clock, or
 *     its own lookback when it has one, stated as such);
 *   - **subjects** — the subject roster it reads, or that it reads none;
 *   - **placement** — how a row earns a position, and what never does.
 *
 * The six source layers' per-subject DECLARATION (present / absent /
 * unmeasured / undeclared) is a different object and stays where it is
 * (`lib/layerAperture.ts`, the LayerPanel's "Source layers · aperture"
 * section): this catalog is about the map's DRAW layers.
 *
 * WHICH LAYERS EXIST COMES FROM THE PROFILE. A layer declares the profile
 * geometries it can draw under: the banded-verdict choropleth needs subjects
 * that declare ISO codes (`map.geometry: iso2`); the point layers place rows
 * from their own coordinates and draw under `iso2` or `declared`; under
 * `none` there is no map and so no layer at all ({@link layersForProfile}).
 * The default layer set is the profile's layers with `defaultOn` — so the
 * geopolitics profile keeps exactly today's layers (the snapshot test) and no
 * profile is forced onto a choropleth it cannot fill.
 *
 * ABSENCE. A layer whose read has not answered prints *no read yet*
 * (`lib/absenceVocabulary.ts`), never a 0 and never an empty choropleth; a
 * layer that has read and found nothing in the window prints a measured 0.
 */
import type { FigureData } from '@/lib/Figure'
import { PROFILE_UI, type ProfileUi } from '@/lib/profileUi'
import type { MapEngine } from '@/lib/mapEngine'
import type { WorldLayer } from '@/v4/world/worldState'

export type LayerId =
  | 'choropleth'
  | 'signals'
  | 'findings'
  | 'situations'
  | 'events'
  | 'entities'
  | 'density'
  | 'arcs'
  | 'convergence'
  | 'watch'

/** What a layer can see — the content of its `i`. Every field is required. */
export interface LayerAperture {
  reads: string
  sourceLayers: string
  window: string
  subjects: string
  placement: string
}

export interface CatalogLayer {
  id: LayerId
  label: string
  /**
   * The world-store key when the layer is one of the store's toggles — the
   * keys a mission and a case's `map.layers` name. Null for a renderer-local
   * mode (choropleth, density, arcs, convergence, watch).
   */
  storeKey: WorldLayer | null
  /** The profile geometries the layer can draw under. */
  geometries: readonly Exclude<ProfileUi['map']['geometry'], 'none'>[]
  /** The renderers that draw it. */
  engines: readonly MapEngine[]
  /** On when the map first mounts. */
  defaultOn: boolean
  /** False for a layer the vocabulary names but no renderer draws yet. */
  drawn: boolean
  /** True when the layer is cut to the one shared window. */
  windowed: boolean
  aperture: LayerAperture
}

const EVERY_SOURCE_LAYER =
  'every source layer — a signal is drawn whatever layer its source is filed under; the map applies no layer filter'

export const LAYER_CATALOG: readonly CatalogLayer[] = [
  {
    id: 'choropleth',
    label: 'Verdict choropleth',
    storeKey: null,
    geometries: ['iso2'],
    engines: ['maplibre'],
    defaultOn: true,
    drawn: true,
    windowed: false,
    aperture: {
      reads: 'GET /findings?analyst_id=country_composition&limit=200 — the composition tier, newest verified read per subject',
      sourceLayers: 'none directly — it reads the composition, which reads its units, which read their sources',
      window: 'not the shared window: the newest composition on record, whatever its age (the hover states it)',
      subjects: 'subjects whose composition is on record and whose subject maps to a country code; every other country is base land — unassessed, never a colour',
      placement: 'the country polygon of the subject (Natural Earth 110m); a subject that declares no ISO code is not drawn',
    },
  },
  {
    id: 'signals',
    label: 'Signals',
    storeKey: 'signals',
    geometries: ['iso2', 'declared'],
    engines: ['maplibre', 'drawn'],
    defaultOn: true,
    drawn: true,
    windowed: true,
    aperture: {
      reads: 'GET /signals — the newest 3,000 ingested signals, cursor-paged',
      sourceLayers: EVERY_SOURCE_LAYER,
      window:
        "the shared window (the one clock the scrubber and missions write), on the signal's VALID time — the publisher's own instant (data.published_at); a signal that carries none is not drawn on the window, and its fetch time is never borrowed",
      subjects: 'none — the stream is global; the country filter narrows it, the subject roster does not',
      placement: "the signal's own coordinate, else the gazetteer centroid of its first geo code; a signal with neither is not drawn",
    },
  },
  {
    id: 'findings',
    label: 'Findings',
    storeKey: 'findings',
    geometries: ['iso2', 'declared'],
    engines: ['maplibre', 'drawn'],
    defaultOn: true,
    drawn: true,
    windowed: true,
    aperture: {
      reads: 'GET /findings?limit=500 — the newest 500 findings of every unit',
      sourceLayers: 'none directly — a finding is a unit read; its sources are on its receipt',
      window: 'the shared window, on the finding\'s produced time',
      subjects: "every subject on the roster; a finding places through its subject's declared geometry when it carries none of its own",
      placement: "the finding's own coordinate, else its geo codes, else its subject's declared geometry; a finding with none of these is not drawn",
    },
  },
  {
    id: 'situations',
    label: 'Situations',
    storeKey: 'situations',
    geometries: ['iso2', 'declared'],
    engines: ['maplibre', 'drawn'],
    defaultOn: true,
    drawn: true,
    windowed: true,
    aperture: {
      reads: 'GET /situations?limit=500 — the newest 500 situations (one page)',
      sourceLayers: 'none directly — a situation frames findings',
      window: "the shared window, on the situation's VALID time — its newest member event (last_event_at)",
      subjects: "the situation's subject, when it has one — placed through that subject's declared geometry",
      placement:
        "the centroid of its first placeable country where the route serves countries (it serves none today), else its subject's declared geometry when that names one place; a situation with neither is not drawn",
    },
  },
  {
    id: 'events',
    label: 'Events',
    storeKey: 'events',
    geometries: ['iso2', 'declared'],
    engines: ['maplibre', 'drawn'],
    defaultOn: true,
    drawn: true,
    windowed: true,
    aperture: {
      reads: 'GET /v3/events?limit=500&fields=summary — the newest 500 bounded events, light rows',
      sourceLayers: 'none directly — an event is reconciled from signals; its sources are on its receipt',
      window: "the shared window, on the event's start",
      subjects: 'none — placed by its own coordinate only',
      placement: 'the coordinate its writers stamped, ONLY — a country-only event claims no precision and is not drawn',
    },
  },
  {
    id: 'entities',
    label: 'Entities',
    storeKey: 'entities',
    geometries: ['iso2', 'declared'],
    engines: [],
    defaultOn: false,
    drawn: false,
    windowed: false,
    aperture: {
      reads: 'nothing — the case vocabulary names this layer, but no renderer draws it yet',
      sourceLayers: 'not applicable: nothing is drawn',
      window: 'not applicable: nothing is drawn',
      subjects: 'not applicable: nothing is drawn',
      placement: 'not drawn — toggling it changes nothing on the map, and the map does not pretend otherwise',
    },
  },
  {
    id: 'density',
    label: 'Signal density',
    storeKey: null,
    geometries: ['iso2', 'declared'],
    engines: ['maplibre'],
    defaultOn: false,
    drawn: true,
    windowed: true,
    aperture: {
      reads: 'the Signals layer\'s rows — no read of its own',
      sourceLayers: EVERY_SOURCE_LAYER,
      window: 'the shared window',
      subjects: 'none — the stream is global',
      placement: "the signals' placements, weighted by severity (heat, or 60 km hexagons)",
    },
  },
  {
    id: 'arcs',
    label: 'Co-mention arcs',
    storeKey: null,
    geometries: ['iso2'],
    engines: ['maplibre'],
    defaultOn: false,
    drawn: true,
    windowed: true,
    aperture: {
      reads: "the Signals layer's rows — no read of its own",
      sourceLayers: EVERY_SOURCE_LAYER,
      window: 'the shared window',
      subjects: 'none — country pairs one signal co-references, at least twice',
      placement: 'between the two countries\' centroids; empty while enrichment writes one country per signal (a declared gap, docs/UI.md "Declared gaps")',
    },
  },
  {
    id: 'convergence',
    label: 'Convergence',
    storeKey: null,
    geometries: ['iso2', 'declared'],
    engines: ['maplibre'],
    defaultOn: true,
    drawn: true,
    windowed: false,
    aperture: {
      reads: 'GET /v3/since?channel=geo_convergence — the cross-stream correlator\'s alerts',
      sourceLayers: 'every source layer the correlator reads (it counts streams, not layers)',
      window: 'its own 7-day lookback, NOT the shared window',
      subjects: 'none — bins are cells and countries, not subjects',
      placement: "the alert's cell or country; an alert with no placement is not drawn",
    },
  },
  {
    id: 'watch',
    label: 'Watch locations',
    storeKey: null,
    geometries: ['iso2', 'declared'],
    engines: ['maplibre'],
    defaultOn: true,
    drawn: true,
    windowed: false,
    aperture: {
      reads: "nothing from the server — the operator's own points, kept in this browser",
      sourceLayers: 'not applicable: the points are the operator\'s; the proximity halo counts windowed signals',
      window: 'the points have none; the halo uses the shared window',
      subjects: "none — the points are the operator's own places, not subjects",
      placement: 'where the operator dropped the point, with its radius ring',
    },
  },
]

const BY_ID = new Map(LAYER_CATALOG.map((l) => [l.id, l]))

export function catalogLayer(id: LayerId): CatalogLayer {
  const l = BY_ID.get(id)
  if (!l) throw new Error(`unknown map layer: ${id}`)
  return l
}

/** The layer for a world-store key (the LayerPanel's toggles, a mission's layers). */
export function layerForStoreKey(key: WorldLayer): CatalogLayer | undefined {
  return LAYER_CATALOG.find((l) => l.storeKey === key)
}

/**
 * The layers this profile can draw — none at all under `geometry: none`; under
 * any other geometry, the layers that declare it. With an `engine`, only the
 * layers that renderer draws.
 */
export function layersForProfile(ui: ProfileUi = PROFILE_UI, engine?: MapEngine): CatalogLayer[] {
  const g = ui.map.geometry
  if (g === 'none') return []
  return LAYER_CATALOG.filter(
    (l) => l.geometries.includes(g) && (engine === undefined || l.engines.includes(engine) || l.storeKey !== null),
  )
}

/** The profile's DEFAULT layer set (ids, catalog order). */
export function defaultLayerIds(ui: ProfileUi = PROFILE_UI, engine?: MapEngine): LayerId[] {
  return layersForProfile(ui, engine)
    .filter((l) => l.defaultOn)
    .map((l) => l.id)
}

/** Whether a layer starts on under this profile (false when the profile cannot draw it). */
export function layerDefaultOn(id: LayerId, ui: ProfileUi = PROFILE_UI): boolean {
  return defaultLayerIds(ui).includes(id)
}

/** Whether this profile can draw a layer at all. */
export function layerAvailable(id: LayerId, ui: ProfileUi = PROFILE_UI): boolean {
  return layersForProfile(ui).some((l) => l.id === id)
}

/** The aperture as labelled lines, in a fixed order — the `i`'s body. */
export function apertureLines(layer: CatalogLayer, technical = true): { label: string; text: string }[] {
  const a = layer.aperture
  return [
    technical ? { label: 'Reads', text: a.reads } : { label: 'Shows', text: plainReads(a.reads) },
    { label: 'Source layers', text: a.sourceLayers },
    { label: 'Window', text: a.window },
    { label: 'Subjects', text: a.subjects },
    { label: 'Placement', text: a.placement },
  ]
}

/**
 * What a layer reads, without its route: the route is the admin's detail, a
 * reader is told what the rows are (`GET /signals — the newest 3,000 …` →
 * `the newest 3,000 …`).
 */
export function plainReads(reads: string): string {
  const i = reads.indexOf(' — ')
  if (i >= 0) return reads.slice(i + 3).trim()
  return reads.replace(/^(?:GET|POST)\s+\S+\s*/, '').trim() || 'what this layer draws'
}

/** The aperture as one sentence (the accessible description, a CSV cell). */
export function apertureSentence(layer: CatalogLayer, technical = true): string {
  return `${layer.label}. ${apertureLines(layer, technical)
    .map((l) => `${l.label}: ${l.text}.`)
    .join(' ')}`
}

/** An instant as a short UTC stamp, `2026-10-04 07:00Z`. */
export function utcStamp(ms: number): string {
  return new Date(ms).toISOString().slice(0, 16).replace('T', ' ') + 'Z'
}

/**
 * A layer's row count as a Figure. `null`/`undefined` — the read has not
 * answered (or, for the choropleth, the tier holds no read) — is *no read
 * yet*; a number is a measured count (a measured 0 stays 0) as of `asOfMs`:
 * the window's end for a windowed layer, the newest row for one that is not.
 * A layer no renderer draws prints "—", and its `i` says why.
 */
export function layerCountFigure(
  layer: CatalogLayer,
  count: number | null | undefined,
  asOfMs: number | null,
): FigureData {
  const unit = layer.windowed ? 'in window' : 'on record'
  if (!layer.drawn) return { state: 'dash', unit, asOf: { absent: 'no renderer draws this layer' } }
  if (count === null || count === undefined) {
    return { state: 'no_read_yet', unit, asOf: { absent: 'the layer has not read yet' } }
  }
  const asOf = asOfMs === null || !Number.isFinite(asOfMs) ? { absent: 'the read carries no instant' } : utcStamp(asOfMs)
  return { state: 'measured', value: count, unit, asOf }
}
