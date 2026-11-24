/**
 * The globe's layer catalogue (U-4; layout design §3 "The layer catalogue").
 *
 * Grouped by OBJECT, at most seven rows, each with its source, its own legend
 * with units, what it can and cannot see (the `i`), its hard cap, whether it
 * is on at rest, and — for the rows Consult may drive — the world layer it is
 * bound to. A row whose data no route provides yet is listed as UNAVAILABLE
 * with the route it needs; it is never drawn from invented data.
 *
 * CONSULT'S VOCABULARY. The `map` drive action names the five world layers of
 * the closed server vocabulary (`workstation_vocabulary.MAP_LAYERS`:
 * signals, findings, situations, entities, events). A row bound to one of
 * them follows it — so a Consult turn that sets the layers moves the globe,
 * and its Undo (which restores the world layers) moves it back. The rows with
 * no binding (blanks, series, forecasts) are not drivable by Consult until the
 * vocabulary is widened server-side; the reader toggles them here.
 *
 * Dropped from the globe's catalogue, deliberately: co-mention arcs. Every
 * signal is resolved to one country upstream, so no pair forms and the layer
 * is empty by construction (`lib/mapLayers.ts::coMentionArcs`); it stays on
 * the flat map (flag off), and deck.gl is never loaded by the globe.
 */
import type { WorldLayer } from '@/v4/world/worldState'

export type GlobeLayerId = 'change' | 'blanks' | 'series' | 'forecasts' | 'events' | 'at_sea' | 'drift'

export interface LegendEntry {
  /** The mark, drawn by the legend (`chrome.tsx::LegendMark`). */
  mark: 'wash1' | 'wash2' | 'wash3' | 'hatch-dense' | 'hatch-sparse' | 'dot-good' | 'dot-warn' | 'dot-critical' | 'dot-none' | 'ring' | 'ring-heavy' | 'cluster' | 'point' | 'incident'
  text: string
  /** The legend's short form (the full text is its hover). */
  short?: string
}

export interface GlobeLayerDef {
  id: GlobeLayerId
  label: string
  /** The platform object it draws. */
  object: string
  /** Where its rows come from (source families and the route). */
  source: string
  /** What it can see and cannot — the `i`. */
  sees: string
  cannotSee: string
  legend: LegendEntry[]
  /** Hard cap on drawn marks, and what happens beyond it. */
  cap: string
  defaultOn: boolean
  /** The Consult-drivable world layer(s) this row follows. */
  world: WorldLayer[]
  /** Set when no route serves it: the route it needs. */
  unavailable?: string
}

export const GLOBE_CAPS = {
  subjects: 250,
  /** Numbers drawn on the map at once (layout design §2 budget: at most 12). */
  seriesGlyphs: 12,
  seriesPoints: 250,
  forecasts: 60,
  events: 2000,
  incidents: 500,
} as const

