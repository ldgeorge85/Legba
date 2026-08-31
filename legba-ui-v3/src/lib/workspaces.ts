/**
 * Workspaces — the STANCE model (UI_HOLISTIC_DESIGN_2026-08-24 §2).
 *
 * A workspace is not a preset and not a route. It is a named, keyboard-switchable
 * arrangement of tiles curated for ONE reason to open the app, and — the part the
 * old `LAYOUT_PRESETS` got wrong — it is an OBJECT YOU RETURN TO, not a reset:
 *
 *   - switching SERIALIZES the outgoing workspace into its own slot first, so
 *     nothing you arranged is ever destroyed (`applyPreset` calls `api.clear()`
 *     before re-seeding; that is the one behavioural bug that made the presets
 *     unusable as workspaces — design §2.5);
 *   - each stance remembers itself per mode: `legba_ws[<workspace>][<mode>]`,
 *     versioned as `{ schemaVersion, layout }` because Dockview's
 *     `SerializedDockview` carries no version field of its own (design §6.2);
 *   - first entry seeds from a curated default (below) which the operator then
 *     bends — Datadog's out-of-the-box-content pattern, never a blank dock.
 *
 * The six stances and the question each answers (design §2.2):
 *
 *   1 MORNING READ  what happened, what moved, what does it mean?   Alt+1  (landing)
 *   2 DESK          everything about the one place/lane I selected  Alt+2
 *   3 INVESTIGATE   follow this one thing to the bottom             Alt+3
 *   4 TRUST         is what it produced any good?                   Alt+4
 *   5 THE GATE      work the human queues                           Alt+5
 *   6 ENGINE        is the machine running, and configured as what? Alt+6
 *
 * SEEDS USE TODAY'S KINDS. The design's §4 subtraction (67 → 25 kinds:
 * `target.desk`, `system.registry`, `system.engine`, `system.ledgers`,
 * `system.evidence`, `system.register`) rides the later merge trains; this
 * module deliberately points at the kinds that exist NOW so the stance model
 * ships with zero registry churn. When a merge train lands, the seed row
 * changes and the alias table (panel-registry/aliases.ts) keeps every saved
 * workspace slot resolving.
 *
 * LEGACY LAYOUTS ARE NEVER CLOBBERED. `legba_layout_custom` (the single
 * Save/Restore slot the sidebar's Layouts menu owns) is read once, COPIED into
 * the Morning Read slot on a first boot that has no workspace store at all, and
 * otherwise left completely alone — Save/Restore keep working exactly as before.
 */

import type { DockviewApi, SerializedDockview } from 'dockview-react'
import type { PanelKind } from '@/types'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import type { PresetPlacement } from '@/lib/layoutPresets'
import { rewriteSerializedLayout } from '@/panel-registry/aliases'

/** The six stances. Stable ids — they key the persistence slots. */
export type WorkspaceId = 'morning_read' | 'desk' | 'investigate' | 'trust' | 'gate' | 'engine'

/** Where the app opens: the landing stance (design §3.1 — "Morning Read, seeded, in one paint"). */
export const LANDING_WORKSPACE: WorkspaceId = 'morning_read'

/**
 * A post-seed size nudge. Dockview splits ~50/50 by default; these pin the
 * proportions the stance was drawn for (the successor of `sizeMissionControl`'s
 * hardcoded boot arithmetic, now colocated with the layout it sizes).
 */
export interface WorkspaceSizeHint {
  kind: PanelKind
  /** Absolute pixel height for a full-width band. */
  height?: number
  /** Fraction of the dock width (0–1) for a rail. */
  widthFraction?: number
}

export interface WorkspaceDef {
  id: WorkspaceId
  /** Tab label on the workspace bar. */
  label: string
  /** The question the stance answers — the bar's tooltip, and ⌘K's search text. */
  question: string
  /** 1-based position; the hotkey is Alt+<index>. */
  index: number
  /** Curated default arrangement, seeded on first entry (and on reset). */
  seed: PresetPlacement[]
  /** Panels to activate after seeding (the default-active tab of their group). */
  active?: PanelKind[]
  /** Proportions to pin after seeding. */
  sizes?: WorkspaceSizeHint[]
}

/**
 * The six curated defaults.
 *
 * Every seed is an ordered placement list with the same contract the layout
 * presets use: entry 0 is the anchor (no position), every later entry splits
 * relative to a panel named EARLIER in the same list (`workspaces.test.ts`
 * asserts both, plus that every kind is a real, non-binding singleton).
 */
