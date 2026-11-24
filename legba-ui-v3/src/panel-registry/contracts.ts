/**
 * contracts — THE PANEL CONTRACT (U-6; 02_UI_TARGET_BRIEF §12: "every panel
 * declares what Consult can do to it").
 *
 * The registry (`registry.ts`) knows a kind and its component. A CONTRACT says,
 * in one place per view, what the frame and Consult need to know to place it
 * and drive it:
 *
 *   - a stable id (the view id: a registry kind, or a frame view such as
 *     `view:forecasts`), a human name and a one-line description;
 *   - a catalog group (the ＋ catalog lists by group and searches the text);
 *   - the object types it can be BOUND to (subject, record, source,
 *     forecast, window, place, unit, alert);
 *   - its size class and its home — the STAGE (a page-sized read) or a PANEL
 *     column;
 *   - the ACTIONS it accepts, each with typed arguments. Every view accepts
 *     open / close / focus; the rest are declared per view.
 *
 * `lib/panelActions.ts` is the one router: a click, a link and a Consult
 * action reach a view only through {@link contractOf} and the actions listed
 * here, and a view action is undoable.
 *
 * ADDING A VIEW is one call — {@link registerPanel} with its contract and, for
 * a view that is not a registry kind, its component. The frame renders any
 * registered view through `components/ViewHost.tsx` and never names a kind.
 */
import type { ComponentType } from 'react'
import { PROFILE_UI } from '@/lib/profileUi'
import { GLOBE_LAYERS } from '@/v4/globe/catalog'

/** What a view can be bound to — the point kinds of the brief's §4 table. */
export type BindType = 'subject' | 'record' | 'source' | 'forecast' | 'window' | 'place' | 'unit' | 'alert'

/** How much room a view wants: a page-sized read, a column, or a small card. */
export type SizeClass = 'stage' | 'column' | 'compact'

/** Where an open of the view lands by default. */
export type ViewHome = 'stage' | 'panel'

export type ArgType = 'string' | 'boolean' | 'number' | 'ref' | 'enum'

export interface ActionArg {
  name: string
  type: ArgType
  required?: boolean
  /** The allowed values of an `enum`. */
  values?: readonly string[]
  /** For a `ref`: the ref kinds it takes (`target`, `finding`, …). */
  refKinds?: readonly string[]
  describe: string
}

export interface PanelActionSpec {
  id: string
  label: string
  args: readonly ActionArg[]
}

export type CatalogGroup = 'Read' | 'Subjects' | 'Forecasts and proof' | 'Watch' | 'Records' | 'Map' | 'Operator' | 'Other'

export interface PanelContract {
  /** The view id (a registry kind or a frame view id). Stable; never reused. */
  id: string
  name: string
  /** One line: what the view is for. */
  description: string
  group: CatalogGroup
  side: 'reader' | 'operator'
  binds: readonly BindType[]
  size: SizeClass
  home: ViewHome
  /** The actions beyond the three every view accepts. */
  actions: readonly PanelActionSpec[]
}

/** The three actions every view accepts. */
export const COMMON_ACTIONS: readonly PanelActionSpec[] = [
  {
    id: 'open',
    label: 'Open',
    args: [{ name: 'place', type: 'enum', values: ['stage', 'panel'], describe: 'where it opens; its home when absent' }],
  },
  { id: 'close', label: 'Close', args: [] },
  { id: 'focus', label: 'Bring to the front', args: [] },
]

// ---------------------------------------------------------------------------
// Reusable action specs.
// ---------------------------------------------------------------------------

const SPANS = ['6h', '24h', '7d', '30d'] as const

export const SET_WINDOW: PanelActionSpec = {
  id: 'set_window',
  label: 'Set the window',
  args: [{ name: 'span', type: 'enum', values: SPANS, required: true, describe: 'the window ending now' }],
}

export const SELECT: PanelActionSpec = {
  id: 'select',
  label: 'Select a record',
  args: [
    {
      name: 'ref',
      type: 'ref',
      required: true,
      refKinds: ['finding', 'signal', 'event', 'situation', 'report', 'entity', 'source', 'analyst', 'journal_entry'],
      describe: 'the record to select; the Record sheet follows',
    },
  ],
}

