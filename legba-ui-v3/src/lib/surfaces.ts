/**
 * surfaces — THE ARRANGEMENT MODEL (U-6 frame; 02_UI_TARGET_BRIEF §12, which
 * supersedes the fixed Consult column of §2/§11).
 *
 * The console is TWO SIDES, kept apart (the owner: "we shouldn't mix the infra
 * and technical bits with the actual workstation usage side"):
 *
 *   READER    Board · Subject · Ledger · Watch            Alt+1 … Alt+4
 *   OPERATOR  Engine · Evaluation · Gate · Investigate    Alt+5 … Alt+8
 *
 * EVERY PAGE IS THE SAME FRAME (`lib/frameGrid.ts` has the numbers): a slim
 * rail; a CENTRE at a reading measure, split by a draggable divider into the
 * STAGE above and the CHAT (Consult) below; and PANELS to the right in the
 * remaining width — one column on a narrow screen, two or three on a wide one.
 * A page declares only three things here:
 *
 *   - `stage`    what the stage shows by default (today's read on the Board);
 *   - `regions`  the panel side's seeded regions, lead first, each a Dockview
 *                group whose tabs are the views the reader can swap between
 *                (at most {@link MAX_SEEDED_REGIONS}; the reader may add up to
 *                {@link MAX_REGIONS});
 *   - `chat`     whether the chat starts open (about half the centre) or
 *                collapsed to the ask bar.
 *
 * Consult is NOT a region: it is the chat, which the reader can resize (the
 * divider), collapse to the ask bar, pop to the full centre or move to a
 * column on the left or right (`state/frame.ts`). The Record is the sheet
 * behind a statement (`components/RecordSheet.tsx`); the time band is frame.
 *
 * What replaced what (brief §7, then §12):
 *   - the six workspaces, four missions, seven presets, user missions and the
 *     Save/Restore slot → these eight pages; a SAVED VIEW IS A LINK
 *     (`lib/address.ts`), never a layout blob;
 *   - "an open adds a column" → an open lands on the STAGE (a page-sized view,
 *     with a way back) or REPLACES the opened view of its home region
 *     ({@link homeOf}); it never adds a column;
 *   - the stale-slot merge and the re-seed epoch → ONE versioned store per
 *     page ({@link surfaceLayoutVersion}) with a Reset. A stored arrangement
 *     whose version differs, which is missing a seeded view the reader did not
 *     close, or which holds more than {@link MAX_REGIONS} regions is DISCARDED
 *     for the default, with a one-line notice — never merged.
 *
 * The retired ids (`#ws=morning_read`, a recorded case's `arrange desk`, a
 * `#mission=crisis` link) resolve through the legacy tables in
 * `lib/workspaces.ts`, `lib/layoutPresets.ts` and `lib/missions.ts` onto the
 * nearest page ({@link resolveSurfaceId}).
 *
 * Everything in this module is pure or storage-only: no Dockview, no React.
 */
import type { SerializedDockview } from 'dockview-react'
import type { PanelKind } from '@/types'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { rewriteSerializedLayout } from '@/panel-registry/aliases'
import { isMapSurfaceKind, mapSurfaceDeclared } from '@/lib/subjectGeometry'
import { PROFILE_UI } from '@/lib/profileUi'
import { legacyWorkspaceSurface } from '@/lib/workspaces'
import { legacyPresetSurface } from '@/lib/layoutPresets'
import { legacyMissionSurface } from '@/lib/missions'
import { contractOf } from '@/panel-registry/contractOf'

// ---------------------------------------------------------------------------
// The model.
// ---------------------------------------------------------------------------

export type Side = 'reader' | 'operator'

export type ReaderSurfaceId = 'board' | 'subject' | 'ledger' | 'watch'
export type OperatorSurfaceId = 'engine' | 'evaluation' | 'gate' | 'investigate'
export type SurfaceId = ReaderSurfaceId | OperatorSurfaceId

/**
 * A region a panel that does not exist yet will fill. Rendered by
 * `components/RegionSlot.tsx` as an honest empty region that says what will
 * be there and what it waits on — never a number, never a fake list.
 */
export type SlotId = 'slot:subject_forecasts' | 'slot:coverage' | 'slot:subject_field'