export const WORKSPACES: readonly WorkspaceDef[] = [
  {
    id: 'morning_read',
    label: 'Morning Read',
    question: 'What happened, what moved, what does it mean?',
    index: 1,
    // Design §2.4 #1: a "what changed" band across the top, the feed as the
    // left spine, the "what does it mean" reads tabbed on the right, the world
    // map beneath them.
    //
    // The Wall REPLACES the old boot grid's standalone `system.wall_movers`
    // tile: movers-since-last-visit is the Wall's own quadrant, and the boot
    // grid mounted it a second time beside its own parent (design §1.2). The
    // Wall carries it, the world-at-a-glance band grid, newest verified, and
    // the health corner in one band.
    seed: [
      { kind: 'v4.kpi' },
      { kind: 'system.wall', position: { referencePanel: 'v4.kpi', direction: 'below' } },
      { kind: 'system.findings', position: { referencePanel: 'system.wall', direction: 'below' } },
      {
        kind: 'v4.assessment',
        position: { referencePanel: 'system.findings', direction: 'right' },
      },
      {
        kind: 'system.alerts_watches',
        position: { referencePanel: 'v4.assessment', direction: 'within' },
      },
      {
        kind: 'system.inspector',
        position: { referencePanel: 'v4.assessment', direction: 'within' },
      },
      { kind: 'v4.map', position: { referencePanel: 'v4.assessment', direction: 'below' } },
    ],
    // The product one-pager is the default-active right tab (not the Inspector,
    // which peeks on the first selection); the feed anchors the left.
    active: ['v4.assessment', 'system.findings'],
    sizes: [
      { kind: 'v4.kpi', height: 100 },
      { kind: 'system.wall', height: 300 },
      { kind: 'v4.assessment', widthFraction: 0.34 },
    ],
  },
  {
    id: 'desk',
    label: 'Desk',
    question: 'Everything about the one place or lane I have selected.',
    index: 2,
    // Design §2.4 #2 — the bound stance. `target.desk` (nine `target.*` kinds
    // collapsed onto one selection-parameterized panel) is the merge train's
    // job; until then the Desk is exactly what already follows the shared
    // selection: the feed, the Inspector, the Timeline, and the provenance /
    // register reads for whatever desk is selected.
    seed: [
      { kind: 'system.findings' },
      {
        kind: 'system.inspector',
        position: { referencePanel: 'system.findings', direction: 'right' },
      },
      { kind: 'system.timeline', position: { referencePanel: 'system.findings', direction: 'below' } },
      {
        kind: 'system.provenance',
        position: { referencePanel: 'system.inspector', direction: 'below' },
      },
    ],
    active: ['system.findings'],
    sizes: [
      { kind: 'system.inspector', widthFraction: 0.35 },
      { kind: 'system.timeline', height: 220 },
    ],
  },
  {
    id: 'investigate',
    label: 'Investigate',
    question: 'Follow this one thing to the bottom.',
    index: 3,
    // Design §2.4 #3 — feed → inspector → evidence → consult, with the record
    // as the work (the Inspector is pinned wide here, not peeking).
    seed: [
      { kind: 'system.findings' },
      {
        kind: 'system.inspector',
        position: { referencePanel: 'system.findings', direction: 'right' },
      },
      { kind: 'system.entities', position: { referencePanel: 'system.findings', direction: 'below' } },
      {
        kind: 'system.provenance',
        position: { referencePanel: 'system.inspector', direction: 'below' },
      },
      { kind: 'system.consult', position: { referencePanel: 'system.entities', direction: 'within' } },
    ],
    active: ['system.findings', 'system.entities'],
    sizes: [{ kind: 'system.inspector', widthFraction: 0.38 }],
  },
  {
    id: 'trust',
    label: 'Trust',
    question: 'Is what it produced any good?',
    index: 4,
    // Design §2.4 #4 — the four GLASS-3 ops-deck panels, composed. They are
    // deliberately NOT tabbed together: you read "did it produce" beside "who
    // served the judge" at the same time, which is the whole point (§4.1's
    // merge test).
    seed: [
      { kind: 'system.production_gauge' },
      {
        kind: 'system.judge_stats',
        position: { referencePanel: 'system.production_gauge', direction: 'right' },
      },
      {
        kind: 'system.source_health',
        position: { referencePanel: 'system.production_gauge', direction: 'below' },
      },
      {
        kind: 'system.eval_boards',
        position: { referencePanel: 'system.judge_stats', direction: 'below' },
      },
      {
        kind: 'system.inspector',
        position: { referencePanel: 'system.eval_boards', direction: 'within' },
      },
    ],
    active: ['system.eval_boards'],
  },
  {
    id: 'gate',
    label: 'The Gate',
    question: 'Work the human queues — journal proposals, grading, optimizer candidates.',
    index: 5,
    // Design §2.4 #5 — three queues, one stance. The standing rule ("journal
    // writes are human-gated") gets a named front door instead of one row
    // inside a collapsed Engine Room.
    seed: [
      { kind: 'system.journal_gate' },
      {
        kind: 'system.goldset',
        position: { referencePanel: 'system.journal_gate', direction: 'right' },
      },
      { kind: 'system.optimizer', position: { referencePanel: 'system.goldset', direction: 'within' } },
      {
        kind: 'system.inspector',
        position: { referencePanel: 'system.goldset', direction: 'below' },
      },
      {
        kind: 'system.journal',
        position: { referencePanel: 'system.journal_gate', direction: 'below' },
      },
      {
        kind: 'system.report_export',
        position: { referencePanel: 'system.journal', direction: 'within' },
      },
    ],
    active: ['system.goldset', 'system.journal'],
    sizes: [{ kind: 'system.journal', height: 260 }],
  },
  {
    id: 'engine',
    label: 'Engine',
    question: 'Is the machine running, and what is it configured as?',
    index: 6,
    // Design §2.4 #6 — status + failures on the left, the registry families and
    // the append-only ledgers on the right. `system.registry` / `system.ledgers`
    // (9 → 1 and 3 → 1) are merge-train work; the tabs below are their shape.
    seed: [
      { kind: 'system.status' },
      { kind: 'system.dead_letter', position: { referencePanel: 'system.status', direction: 'below' } },
      {
        kind: 'registry.targets',
        position: { referencePanel: 'system.status', direction: 'right' },
      },
      {
        kind: 'registry.sources',
        position: { referencePanel: 'registry.targets', direction: 'within' },
      },
      {
        kind: 'system.settings',
        position: { referencePanel: 'registry.targets', direction: 'within' },
      },
      // The Inspector renders the selected descriptor's body (DescriptorView),
      // which is what makes a registry row clickable rather than a dead end.
      {
        kind: 'system.inspector',
        position: { referencePanel: 'registry.targets', direction: 'within' },
      },
      {
        kind: 'system.governor',
        position: { referencePanel: 'registry.targets', direction: 'below' },
      },
      { kind: 'system.audit', position: { referencePanel: 'system.governor', direction: 'within' } },
      { kind: 'system.budget', position: { referencePanel: 'system.governor', direction: 'within' } },
    ],
    active: ['registry.targets', 'system.governor'],
    sizes: [{ kind: 'system.dead_letter', height: 260 }],
  },
]