/**
 * U-10 — open THE RECORD SHEET on one record: the statement as published, the
 * row(s) it maps to, the evidence, the receipt of what was fetched, later
 * versions and the integrity line (`components/record/RecordChain.tsx`).
 * `cite` names one cited sentence of a finding (its marker's ordinal).
 */
export const OPEN_RECORD: PanelActionSpec = {
  id: 'open_record',
  label: 'Open the record',
  args: [
    {
      name: 'ref',
      type: 'ref',
      required: true,
      refKinds: ['finding', 'signal', 'event', 'situation', 'fact', 'hypothesis', 'observation'],
      describe: 'the record whose chain to show (statement, rows, evidence, receipt, integrity)',
    },
    { name: 'cite', type: 'number', describe: "one cited sentence of a finding: its citation marker's number" },
  ],
}

function setTab(values: readonly string[]): PanelActionSpec {
  return {
    id: 'set_tab',
    label: 'Show a tab',
    args: [{ name: 'tab', type: 'enum', values, required: true, describe: 'the tab to show' }],
  }
}

/** U-9 — the Watch alert log's kind groups and severity floors (`v4/watch/model.ts`). */
export const WATCH_ALERT_GROUPS = ['reader', 'all', 'fired', 'quiet', 'changed', 'desk', 'forecast', 'system'] as const
export const WATCH_SEVERITY_FLOORS = ['any', 'info', 'low', 'medium', 'high', 'critical'] as const

const BIND_SUBJECT: PanelActionSpec = {
  id: 'bind',
  label: 'Bind to a subject',
  args: [{ name: 'subject', type: 'ref', required: true, refKinds: ['target'], describe: 'the subject to read' }],
}

/**
 * THE MAP'S LAYER IDS (U-7) — the globe's own rows (`v4/globe/catalog.ts`),
 * the ids the layer card, the legend and `globeActions.setLayer` use. A row no
 * route serves yet (drift) is left out: Consult cannot turn on what the map
 * cannot draw; the arg's description names it and what it waits on.
 */
export const MAP_LAYER_IDS: readonly string[] = GLOBE_LAYERS.filter((l) => !l.unavailable).map((l) => l.id)
const MAP_LAYERS_DESCRIBED = GLOBE_LAYERS.filter((l) => !l.unavailable)
  .map((l) => `${l.id} (${l.label.toLowerCase()})`)
  .join('; ')
const MAP_LAYERS_UNAVAILABLE = GLOBE_LAYERS.filter((l) => l.unavailable)
  .map((l) => `${l.id} — not available: needs ${l.unavailable}`)
  .join('; ')

/** The record and place kinds the map can fly to and select. */
const MAP_REF_KINDS = ['target', 'finding', 'signal', 'event', 'situation', 'entity', 'place'] as const

// ---------------------------------------------------------------------------
// The declared contracts — the reader side (and the frame's own views).
// ---------------------------------------------------------------------------

const subjectNoun = PROFILE_UI.subject.singular