/**
 * A MOUNTED VIEW: a built component a region hosts that is NOT a panel kind
 * (the registry's kind ceiling holds). `App.tsx` renders it through the same
 * `slot` dock component as a slot (`components/MountedView.tsx`). U-11: the
 * Ledger page's three (the forecasts on its stage, the track record and the
 * evaluation record beside it) and the Gate's resolution queue (operator side).
 */
export type MountedViewId = 'view:forecasts' | 'view:track_record' | 'view:proof' | 'view:resolutions'

/**
 * The Board's stage: the ranked list of what changed (`v4/read/BoardList.tsx`),
 * a view registered with its own component. The long-form daily read
 * (`v4.morning_read`) is one link from it.
 */
export const BOARD_LIST_VIEW = 'view:board_changed' as const
/** The long-form daily read, reached from the Board's list. */
export const DAILY_READ_VIEW = 'v4.morning_read' as const

export const MOUNTED_VIEWS: Readonly<Record<MountedViewId, { title: string }>> = {
  'view:forecasts': { title: 'Forecasts' },
  'view:track_record': { title: 'Track record' },
  'view:proof': { title: 'Evaluations' },
  'view:resolutions': { title: 'Resolutions' },
}

export function isMountedViewId(id: string): id is MountedViewId {
  return id in MOUNTED_VIEWS
}

/**
 * A view REGISTERED with its own component (`panel-registry/contracts.ts::
 * registerPanel`): `view:<name>`, rendered by `ViewHost` like a mounted view,
 * never a panel kind (the kind ceiling holds). U-9: the Watch surface's three.
 */
export type RegisteredViewId = `view:${string}`

export type ViewId = PanelKind | SlotId | MountedViewId | RegisteredViewId

export function isSlotId(id: string): id is SlotId {
  return id in SLOTS
}

/** A `view:` id that is not one of the mounted views above. */
export function isRegisteredViewId(id: string): id is RegisteredViewId {
  return id.startsWith('view:') && !isMountedViewId(id)
}

/** A view that is not a panel kind: a slot, a mounted view or a registered view (all render through the dock's `slot` component). */
export function isFrameView(id: string): id is SlotId | MountedViewId | RegisteredViewId {
  return isSlotId(id) || isMountedViewId(id) || isRegisteredViewId(id)
}

/** The tab title of a slot or mounted view. */
export function frameViewTitle(id: SlotId | MountedViewId): string {
  return isMountedViewId(id) ? MOUNTED_VIEWS[id].title : SLOTS[id].title
}

export interface SlotDef {
  title: string
  /** What will be here, in one or two plain sentences. */
  what: string
  /** What it waits on. */
  waits: string
}

export const SLOTS: Readonly<Record<SlotId, SlotDef>> = {
  'slot:subject_forecasts': {
    title: 'Forecasts here',
    what: 'The open forecasts on this subject, with their due dates, and the numeric series that belong to it.',
    waits: 'a subject filter on the forecast ledger (the Ledger surface shows every forecast, unfiltered) and the series route',
  },
  'slot:coverage': {
    title: 'Coverage',
    what: 'One cell per subject, with the blanks named: looked and could not see, or not covered at all.',
    waits: 'the coverage-per-subject route (layout design §4, item 5)',
  },
  'slot:subject_field': {
    title: 'Subjects',
    what: 'This profile declares no map geometry, so the subject field (a tile per subject, with the same change and blank marks a map would carry) stands in for the map.',
    waits: 'nothing — built; the region renders the field itself',
  },
}

export type RegionId = 'a' | 'b' | 'c'

export interface RegionDef {
  id: RegionId
  /** The views this region holds, front first. A region is never empty. */
  views: ViewId[]
  /** Its share of the panel side's width when it has a column of its own (relative). */
  weight: number
}

export interface SurfaceDef {
  id: SurfaceId
  side: Side
  label: string
  /** The one job, in a sentence (the tab's title, ⌘K's search text). */
  question: string
  /** Alt+<key> switches to it. */
  key: number
  /** What the stage shows when the page opens (its first breadcrumb). */
  stage: ViewId
  /** The panel side's seeded regions, the lead first. */
  regions: RegionDef[]
  /** Whether the chat starts open (about half the centre) or as the ask bar alone. */
  chat: 'open' | 'collapsed'
  /** Where an opened panel-home view with no seeded home on this page lands. */
  openRegion: RegionId
}

/** The panel side holds at most three regions (the reader may add up to this). */
export const MAX_REGIONS = 3
/** A page SEEDS at most two panel regions (was three regions + a fixed Consult column). */
export const MAX_SEEDED_REGIONS = 2