/** Lookup by id; undefined for an unknown id. */
export function findWorkspace(id: string): WorkspaceDef | undefined {
  return WORKSPACES.find((w) => w.id === id)
}

/** Whether a string is one of the six workspace ids (hash / storage validation). */
export function isWorkspaceId(id: string): id is WorkspaceId {
  return WORKSPACES.some((w) => w.id === id)
}

// ---------------------------------------------------------------------------
// Seeding.
// ---------------------------------------------------------------------------

/**
 * Opener signature — the SAME `addSingleton` App.tsx uses for the sidebar, ⌘K,
 * and the layout presets, so a seeded tile is indistinguishable from a
 * hand-opened one (mode gating included). Returns falsy when the kind was
 * skipped (not registered, or gated out of the active mode).
 */
export type WorkspaceOpener = (
  kind: PanelKind,
  position?: PresetPlacement['position'],
) => unknown | undefined

/**
 * Seed a workspace's curated default through the opener.
 *
 * Skips a placement whose reference panel never opened (mode gating can drop an
 * earlier tile — e.g. every operator-category panel in `cis` mode), re-anchoring
 * it instead of handing Dockview a position that names a panel which does not
 * exist. Returns the kinds actually opened, in order.
 */
export function seedWorkspace(def: WorkspaceDef, open: WorkspaceOpener): PanelKind[] {
  const opened: PanelKind[] = []
  for (const placement of def.seed) {
    if (!PANEL_REGISTRY[placement.kind]) continue
    const position =
      placement.position && opened.includes(placement.position.referencePanel)
        ? placement.position
        : undefined
    // The anchor (and any re-anchored placement) opens with no position.
    if (open(placement.kind, position)) opened.push(placement.kind)
  }
  return opened
}