export const GLOBE_LAYERS: readonly GlobeLayerDef[] = [
  {
    id: 'change',
    label: 'Change since your last visit',
    object: 'subject state',
    source: 'the since-diff — verified findings, band crossings, situation edges and alerts by subject (GET /v3/since, the Board list\'s own read)',
    sees: 'every change the Board list ranks, counted on the subject it belongs to and cut to the window',
    cannotSee: 'a change with no subject, and a subject that declares no geometry',
    legend: [
      { mark: 'wash1', text: '1–2 changes', short: '1–2' },
      { mark: 'wash2', text: '3–5 changes', short: '3–5' },
      { mark: 'wash3', text: '6 or more changes', short: '6+ changes' },
    ],
    cap: `${GLOBE_CAPS.subjects} subjects`,
    defaultOn: true,
    world: ['findings'],
  },
  {
    id: 'blanks',
    label: 'Blanks',
    object: 'coverage and gaps',
    source: 'the subject roster (declared geometry and sources) and the subject reads (country compositions) in the window',
    sees: 'a subject with no read in the window (looked, could not see) and a subject that declares no source (not covered); a declared gap\'s reason on hover, read per subject from GET /v3/absence',
    cannotSee: 'declared gaps across the whole roster at once — that needs a coverage-per-subject route (layout design §4, item 5)',
    legend: [
      { mark: 'hatch-dense', text: 'looked, could not see — no read in the window', short: 'looked, could not see' },
      { mark: 'hatch-sparse', text: 'not covered — no source declared', short: 'not covered' },
    ],
    cap: `${GLOBE_CAPS.subjects} areas`,
    defaultOn: true,
    world: [],
  },
  {
    id: 'series',
    label: 'Series by place',
    object: 'numeric series',
    source: 'live series by place — IMF PortWatch daily chokepoint transits (GET /v3/map/series)',
    sees: 'the latest held day of each place, as a share of the mean of the 30 days before it; a revised day carries an r',
    cannotSee: 'a series subject this console has no position for (listed as unplaced); days the provider has not published yet (it runs about five days behind)',
    legend: [
      { mark: 'dot-good', text: 'within ±15 % of the 30-day baseline', short: '±15 %' },
      { mark: 'dot-none', text: 'between −25 % and −15 %, or above +15 %', short: 'other' },
      { mark: 'dot-warn', text: 'more than 25 % below', short: '< −25 %' },
      { mark: 'dot-critical', text: 'more than 50 % below', short: '< −50 %' },
    ],
    cap: `${GLOBE_CAPS.seriesGlyphs} labelled glyphs (the largest moves); the rest as dots without values`,
    defaultOn: true,
    world: [],
  },
  {
    id: 'forecasts',
    label: 'Open forecasts',
    object: 'forecast',
    source: 'the forecast ledger, open questions (GET /v3/ledger/forecasts)',
    sees: 'each open question at the first of its subjects that declares a place; the arc is the share of time elapsed from its window\'s start to its due date',
    cannotSee: 'a question whose subjects declare no place (counted in the legend as unplaced)',
    legend: [
      { mark: 'ring', text: 'open; the arc is time elapsed to the due date', short: 'arc = time elapsed' },
      { mark: 'ring-heavy', text: 'due within 7 days', short: 'due ≤ 7 days' },
    ],
    cap: `${GLOBE_CAPS.forecasts} rings`,
    defaultOn: true,
    world: [],
  },
  {
    id: 'events',
    label: 'Events and situations',
    object: 'evidence and situations',
    source: 'bounded events with their own coordinates (GET /v3/events) and situations placed by their countries or subject (GET /situations)',
    sees: 'events by their stamped point and situations by place, in the window, clustered by place',
    cannotSee: 'an event geocoded only to a country (it does not place), a situation with no place',
    legend: [
      { mark: 'point', text: 'one event or situation; an alerted one in its severity colour', short: 'one' },
      { mark: 'cluster', text: 'several in one place, with the count', short: 'several, with the count' },
    ],
    cap: `${GLOBE_CAPS.events.toLocaleString('en-US')} drawn after clustering (the newest)`,
    defaultOn: false,
    world: ['events', 'situations'],
  },
  {
    id: 'at_sea',
    label: 'Incidents at sea',
    object: 'signals filed at sea',
    source: 'signals the geocoder filed at sea, from any source that reports a point (GET /v3/map/at_sea)',
    sees: 'each incident at its own point in the window; the coastal states it lies off on hover (a relation, never a filing)',
    cannotSee: 'an incident reported without a point',
    legend: [{ mark: 'incident', text: 'one incident at its reported point', short: 'one incident' }],
    cap: `${GLOBE_CAPS.incidents} incidents`,
    defaultOn: false,
    world: ['signals'],
  },
  {
    id: 'drift',
    label: 'Changed after read',
    object: 'source drift',
    source: 'source drift events with a place',
    sees: '—',
    cannotSee: 'everything: no route serves drift events with a place yet',
    legend: [],
    cap: '100 marks',
    defaultOn: false,
    world: [],
    unavailable: 'a route serving drift events with the source\'s or the subject\'s place (layout design §4, item 7)',
  },
]

export function globeLayer(id: GlobeLayerId): GlobeLayerDef {
  return GLOBE_LAYERS.find((l) => l.id === id)!
}

/** The rows on at rest. */
export function defaultGlobeLayers(): Record<GlobeLayerId, boolean> {
  return Object.fromEntries(GLOBE_LAYERS.map((l) => [l.id, l.defaultOn && !l.unavailable])) as Record<GlobeLayerId, boolean>
}

/** The rows a world-layer set turns on (Consult's `map` action, its Undo). */
export function globeLayersFromWorld(
  world: Record<WorldLayer, boolean>,
  current: Record<GlobeLayerId, boolean>,
): Record<GlobeLayerId, boolean> {
  const next = { ...current }
  for (const l of GLOBE_LAYERS) {
    if (l.world.length === 0 || l.unavailable) continue
    next[l.id] = l.world.some((w) => world[w])
  }
  return next
}