function capitalised(word: string): string {
  return word ? word.charAt(0).toUpperCase() + word.slice(1) : word
}

const DECLARED_SURFACES: readonly SurfaceDef[] = [
  // ── READER ──────────────────────────────────────────────────────────────
  {
    id: 'board',
    side: 'reader',
    label: 'Board',
    question: 'What changed, ranked.',
    key: 1,
    // The landing: the ranked list of what changed on the stage, the chat
    // open under it, and the map alone on the panel side, full height, with
    // its coverage strip under it. Nothing else competes on a first look; the
    // full daily read is one link from the list, and an opened panel joins
    // the map's region as a tab.
    stage: BOARD_LIST_VIEW,
    regions: [{ id: 'a', views: ['v4.map'], weight: 1 }],
    chat: 'open',
    openRegion: 'a',
  },
  {
    id: 'subject',
    side: 'reader',
    label: capitalised(PROFILE_UI.subject.singular),
    question: `What is established about one ${PROFILE_UI.subject.singular.toLowerCase()}, and what moved.`,
    key: 2,
    // The subject's page on the stage, following the point; the map beside
    // it, marking the subject. (No placeholder region: a region that waits on
    // something unbuilt is not part of the default.)
    stage: 'subject.page',
    regions: [{ id: 'a', views: ['v4.map'], weight: 1 }],
    chat: 'open',
    openRegion: 'a',
  },
  {
    id: 'ledger',
    side: 'reader',
    label: 'Ledger',
    question: 'What was expected, what happened, and how good this system is.',
    key: 3,
    // U-11: the forecasts on the stage; the track record (behind its
    // five-resolution gate) and the evaluation record beside them. The
    // roster-wide Accuracy page is the operator's (Evaluation).
    stage: 'view:forecasts',
    regions: [
      { id: 'a', views: ['view:track_record'], weight: 1 },
      { id: 'b', views: ['view:proof'], weight: 1 },
    ],
    chat: 'open',
    openRegion: 'a',
  },
  {
    id: 'watch',
    side: 'reader',
    label: 'Watch',
    question: 'Did anything happen that I asked to be told about?',
    key: 4,
    // U-9: the one alert log on the stage; the silences and the pages
    // changed after they were read beside it. The source-health table (with
    // its re-check flags) and the watches' own definitions are the
    // operator's (Engine).
    stage: 'view:watch_alerts',
    regions: [
      { id: 'a', views: ['view:watch_silences'], weight: 1 },
      { id: 'b', views: ['view:watch_edits'], weight: 1 },
    ],
    chat: 'open',
    openRegion: 'a',
  },
  // ── OPERATOR ────────────────────────────────────────────────────────────
  {
    id: 'engine',
    side: 'operator',
    label: 'Engine',
    question: 'Is the machine running, and what is it configured as?',
    key: 5,
    stage: 'system.status',
    regions: [
      { id: 'a', views: ['system.registry', 'system.flow', 'system.settings', 'system.source_health'], weight: 3 },
      { id: 'b', views: ['system.ledgers', 'system.alerts_watches'], weight: 2 },
    ],
    chat: 'collapsed',
    openRegion: 'a',
  },
  {
    id: 'evaluation',
    side: 'operator',
    label: 'Evaluation',
    question: 'How the checking machinery itself is doing: judge, boards, divergence, contentions.',
    key: 6,
    stage: 'system.eval_boards',
    regions: [
      { id: 'a', views: ['system.read_scoreboard', 'system.judge_stats', 'system.layer_divergence'], weight: 1 },
      { id: 'b', views: ['analysis.contentions', 'system.eval_scorecard'], weight: 1 },
    ],
    chat: 'collapsed',
    openRegion: 'a',
  },
  {
    id: 'gate',
    side: 'operator',
    label: 'Gate',
    question: 'Work the human queues: journal proposals, forecast resolutions, grading, optimizer candidates.',
    key: 7,
    stage: 'system.journal_gate',
    regions: [
      { id: 'a', views: ['view:resolutions', 'system.goldset', 'system.optimizer'], weight: 1 },
      { id: 'b', views: ['system.journal', 'system.report_export'], weight: 1 },
    ],
    chat: 'collapsed',
    openRegion: 'a',
  },
  {
    id: 'investigate',
    side: 'operator',
    label: 'Investigate',
    question: 'Follow one thing to the bottom: the feed, its evidence, its entities.',
    key: 8,
    stage: 'system.findings',
    regions: [
      { id: 'a', views: ['system.provenance', 'system.register', 'system.graph_walk'], weight: 3 },
      { id: 'b', views: ['system.entities', 'system.timeline'], weight: 2 },
    ],
    chat: 'collapsed',
    openRegion: 'a',
  },
]