// ---------------------------------------------------------------------------
// Persistence — one slot per workspace per mode, versioned.
// ---------------------------------------------------------------------------

const WS_KEY = 'legba_ws'
/** The single Save/Restore slot the sidebar's Layouts menu owns (legacy, read-only here). */
const LEGACY_KEY = 'legba_layout_custom'

/**
 * Slot schema version. v1 = the unversioned `legba_layout_custom[mode]` blob;
 * v2 = this shape. Dockview ships no layout versioning of its own (design
 * §6.4), so the wrapper is ours.
 */
export const WS_SCHEMA_VERSION = 2

export interface WorkspaceSlot {
  schemaVersion: number
  layout: SerializedDockview
}

/** `legba_ws` → workspace id → mode → slot. */
export type WorkspaceStore = Record<string, Record<string, WorkspaceSlot>>

function readStore(): WorkspaceStore {
  try {
    const raw = localStorage.getItem(WS_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as WorkspaceStore
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function writeStore(store: WorkspaceStore): void {
  try {
    localStorage.setItem(WS_KEY, JSON.stringify(store))
  } catch {
    // localStorage unavailable (private mode / quota) — persisting a workspace
    // is best-effort and must never surface as an error.
  }
}

/** Read the legacy single-slot custom layout for `mode` (never written to). */
function readLegacyLayout(mode: string): SerializedDockview | undefined {
  try {
    const raw = localStorage.getItem(LEGACY_KEY)
    if (!raw) return undefined
    const parsed = JSON.parse(raw) as Record<string, SerializedDockview>
    return parsed && typeof parsed === 'object' ? parsed[mode] : undefined
  } catch {
    return undefined
  }
}

/**
 * First-boot migration (design §6.2 rule 1): with NO workspace store at all, a
 * previously-saved custom layout is adopted as the Morning Read slot and
 * stamped v2 — so the operator's arrangement is what they land in, not a
 * curated default that overwrote it.
 *
 * COPY, never move: `legba_layout_custom` is left byte-identical, so the
 * sidebar's Save/Restore keeps behaving exactly as it did.
 */
export function migrateLegacyLayout(mode: string): boolean {
  const store = readStore()
  if (Object.keys(store).length > 0) return false
  const legacy = readLegacyLayout(mode)
  if (!legacy) return false
  store[LANDING_WORKSPACE] = { [mode]: { schemaVersion: WS_SCHEMA_VERSION, layout: legacy } }
  writeStore(store)
  return true
}

/** Serialize the live layout into `<workspace>`'s slot for `mode`. */
export function saveWorkspaceLayout(api: DockviewApi, ws: WorkspaceId, mode: string): void {
  try {
    const store = readStore()
    const byMode = (store[ws] ??= {})
    byMode[mode] = { schemaVersion: WS_SCHEMA_VERSION, layout: api.toJSON() }
    writeStore(store)
  } catch {
    // toJSON can throw on a half-torn-down dock; losing one autosave is
    // acceptable, crashing the switch is not.
  }
}

/** Whether `<workspace>` has a stored layout for `mode`. */
export function hasWorkspaceLayout(ws: WorkspaceId, mode: string): boolean {
  return readStore()[ws]?.[mode]?.layout != null
}

/**
 * Restore `<workspace>`'s stored layout for `mode`.
 *
 * Runs the alias pre-pass first (design §6.2 steps 2–3): stale panel ids are
 * rewritten onto their survivor, duplicates collapse onto one tile, and
 * anything with no alias and no registry entry is dropped BEFORE `fromJSON`
 * ever sees it — Dockview has no per-panel fallback for an unknown component
 * and would throw. Returns false when there is nothing renderable to restore,
 * which is the caller's signal to seed the curated default instead of leaving
 * the operator staring at an empty dock.
 */
export function loadWorkspaceLayout(api: DockviewApi, ws: WorkspaceId, mode: string): boolean {
  try {
    const slot = readStore()[ws]?.[mode]
    if (!slot?.layout) return false
    const rewrite = rewriteSerializedLayout(slot.layout)
    if (rewrite.empty) return false
    api.fromJSON(rewrite.layout)
    return true
  } catch {
    return false
  }
}

/** Drop `<workspace>`'s stored layout for `mode` (⌘K "Reset this workspace"). */
export function resetWorkspaceLayout(ws: WorkspaceId, mode: string): void {
  const store = readStore()
  if (!store[ws]) return
  delete store[ws][mode]
  if (Object.keys(store[ws]).length === 0) delete store[ws]
  writeStore(store)
}
