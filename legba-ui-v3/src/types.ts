/**
 * Shared types for the L-204 panel set.
 *
 * `PanelRegistration` mirrors the asyncpg row dataclass at
 * `src/legba/data/outputs/ui_panel.py::PanelRegistration` 1:1 — when the
 * backend REST surface adds a field, mirror it here.
 *
 * `PanelKind` enumerates every panel module we ship in the bundle-time
 * registry (per L-108 §2). New panels add a row here AND a row in
 * `panel-registry/registry.ts`.
 */

/** Deployment mode per M-036. The backend persists snake_case wire form. */
export type Mode = 'personal' | 'above_ai' | 'cis'

/** L-108 §2 PanelKind union — every L-092 panel listed. */
export type PanelKind =
  // The SUBJECT PAGE (UI-B3, UI-D1 Table A) — one bound page per subject of
  // any frame kind, tabbed Brief · Evidence · Claims · Signals · Situations ·
  // Sources · Graph · Runtime. The eight per-subject reads it absorbed —
  // target.{overview,signals,findings,claims,situations,sources,graph,
  // desk_brief_page} — are no longer in this union: they RETIRED into
  // `panel-registry/aliases.ts`, each landing on its tab.
  | 'subject.page'
  // Per-target reads still pending their own survivors (Table B: the map and
  // the clock, with a subject param)
  | 'target.map'
  | 'target.timeline'
  // Per-analyst (A1–A5)
  | 'analyst.runs'
  | 'analyst.outputs'
  | 'analyst.cross_target'
  | 'analyst.critiques'
  // Cross-target dashboards
  //   (D1–D3 system.targets.roster / system.pulse / dashboard.dynamic
  //    DELETED in S7-T2 panel consolidation — roster→registry.targets,
  //    no pulse correlator, no dynamic widgets)
  // Operator — the Engine's merged wrappers (UI-B1/merges, UI-D1 Table A). The
  // ids they absorbed — registry.{targets,analysts,sources,stack,action_packs},
  // source.{detail,subscription_builder,subscription_policy,fanout},
  // system.{budget,governor,audit,dead_letter,actor_health,stream_lag,
  // production_gauge,optimizer.diff,wall_movers,navigator}, v4.kpi — are no
  // longer in this union: they RETIRED into `panel-registry/aliases.ts`, each
  // landing on its survivor's tab.
  //   (registry.wirings / registry.mutations / registry.discovery DELETED —
  //    empty wiring table, superseded mutations queue, 0 discovery descriptors)
  // The descriptor families: Subjects · Units · Sources · Stack · Packs ·
  // Builder · Policy · Canvas
  | 'system.registry'
  // N-J — the Flow: a READ-ONLY canvas over the descriptors (what each agent
  // reads and writes, as declared; what a real run did). Not the Registry's
  // Canvas tab (the v4 wiring editor): this one derives every edge from a
  // declared field and edits nothing.
  | 'system.flow'
  // The append-only engine ledgers: Governor · Audit · Budget
  | 'system.ledgers'
  // The frame register: Situations · Narratives
  | 'system.register'
  // System (S1–S8 + P-1 cross-target findings feed)
  | 'system.findings'
  | 'system.optimizer'
  | 'system.consult'
  | 'system.settings'
  //   (system.eval→system.eval_scorecard, system.runtime→system.actor_health,
  //    system.streams/users/backfill DELETED in S7-T2 consolidation)
  // System Status — per-layer health + Production · Failures · Actors · Queues
  | 'system.status'
  // The Timeline — validity-window temporal view (P4-4): facts/situations/
  // findings as ranged items ([valid_from, valid_until) / lifecycle /
  // [produced_at, superseded_at)) + supersession-chain edges, brushable/zoomable
  | 'system.timeline'
  // The Wall — the mission-control anchor tile (P1-7): banded-verdict desk
  // grid + movers-since-last-visit (/v3/since) + newest high-severity
  // verified + a system-health corner, one glanceable 2×2 screen; the
  // At-a-Glance band above it and a Movers tab (UI-B1/merges)
  | 'system.wall'
  // Entity knowledge-graph (UI-3 — source-first analogue of v2's entity KG)
  | 'system.entities'
  // K-G4 — the graph WALK: anchored ego expansion over the reified
  // `entity_edges` store, one interactive hop per click, with per-edge
  // evidence. Distinct from system.entity_graph (the older `proposed_edges`
  // top-N subgraph projection); this is the surface under the operator's
  // "walking the world graph" vision.
  | 'system.graph_walk'
  // Eval + Ops (UI-5 / Tiers E+F — appended)
  | 'system.eval_scorecard'
  // Correctness gold-set weekly labeling worksheet (P2-5) — the operator
  // judges ~8 sampled verified findings/week; verdicts grow the scoreboard n
  | 'system.goldset'
  // Product surfaces (UI-6 / Tier G — pivot)
  | 'system.search'
  | 'system.report_export'
  //   (system.tenant_view DELETED — multitenancy is ingestion-only, not baked)
  // The Inspector — the unified detail surface (redesign Move 1, the keystone)
  | 'system.inspector'
  // The Journal — Legba's reflective voice + navigable index over the product
  // (JOURNAL_ASSESSOR_PLAN §9, Wave 3)
  | 'system.journal'
  // GLASS-2 — the three API surfaces that shipped with no consumer.
  //
  // The Journal Gate (JOURNAL_ASSESSOR_PLAN §7.4/§7.5): the operator surface for
  // `journal_proposals` accept/reject. Journal writes are human-gated by
  // standing rule, and the gate was API-only until this kind existed.
  | 'system.journal_gate'
  // GLASS-3 — the ops deck. Four kinds over seven server surfaces that had no
  // consumer at all, plus the one new API the track shipped. (The production
  // gauge is System Status' Production tab since UI-B1/merges.)
  //
  // Judge verdict mix by SERVING PROVIDER (`/v3/system/judge-stats`) — the
  // track's one new backend surface, making `served_by` provider drift visible.
  | 'system.judge_stats'
  // Source health rollup — `/v3/source-quality` + `/v3/system/staleness-debt`,
  // with the per-source `/quality` drill-down.
  | 'system.source_health'
  // The three eval boards that had live routes and no reader:
  // `/v3/eval/desk_baselines`, `/band_trajectory`, `/analyst_runtime`.
  | 'system.eval_boards'
  // THE DIVERGENCE MAP (7b-v) — `/v3/layers/divergence`. Program 6 L2's reader
  // surface: the six source layers per country with their aperture
  // declaration, and the three layer-to-layer gaps with the z against each
  // pair's own rolling baseline. The one checking surface the next-arc capture
  // named that had no reader at all.
  | 'system.layer_divergence'
  // CROSS-FRAMING (wave P lane B) — one claim against the units that touch it.
  // The FIRST kind outside the five existing families, and deliberately so: it
  // is scoped to a CLAIM rather than to a target, an analyst or the system, so
  // filing it under `system.` would have described a reading about the world as
  // a piece of plumbing. Its five sections ride five routes the UI already
  // calls — `/findings`, `/v3/contentions`, `/v3/absence`,
  // `/v3/layers/divergence`, `/journal?kind=lens` — and it adds none.
  | 'analysis.cross_framing'
  // CONTENTIONS (W2 S-batch) — the contrary-evidence pass's records as a LIST,
  // fleet-wide or by desk, with each row's stance, host class and fences.
  // `/v3/contentions` only; it adds no route.
  | 'analysis.contentions'
  // THE READ SCOREBOARD (D2e) — `/read-events/rollup`. The only panel that
  // measures the OPERATOR rather than the engine: reads today / this week and
  // the morning-read day count the 90-day oracle wager is graded on.
  | 'system.read_scoreboard'
  // v4 visual workspace panels (geotemporal / flow / provenance)
  //   (v4.case Casework Board DELETED in S7-T2 — shelved, no pin board reachable)
  | 'v4.map'
  | 'v4.assessment'
  // The composition-demotion reading surface (D-4): the quoted record, its
  // evidence map, its published selection and the fenced Assessment channel.
  | 'v4.morning_read'
  // U-3 merges (COHERENCE_WAVES_PLAN_2026-07-28 §U-3) — one panel kind per
  // merge set. The folded-away originals (v4.why / system.lineage / v4.flow /
  // system.situations / system.narratives, system.alert_center /
  // system.watchlist / system.escalations, system.deep_consult, v4.timeline,
  // system.entity_graph / system.notable_structure) no longer appear in this
  // union at all: they RETIRED into `panel-registry/aliases.ts`, which resolves
  // an old saved-layout id / ⌘K deep-link onto the survivor and the tab that
  // renders it (UI_HOLISTIC_DESIGN_2026-08-24 §4.4). A retired kind costs one
  // line of data instead of a registry row.
  | 'system.provenance'
  | 'system.alerts_watches'