/**
 * The pages THIS PROFILE mounts. Under `map.geometry: none` no region holds a
 * map: the view is dropped, and a region it leaves empty takes the subject
 * field's slot instead — a page never lands on a blank region.
 */
export function surfacesForProfile(defs: readonly SurfaceDef[] = DECLARED_SURFACES, maps = mapSurfaceDeclared()): SurfaceDef[] {
  if (maps) return [...defs]
  return defs.map((s) => ({
    ...s,
    regions: s.regions.map((r) => {
      const views = r.views.filter((v) => !isMapSurfaceKind(v))
      return { ...r, views: views.length > 0 ? views : ['slot:subject_field' as const] }
    }),
  }))
}

export const SURFACES: readonly SurfaceDef[] = surfacesForProfile()

export const READER_SURFACES = SURFACES.filter((s) => s.side === 'reader')
export const OPERATOR_SURFACES = SURFACES.filter((s) => s.side === 'operator')

/** Where the app opens. */
export const LANDING_SURFACE: SurfaceId = 'board'

export function findSurface(id: string): SurfaceDef | undefined {
  return SURFACES.find((s) => s.id === id)
}

export function isSurfaceId(id: string): id is SurfaceId {
  return SURFACES.some((s) => s.id === id)
}

/**
 * Any arrangement word → the page it now means: a page id, or a retired
 * workspace, preset or mission id (the legacy tables). Null when none matches.
 */
export function resolveSurfaceId(id: string): SurfaceId | null {
  if (isSurfaceId(id)) return id
  return legacyWorkspaceSurface(id) ?? legacyPresetSurface(id) ?? legacyMissionSurface(id)
}

/** The distinct views a page's panel side seeds, in region order. */
export function surfaceViews(def: SurfaceDef): ViewId[] {
  return def.regions.flatMap((r) => r.views)
}

/** Every view a page shows by default: the stage, then the panel side. */
export function pageViews(def: SurfaceDef): ViewId[] {
  return [def.stage, ...surfaceViews(def)]
}

/**
 * Whether a view renders in `mode`: a slot always does; a kind when it is
 * registered and ships in the mode. A view that does not ship is left out of
 * the seed (its region re-anchors) and is never counted as "missing".
 */
export function viewShipsIn(view: ViewId, mode: string): boolean {
  if (isFrameView(view)) return true
  const d = PANEL_REGISTRY[view as PanelKind]?.definition
  return !!d && (d.modes.length === 0 || d.modes.includes(mode as never))
}

/** The registered panel kinds a page shows by default (slots and mounted views excluded). */
export function surfaceKinds(def: SurfaceDef): PanelKind[] {
  return pageViews(def).filter((v): v is PanelKind => !isFrameView(v))
}

// ---------------------------------------------------------------------------
// The reader side's reach (the ratchet in surfaces.test.ts).
// ---------------------------------------------------------------------------

/**
 * Every panel kind a reader can reach WITHOUT crossing to the operator side:
 * the reader pages' stages and panels, Consult and the Record sheet. The
 * reader's `＋` catalog lists exactly these; ⌘K takes anything else across.
 */
export function readerKinds(): PanelKind[] {
  const out = new Set<PanelKind>()
  for (const s of READER_SURFACES) for (const k of surfaceKinds(s)) out.add(k)
  out.add('system.consult')
  out.add('system.inspector')
  // The full daily read is no page's default any more, but a reader opens it
  // from the Board's list.
  out.add(DAILY_READ_VIEW)
  return [...out]
}

export function isReaderKind(kind: string): boolean {
  return readerKinds().includes(kind as PanelKind)
}

/** A reader view that is not a panel kind (a slot, the forecast ledger). */
export function isReaderFrameView(id: string): boolean {
  return isFrameView(id) && READER_SURFACES.some((s) => pageViews(s).includes(id as ViewId))
}

// ---------------------------------------------------------------------------
// Where an open goes — the stage, or the view's home region.
// ---------------------------------------------------------------------------

export type Place = 'stage' | RegionId