export const READER_CONTRACTS: readonly PanelContract[] = [
  {
    id: 'v4.morning_read',
    name: "Today's read",
    description: 'What changed since you last looked, ranked, with the sentences that carry it.',
    group: 'Read',
    side: 'reader',
    binds: ['window'],
    size: 'stage',
    home: 'stage',
    actions: [SET_WINDOW, SELECT],
  },
  {
    id: 'subject.page',
    name: subjectNoun,
    description: `One ${subjectNoun.toLowerCase()}: what is established, what moved, its evidence and sources.`,
    group: 'Subjects',
    side: 'reader',
    binds: ['subject'],
    size: 'stage',
    home: 'stage',
    actions: [BIND_SUBJECT, setTab(['brief', 'evidence', 'claims', 'signals', 'situations', 'sources', 'graph', 'runtime']), SELECT],
  },
  {
    id: 'view:forecasts',
    name: 'Forecasts',
    description: 'Every sealed forecast: its probability, due date, the rule that settles it, its state and its seal.',
    group: 'Forecasts and proof',
    side: 'reader',
    binds: ['forecast', 'window'],
    size: 'stage',
    home: 'stage',
    actions: [
      SET_WINDOW,
      {
        id: 'show_forecast',
        label: 'Show one forecast',
        args: [{ name: 'forecast', type: 'string', required: true, describe: "the forecast's slug; it becomes the point and is scrolled into view" }],
      },
      {
        id: 'filter',
        label: 'Show open, resolved or all forecasts',
        args: [{ name: 'show', type: 'enum', values: ['open', 'resolved', 'all'], required: true, describe: 'which forecasts the list shows' }],
      },
    ],
  },
  {
    id: 'view:track_record',
    name: 'Track record',
    description: 'How good the forecasts have been: no score until five have resolved, then Brier, calibration and every outcome.',
    group: 'Forecasts and proof',
    side: 'reader',
    binds: [],
    size: 'column',
    home: 'panel',
    actions: [],
  },
  {
    id: 'view:proof',
    name: 'Evaluations',
    description: 'Standing evaluations such as the blind test: arms, scores with intervals and sample sizes, caveats, deviations.',
    group: 'Forecasts and proof',
    side: 'reader',
    binds: [],
    size: 'column',
    home: 'panel',
    actions: [
      {
        id: 'show_run',
        label: 'Show one evaluation run',
        args: [{ name: 'run', type: 'string', required: true, describe: "the run's id; it moves to the top" }],
      },
    ],
  },
  {
    id: 'system.eval_scorecard',
    name: 'Accuracy',
    description: 'How good this system is: the track record, losses included.',
    group: 'Forecasts and proof',
    side: 'reader',
    binds: [],
    size: 'column',
    home: 'panel',
    actions: [],
  },
  {
    id: 'system.alerts_watches',
    name: 'Alerts and watches',
    description: 'What you asked to be told about, what fired, and what was delivered.',
    group: 'Watch',
    side: 'reader',
    binds: ['alert'],
    size: 'column',
    home: 'panel',
    actions: [setTab(['watches', 'triggers', 'deliveries']), SELECT],
  },
  {
    id: 'system.source_health',
    name: 'Source health',
    description: 'Which sources are answering, which are quiet, and since when.',
    group: 'Watch',
    side: 'reader',
    binds: ['source'],
    size: 'column',
    home: 'panel',
    actions: [SELECT],
  },
  // U-9 — the Watch surface's three views (`v4/watch/`, components attached
  // by `v4/watch/register.tsx`): the alert log on the stage, the silences and
  // the pages changed after they were read in the panel regions.
  {
    id: 'view:watch_alerts',
    name: 'Alert log',
    description: 'What fired and when, its kind and severity, and whether it paged or was only logged, and by which rule.',
    group: 'Watch',
    side: 'reader',
    binds: ['alert', 'source', 'window'],
    size: 'stage',
    home: 'stage',
    actions: [
      SET_WINDOW,
      {
        id: 'filter_alerts',
        label: 'Filter the alert log',
        args: [
          { name: 'group', type: 'enum', values: WATCH_ALERT_GROUPS, describe: 'reader (every kind but the system own), all, or one kind group' },
          { name: 'severity', type: 'enum', values: WATCH_SEVERITY_FLOORS, describe: 'at least this severity, or any' },
          { name: 'paged', type: 'enum', values: ['any', 'paged', 'logged'], describe: 'only what paged, only what was logged, or either' },
          { name: 'state', type: 'enum', values: ['any', 'open', 'closed'], describe: 'only open episodes, only closed ones, or either' },
        ],
      },
      {
        id: 'select_alert',
        label: 'Focus one alert',
        args: [{ name: 'alert', type: 'string', required: true, describe: 'the alert id on screen; its subject becomes the point' }],
      },
    ],
  },
  {
    id: 'view:watch_silences',
    name: 'Silences',
    description: 'Sources and units that are quiet, and why: looked and saw nothing, or could not look.',
    group: 'Watch',
    side: 'reader',
    binds: ['source'],
    size: 'column',
    home: 'panel',
    actions: [
      {
        id: 'filter_silences',
        label: 'Show one side of the silences',
        args: [{ name: 'side', type: 'enum', values: ['any', 'looked', 'could_not_look', 'unknown'], required: true, describe: 'looked and saw nothing, could not look, cannot tell yet, or all' }],
      },
      SELECT,
    ],
  },
  {
    id: 'view:watch_edits',
    name: 'Changed after read',
    description: 'Pages relied on that changed after they were read: what changed, how the source said so, what relied on it, both versions.',
    group: 'Watch',
    side: 'reader',
    binds: ['source'],
    size: 'column',
    home: 'panel',
    actions: [
      setTab(['flags', 'record']),
      {
        id: 'open_flag',
        label: 'Open one flag with both versions',
        args: [{ name: 'flag', type: 'string', required: true, describe: 'the flag id on screen' }],
      },
    ],
  },
  {
    id: 'system.wall',
    name: 'At a glance',
    description: 'The band of headline figures and the movers since your last visit.',
    group: 'Read',
    side: 'reader',
    binds: ['window'],
    size: 'column',
    home: 'panel',
    actions: [setTab(['overview', 'movers']), SET_WINDOW],
  },
  {
    id: 'system.inspector',
    name: 'Record',
    description: 'One record and why to believe it: its sources, its lineage, its checks.',
    group: 'Records',
    side: 'reader',
    binds: ['record', 'source'],
    size: 'column',
    home: 'stage',
    actions: [SELECT, OPEN_RECORD],
  },
  {
    // THE MAP — the contract only (the map's code belongs to its own lane):
    // every action reaches the map through the stores it already follows.
    id: 'v4.map',
    name: PROFILE_UI.map.title,
    description: 'Where things are happening: changes, blanks and the point on the map.',
    group: 'Map',
    side: 'reader',
    binds: ['place', 'subject', 'window'],
    size: 'column',
    home: 'panel',
    // U-7: the ONE map contract. Every action maps onto `globeActions`
    // (`v4/globe/actions.ts`) through `v4/globe/contract.ts`, so a request
    // made while the map is hidden is kept by the stores and applied when it
    // is shown; each is undoable through the frame's router (the layers, the
    // camera, the window and the point are all in the undo snapshot).
    actions: [
      {
        id: 'set_layer',
        label: 'Turn a map layer on or off',
        args: [
          {
            name: 'layer',
            type: 'enum',
            values: MAP_LAYER_IDS,
            required: true,
            describe: `the layer: ${MAP_LAYERS_DESCRIBED}${MAP_LAYERS_UNAVAILABLE ? `. Not listed: ${MAP_LAYERS_UNAVAILABLE}` : ''}`,
          },
          { name: 'on', type: 'boolean', required: true, describe: 'on (true) or off (false)' },
        ],
      },
      {
        id: 'fly_to',
        label: 'Fly the map to a place, subject, record or area',
        args: [
          {
            name: 'ref',
            type: 'ref',
            refKinds: MAP_REF_KINDS,
            describe: 'a subject (target), a record or a series place (`place`, e.g. portwatch:chokepoint4) — it becomes the point and the camera fits it',
          },
          { name: 'lon', type: 'number', describe: 'a position to centre on, with lat (degrees)' },
          { name: 'lat', type: 'number', describe: 'a position to centre on, with lon (degrees)' },
          { name: 'zoom', type: 'number', describe: 'the zoom for a position (0.6–8); kept when absent' },
          { name: 'west', type: 'number', describe: 'an area to fit, with south, east and north (degrees)' },
          { name: 'south', type: 'number', describe: 'an area to fit' },
          { name: 'east', type: 'number', describe: 'an area to fit' },
          { name: 'north', type: 'number', describe: 'an area to fit' },
        ],
      },
      { id: 'home', label: 'Back to the home view', args: [] },
      {
        id: 'set_window',
        label: 'Set the window',
        args: [
          { name: 'span', type: 'enum', values: SPANS, describe: 'the window ending now' },
          { name: 'from', type: 'string', describe: 'the window start (ISO 8601), with to' },
          { name: 'to', type: 'string', describe: 'the window end (ISO 8601), with from' },
        ],
      },
      {
        id: 'select',
        label: 'Make a record or place the point',
        args: [{ name: 'ref', type: 'ref', required: true, refKinds: MAP_REF_KINDS, describe: 'the record, subject or place; the Record sheet and Consult follow' }],
      },
    ],
  },
  {
    id: 'system.consult',
    name: 'Consult',
    description: 'Ask; answers cite the records they used and can pull them up for you.',
    group: 'Read',
    side: 'reader',
    binds: ['subject', 'record', 'window'],
    size: 'column',
    home: 'panel',
    actions: [],
  },
]