export type PanelCategory =
  | 'target'
  | 'analyst'
  | 'dashboard'
  | 'operator'
  | 'system'

export type ScopeKey = 'target_id' | 'analyst_id' | 'dashboard_id' | null

/**
 * Mirrors `PanelRegistration` from `src/legba/data/outputs/ui_panel.py`.
 *
 * The backend REST handler at `GET /api/v1/registry/ui_panels?mode=<mode>`
 * returns an array of these.
 */
export interface PanelRegistration {
  id: string
  panel_id: string // logical id like "target_overview" (no `panels.` prefix)
  descriptor_id: string
  descriptor_version: string
  descriptor_family: 'target' | 'analyst'
  analyst_id: string | null
  title: string
  mode: Mode
  layout_slot: string
  data_query: Record<string, unknown>
  binding: Record<string, unknown>
  retired: boolean
  created_at: string
  retired_at: string | null
}

/**
 * Bundle-time registration of one panel kind. The `component` is the
 * React component that renders panels of this kind; the panel-loader
 * dispatches on `panel_id`.
 */
export interface PanelKindDefinition {
  /** Stable kind string (e.g. "target.overview"). */
  kind: PanelKind
  /** Logical id descriptors reference (e.g. "target_overview"). */
  panelId: string
  /** Display category — drives sidebar grouping. */
  category: PanelCategory
  /** Modes the panel ships in. Empty array = bundle-stripped. */
  modes: readonly Mode[]
  /** Scope axis (per-target, per-analyst, etc.) or null for singletons. */
  scopeKey: ScopeKey
  /** Human-readable default title — overridden by registration row. */
  defaultTitle: string
  /**
   * One line saying what the tile is FOR — the `＋` catalog prints it under
   * the name (X3/D2). Data lives in `panel-registry/descriptions.ts`.
   */
  description?: string
  /** Whether the panel only renders when bound to a descriptor (T1-T10, A1-A5). */
  requiresBinding: boolean
  /** Sidebar icon name (lucide-react). */
  iconName?: string
  /**
   * Release tier — drives the chrome badge so a `preview` surface is
   * visually distinct from a `live` one.  Defaults to `live` (omitted in
   * the registry for the common case).  A `preview` panel is one whose
   * backend route is a guarded preview / honest pending-state (e.g. the
   * backfill 501) or whose data is operator-experimental — registered and
   * usable but not part of the everyday product surface.  See `docs/UI.md`
   * §"Panel tiers" for the classification.
   */
  tier?: 'preview' | 'live'
  /**
   * #89 — product bucket for the two-section sidebar: `intelligence` (the
   * analytical product — assessments, findings, entities, why, map) vs
   * `operations` (registries, runtime health, plumbing). Optional, and read
   * by nothing since UI-B1/shell retired the nav tree (`navGroups.ts` and its
   * `productForKind` derivation are gone); kept so a definition that sets it
   * still type-checks.
   */
  product_group?: 'intelligence' | 'operations'
}

/** Props passed to each panel component. */
export interface PanelProps {
  registration: PanelRegistration
  /** Resolved scope value — depends on the panel's scopeKey. */
  scope: {
    target_id?: string
    analyst_id?: string
    dashboard_id?: string
  }
  /** Current deployment mode for cross-cutting concerns. */
  mode: Mode
  /**
   * Which tab/mode a TABBED panel should open on.
   *
   * Set when a RETIRED kind resolved onto this panel through the alias table
   * (`panel-registry/aliases.ts`): opening `system.watchlist` must land on
   * Alerts & Watches' "Watches" tab, not its default. Also honored by a
   * workspace seed that wants a specific tab up front. Ignored by panels with
   * no tabs, and an unrecognized value falls back to the panel's own default —
   * a stale tab name must never render an empty surface.
   */
  initialTab?: string
}

/** JWT scope claims — populated by `auth/jwt.ts`. */
export interface AuthClaims {
  sub: string
  mode: Mode
  roles: ReadonlyArray<'admin' | 'analyst' | 'viewer' | 'operator'>
  exp: number
}