export interface Home {
  surface: SurfaceId
  /** The stage, or a region of the panel side. */
  place: Place
  /** The view is the page's own (its stage default or a seeded view), not an opened one. */
  seeded: boolean
}

/**
 * The home of a view, opened while the reader stands on `current`:
 *
 *   1. the current page's stage default or a view its panel side seeds →
 *      there (the stage, or its region);
 *   2. a view of the current side (any reader view on a reader page; anything
 *      on the operator side) → this page: the STAGE when the view's contract
 *      says it is page-sized (a subject, the read, the forecasts, a record),
 *      else this page's open region;
 *   3. an operator view from a reader page → the operator section that shows
 *      it, else the page its contract names (`page`), else Investigate (the
 *      reader side never hosts engine views). A view whose contract names a
 *      page opens there from any page.
 */
export function homeOf(kind: string, current: SurfaceId, surfaces: readonly SurfaceDef[] = SURFACES): Home {
  const here = surfaces.find((s) => s.id === current)
  if (here) {
    if (here.stage === kind) return { surface: here.id, place: 'stage', seeded: true }
    const r = here.regions.find((x) => x.views.includes(kind as ViewId))
    if (r) return { surface: here.id, place: r.id, seeded: true }
  }
  const contract = contractOf(kind)
  // A reader view: a reader page's own, or one registered with a reader contract.
  const reader = isReaderKind(kind) || isReaderFrameView(kind) || (!PANEL_REGISTRY[kind as PanelKind] && contract?.side === 'reader')
  const stageHome = contract?.home === 'stage'
  // An operator view that names its page (the Users page: Engine) opens there, from anywhere.
  const named = !reader && contract?.page ? surfaces.find((s) => s.id === contract.page && s.side === 'operator') : undefined
  if (named) return { surface: named.id, place: stageHome ? 'stage' : named.openRegion, seeded: false }
  if (here && (here.side === 'operator' || reader)) {
    return { surface: here.id, place: stageHome ? 'stage' : here.openRegion, seeded: false }
  }
  for (const s of surfaces) {
    if (s.side !== 'operator') continue
    if (s.stage === kind) return { surface: s.id, place: 'stage', seeded: true }
    const r = s.regions.find((x) => x.views.includes(kind as ViewId))
    if (r) return { surface: s.id, place: r.id, seeded: true }
  }
  const inv = surfaces.find((s) => s.id === 'investigate')!
  return { surface: inv.id, place: stageHome ? 'stage' : inv.openRegion, seeded: false }
}

// ---------------------------------------------------------------------------
// Persistence — one versioned slot per page per mode, plus Reset.
// ---------------------------------------------------------------------------

/** The one key the shell writes. The retired keys are listed below; it never reads them. */
export const SURFACE_STORE_KEY = 'legba_surfaces'

/**
 * Keys the retired arrangement machinery wrote. The shell NEVER READS them (so
 * a browser holding them lands on the new default with nothing hidden); they
 * are left in place, not deleted, because they are the reader's own data.
 */
export const RETIRED_LAYOUT_KEYS = [
  'legba_ws',
  'legba_ws_retired',
  'legba_layout_custom',
  'legba_user_missions',
  'legba_theme',
] as const

/** FNV-1a 32-bit, hex — a stable, dependency-free fingerprint. */
function fnv1a(text: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h.toString(16).padStart(8, '0')
}

/**
 * THE VERSION STAMP of a page's stored arrangement: a hash of its stage, its
 * regions and their views, and the chat's default. Derived, never hand-bumped,
 * so it changes exactly when the default changes — and a stored arrangement
 * from before that change is discarded for the new default rather than merged.
 * The `s2-` prefix is the U-6 frame: every `s1-` slot the previous shell wrote
 * (three regions and a fixed Consult column) is stale by construction.
 */
export function surfaceLayoutVersion(def: SurfaceDef): string {
  const regions = def.regions.map((r) => `${r.id}:${r.views.join(',')}*${r.weight}`).join(';')
  return `s2-${fnv1a(`stage=${def.stage}|${regions}|chat=${def.chat}`)}`
}

export interface SurfaceSlot {
  version: string
  layout: SerializedDockview
  /** Seeded views the reader closed (they stay closed; anything else missing is a reset). */
  closed?: string[]
}

type SurfaceStore = Record<string, Record<string, SurfaceSlot>>