/** The frame's slots (honest empty regions): opened, closed, never driven further. */
export const SLOT_CONTRACTS: readonly PanelContract[] = [
  {
    id: 'slot:coverage',
    name: 'Coverage',
    description: 'One cell per subject, with the blanks named — not built yet; says what it waits on.',
    group: 'Read',
    side: 'reader',
    binds: [],
    size: 'compact',
    home: 'panel',
    actions: [],
  },
  {
    id: 'slot:subject_forecasts',
    name: 'Forecasts here',
    description: `The open forecasts on this ${subjectNoun.toLowerCase()} — not built yet; says what it waits on.`,
    group: 'Forecasts and proof',
    side: 'reader',
    binds: ['subject'],
    size: 'compact',
    home: 'panel',
    actions: [],
  },
  {
    id: 'slot:subject_field',
    name: 'Subjects',
    description: 'A tile per subject where a profile declares no map — not built yet; says what it waits on.',
    group: 'Map',
    side: 'reader',
    binds: [],
    size: 'column',
    home: 'panel',
    actions: [],
  },
]

/**
 * Operator-side views that are not registry kinds (U-11): declared here so
 * the frame can place them and the server's catalogue can check Consult's
 * actions on them (`scripts/gen_panel_action_catalog.py` reads this array).
 */
