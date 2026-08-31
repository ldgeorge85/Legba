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
  // Per-target (T1–T10)
  | 'target.overview'
  | 'target.signals'
  | 'target.findings'
  | 'target.situations'
  | 'target.sources'
  | 'target.map'
  | 'target.graph'
  | 'target.timeline'
  | 'target.claims'
  // Per-analyst (A1–A5)
  | 'analyst.runs'
  | 'analyst.outputs'
  | 'analyst.cross_target'
  | 'analyst.critiques'
  // Cross-target dashboards
  //   (D1–D3 system.targets.roster / system.pulse / dashboard.dynamic
  //    DELETED in S7-T2 panel consolidation — roster→registry.targets,
  //    no pulse correlator, no dynamic widgets)
  // Operator (O1–O5)
  | 'registry.targets'
  | 'registry.analysts'
  | 'registry.stack'
  | 'registry.action_packs'
  //   (registry.wirings / registry.mutations / registry.discovery DELETED —
  //    empty wiring table, superseded mutations queue, 0 discovery descriptors)
  // System (S1–S8 + P-1 cross-target findings feed)
  | 'system.findings'
  | 'system.budget'
  | 'system.optimizer'
  | 'system.dead_letter'
  | 'system.consult'
  | 'system.settings'
  //   (system.eval→system.eval_scorecard, system.runtime→system.actor_health,
  //    system.streams/users/backfill DELETED in S7-T2 consolidation)
  // System Status — at-a-glance per-layer health (acquisition/analysis/queues/infra)
  | 'system.status'
  // The Timeline — validity-window temporal view (P4-4): facts/situations/
  // findings as ranged items ([valid_from, valid_until) / lifecycle /
  // [produced_at, superseded_at)) + supersession-chain edges, brushable/zoomable
  | 'system.timeline'
  // The Wall — the mission-control anchor tile (P1-7): banded-verdict desk
  // grid + movers-since-last-visit (/v3/since) + newest high-severity
  // verified + a system-health corner, one glanceable 2×2 screen
  | 'system.wall'
  // U-4 (COHERENCE_WAVES_PLAN_2026-07-28) — a standalone mount of JUST the
  // Wall's movers-since-last-visit quadrant. The landing workspace now mounts
  // the WALL itself (which carries that quadrant), so this tile is no longer
  // boot-seeded; it stays registered + hidden (registry.ts HIDDEN_KINDS) so a
  // saved layout holding it keeps rendering, and ⌘K can still open it.
  | 'system.wall_movers'
  // Entity knowledge-graph (UI-3 — source-first analogue of v2's entity KG)
  | 'system.entities'
  // K-G4 — the graph WALK: anchored ego expansion over the reified
  // `entity_edges` store, one interactive hop per click, with per-edge
  // evidence. Distinct from system.entity_graph (the older `proposed_edges`
  // top-N subgraph projection); this is the surface under the operator's
  // "walking the world graph" vision.
  | 'system.graph_walk'
  // Source-first surfaces (UI-2 / Tier C — pivot)
  | 'registry.sources'
  | 'source.detail'
  | 'source.subscription_builder'
  | 'source.subscription_policy'
  | 'source.fanout'
  // Eval + Ops (UI-5 / Tiers E+F — appended)
  | 'system.eval_scorecard'
  // Correctness gold-set weekly labeling worksheet (P2-5) — the operator
  // judges ~8 sampled verified findings/week; verdicts grow the scoreboard n
  | 'system.goldset'
  | 'system.optimizer.diff'
  | 'system.governor'
  | 'system.audit'
  | 'system.stream_lag'
  | 'system.actor_health'
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
  // consumer at all, plus the one new API the track shipped. All four land in
  // Engine Room, whose rows fold behind a single collapsed header and so cost
  // nothing against the ≤23 visible-row budget (navGroups.test.ts) that the
  // GLASS-2 additions had already spent to the last row.
  //
  // The production gauge — the whole-engine "did it produce what its descriptor
  // promised" read (`/v3/system/production-gauge`), including the integrity and
  // metering bricks and the SAME `pages` predicate the alert plane uses.
  | 'system.production_gauge'
  // Judge verdict mix by SERVING PROVIDER (`/v3/system/judge-stats`) — the
  // track's one new backend surface, making `served_by` provider drift visible.
  | 'system.judge_stats'
  // Source health rollup — `/v3/source-quality` + `/v3/system/staleness-debt`,
  // with the per-source `/quality` drill-down.
  | 'system.source_health'
  // The three eval boards that had live routes and no reader:
  // `/v3/eval/desk_baselines`, `/band_trajectory`, `/analyst_runtime`.
  | 'system.eval_boards'
  // THE READ SCOREBOARD (D2e) — `/read-events/rollup`. The only panel that
  // measures the OPERATOR rather than the engine: reads today / this week and
  // the morning-read day count the 90-day oracle wager is graded on.
  | 'system.read_scoreboard'
  // v4 visual workspace panels (geotemporal / flow / provenance)
  //   (v4.case Casework Board DELETED in S7-T2 — shelved, no pin board reachable)
  | 'v4.map'
  | 'v4.assessment'
  // Mission-control default-layout surfaces (S7-T2): the KPI glance strip and
  // the global banded Timeline lanes — self-fetching singletons.
  | 'v4.kpi'
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
  /** Whether the panel only renders when bound to a descriptor (T1-T10, A1-A5). */
  requiresBinding: boolean
  /** Sidebar icon name (lucide-react). */
  iconName?: string
  /**
   * When true, the panel is registered (so existing layouts that
   * reference it don't crash) but hidden from the sidebar and the
   * always-on singleton list.  Used for panels dropped by the §6
   * redesign — they stay in the bundle (deep links still resolve) but
   * don't clutter the operator's daily-driver surface.
   */
  hidden?: boolean
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
   * `operations` (registries, runtime health, plumbing). Optional: when
   * omitted, the bucket is derived from the panel's nav group
   * (`productForKind` in panel-registry/navGroups.ts). Set explicitly only to
   * override that derivation.
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