function readStore(): SurfaceStore {
  try {
    const raw = localStorage.getItem(SURFACE_STORE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as unknown
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? (parsed as SurfaceStore) : {}
  } catch {
    return {}
  }
}

function writeStore(store: SurfaceStore): void {
  try {
    localStorage.setItem(SURFACE_STORE_KEY, JSON.stringify(store))
  } catch {
    // Storage refused (private mode, quota): the arrangement is a per-viewer
    // convenience and the default is always one Reset away.
  }
}

/** Store the live panel-side arrangement of `surface` for `mode`. */
export function saveSurfaceLayout(
  surface: SurfaceId,
  mode: string,
  layout: SerializedDockview,
  closed: readonly string[] = [],
): void {
  const def = findSurface(surface)
  if (!def) return
  const store = readStore()
  ;(store[surface] ??= {})[mode] = {
    version: surfaceLayoutVersion(def),
    layout,
    ...(closed.length ? { closed: [...new Set(closed)] } : {}),
  }
  writeStore(store)
}

/** Drop `surface`'s stored arrangement for `mode` (Reset). */
export function resetSurfaceLayout(surface: SurfaceId, mode: string): void {
  const store = readStore()
  if (!store[surface]) return
  delete store[surface][mode]
  if (Object.keys(store[surface]).length === 0) delete store[surface]
  writeStore(store)
}

/** The panel ids a serialized layout holds, and how many grid regions. */
export function layoutCensus(layout: SerializedDockview): { ids: Set<string>; regions: number } {
  const ids = new Set<string>(Object.keys((layout as { panels?: Record<string, unknown> }).panels ?? {}))
  let regions = 0
  const walk = (node: unknown) => {
    if (!node || typeof node !== 'object') return
    const n = node as { type?: string; data?: unknown }
    if (n.type === 'leaf') {
      regions++
      return
    }
    if (Array.isArray(n.data)) for (const c of n.data) walk(c)
  }
  walk((layout as { grid?: { root?: unknown } }).grid?.root)
  return { ids, regions }
}

export type StoredLayoutRead =
  | { ok: true; layout: SerializedDockview; closed: string[] }
  | { ok: false; reason: 'none' }
  | { ok: false; reason: 'stale' | 'unreadable' | 'missing_view' | 'too_many_regions'; note: string }

/**
 * Read `surface`'s stored arrangement for `mode`, VALIDATED. A stored layout is
 * used only when ALL of these hold; otherwise it is dropped (and the caller
 * seeds the default and shows the one-line note):
 *
 *   - it carries this page's current version stamp;
 *   - it survives the alias pre-pass (`rewriteSerializedLayout`);
 *   - every seeded view is still in it or was closed by the reader (nothing
 *     the default shows is hidden by anything but the reader's own close);
 *   - it holds at most {@link MAX_REGIONS} grid regions.
 */
export function readSurfaceLayout(surface: SurfaceId, mode: string): StoredLayoutRead {
  const def = findSurface(surface)
  if (!def) return { ok: false, reason: 'none' }
  const slot = readStore()[surface]?.[mode]
  if (!slot || !slot.layout) return { ok: false, reason: 'none' }
  const label = def.label
  if (slot.version !== surfaceLayoutVersion(def)) {
    resetSurfaceLayout(surface, mode)
    return { ok: false, reason: 'stale', note: `${label} was reset to its new default` }
  }
  let layout: SerializedDockview
  try {
    const rewrite = rewriteSerializedLayout(slot.layout)
    layout = rewrite.layout
  } catch {
    resetSurfaceLayout(surface, mode)
    return { ok: false, reason: 'unreadable', note: `${label}'s stored arrangement could not be read — back to its default` }
  }
  const closed = Array.isArray(slot.closed) ? slot.closed.filter((x): x is string => typeof x === 'string') : []
  const census = layoutCensus(layout)
  const missing = surfaceViews(def).filter((v) => !census.ids.has(v) && !closed.includes(v) && viewShipsIn(v, mode))
  if (missing.length > 0) {
    resetSurfaceLayout(surface, mode)
    return { ok: false, reason: 'missing_view', note: `${label} was reset to its default so no view stays hidden` }
  }
  if (census.regions > MAX_REGIONS) {
    resetSurfaceLayout(surface, mode)
    return { ok: false, reason: 'too_many_regions', note: `${label} was reset to its default (at most ${MAX_REGIONS} panel regions)` }
  }
  return { ok: true, layout, closed }
}