export const OPERATOR_CONTRACTS: readonly PanelContract[] = [
  {
    id: 'view:resolutions',
    name: 'Forecast resolutions',
    description: 'Proposed forecast resolutions awaiting a person, with the evidence the resolver cites; read-only.',
    group: 'Operator',
    side: 'operator',
    binds: ['forecast'],
    size: 'column',
    home: 'panel',
    actions: [
      {
        id: 'show_forecast',
        label: 'Open one proposal',
        args: [{ name: 'forecast', type: 'string', required: true, describe: "the forecast's slug; its proposal and evidence open" }],
      },
    ],
  },
]

/**
 * The operator-side views that still carry NO declared contract (they get the
 * three common actions and the default home only). Listed so the gap is a
 * named list, not an accident; `contracts.test.ts` keeps it honest.
 */
export const OPERATOR_KINDS_WITHOUT_CONTRACT: readonly string[] = [
  'system.status',
  'system.registry',
  'system.flow',
  'system.settings',
  'system.ledgers',
  'system.eval_boards',
  'system.read_scoreboard',
  'system.judge_stats',
  'system.layer_divergence',
  'analysis.contentions',
  'system.journal_gate',
  'system.goldset',
  'system.optimizer',
  'system.journal',
  'system.report_export',
  'system.findings',
  'system.provenance',
  'system.register',
  'system.graph_walk',
  'system.entities',
  'system.timeline',
  'system.search',
]

// ---------------------------------------------------------------------------
// The registry of contracts — the one place a view is registered.
// ---------------------------------------------------------------------------

/** A view's component when it is not a registry kind (props: the instance's binding). */
export type FrameViewComponent = ComponentType<{ binding: Record<string, string> | null; instanceId: string }>

interface Entry {
  contract: PanelContract
  Component?: FrameViewComponent
}

const entries = new Map<string, Entry>()
const listeners = new Set<() => void>()

/**
 * Register a view: its contract and — for a view that is not a registry kind —
 * the component the frame renders. Re-registering an id replaces it (HMR, a
 * test). Returns the unregister function.
 */
export function registerPanel(contract: PanelContract, Component?: FrameViewComponent): () => void {
  entries.set(contract.id, { contract, Component })
  for (const l of listeners) l()
  return () => {
    if (entries.get(contract.id)?.contract === contract) {
      entries.delete(contract.id)
      for (const l of listeners) l()
    }
  }
}

for (const c of [...READER_CONTRACTS, ...SLOT_CONTRACTS, ...OPERATOR_CONTRACTS]) registerPanel(c)

/** The view id of an instance id: `subject.page:brazil` → `subject.page`. */
export function viewIdOf(instanceId: string): string {
  if (entries.has(instanceId)) return instanceId
  const at = instanceId.indexOf(':')
  if (at > 0) {
    const head = instanceId.slice(0, at)
    // `slot:…` and `view:…` are whole ids; `<kind>:<record>` is a bound instance.
    if (head !== 'slot' && head !== 'view') return head
  }
  return instanceId
}

/** The declared contract of a view (or instance), or undefined. */
export function declaredContract(id: string): PanelContract | undefined {
  return entries.get(id)?.contract ?? entries.get(viewIdOf(id))?.contract
}

/** A registered view's own component (frame views only). */
export function frameViewComponent(id: string): FrameViewComponent | undefined {
  return entries.get(id)?.Component ?? entries.get(viewIdOf(id))?.Component
}

/** Every registered contract, in registration order. */
export function allContracts(): PanelContract[] {
  return [...entries.values()].map((e) => e.contract)
}

/** Subscribe to registrations (the catalog re-lists). */
export function onContractsChanged(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

const BIND_UNIT: PanelActionSpec = {
  id: 'bind',
  label: 'Bind to a unit',
  args: [{ name: 'subject', type: 'ref', required: true, refKinds: ['analyst'], describe: 'the unit to read' }],
}

/**
 * Whether an action is one the view accepts: the common three, its own, and
 * — for a view that binds to a subject or a unit — `bind`.
 */
export function acceptedAction(c: PanelContract, actionId: string): PanelActionSpec | undefined {
  const own = COMMON_ACTIONS.find((a) => a.id === actionId) ?? c.actions.find((a) => a.id === actionId)
  if (own || actionId !== 'bind') return own
  if (c.binds.includes('subject')) return BIND_SUBJECT
  if (c.binds.includes('unit')) return BIND_UNIT
  return undefined
}

export type ArgCheck = { ok: true; args: Record<string, unknown> } | { ok: false; reason: string }

/** Check an action's arguments against its spec (types, enums, ref kinds, required). */
export function checkArgs(spec: PanelActionSpec, args: Record<string, unknown>): ArgCheck {
  const out: Record<string, unknown> = {}
  for (const a of spec.args) {
    const v = args[a.name]
    if (v === undefined || v === null || v === '') {
      if (a.required) return { ok: false, reason: `missing ${a.name}` }
      continue
    }
    switch (a.type) {
      case 'string':
        if (typeof v !== 'string') return { ok: false, reason: `${a.name} must be text` }
        break
      case 'boolean':
        if (typeof v !== 'boolean') return { ok: false, reason: `${a.name} must be true or false` }
        break
      case 'number':
        if (typeof v !== 'number' || !Number.isFinite(v)) return { ok: false, reason: `${a.name} must be a number` }
        break
      case 'enum':
        if (typeof v !== 'string' || !(a.values ?? []).includes(v)) {
          return { ok: false, reason: `${a.name} must be one of ${(a.values ?? []).join(', ')}` }
        }
        break
      case 'ref': {
        const r = v as { kind?: unknown; id?: unknown }
        if (!r || typeof r !== 'object' || typeof r.kind !== 'string' || typeof r.id !== 'string' || !r.id) {
          return { ok: false, reason: `${a.name} must be a {kind, id} ref` }
        }
        if (a.refKinds && !a.refKinds.includes(r.kind)) {
          return { ok: false, reason: `${a.name} takes ${a.refKinds.join(' or ')}, not ${r.kind}` }
        }
        break
      }
    }
    out[a.name] = v
  }
  return { ok: true, args: out }
}
