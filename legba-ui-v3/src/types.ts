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
  | 'system.lineage'
  | 'system.budget'
  | 'system.optimizer'
  | 'system.dead_letter'
  | 'system.consult'
  | 'system.deep_consult'
  | 'system.settings'
  //   (system.eval→system.eval_scorecard, system.runtime→system.actor_health,
  //    system.streams/users/backfill DELETED in S7-T2 consolidation)
  // System Status — at-a-glance per-layer health (acquisition/analysis/queues/infra)
  | 'system.status'
  // Entity knowledge-graph (UI-3 — source-first analogue of v2's entity KG)
  | 'system.entities'
  | 'system.entity_graph'
  // Notable-structure overlay (#99 — ranked interesting graph-structure shortlist)
  | 'system.notable_structure'
  // Source-first surfaces (UI-2 / Tier C — pivot)
  | 'registry.sources'
  | 'source.detail'
  | 'source.subscription_builder'
  | 'source.subscription_policy'
  | 'source.fanout'
  // Eval + Ops (UI-5 / Tiers E+F — appended)
  | 'system.eval_scorecard'
  | 'system.optimizer.diff'
  | 'system.governor'
  | 'system.audit'
  | 'system.stream_lag'
  | 'system.actor_health'
  // Product surfaces (UI-6 / Tier G — pivot)
  | 'system.search'
  | 'system.alert_center'
  // Escalation-delivery audit edge (audit finding C3 / decision D1): renders
  // alert_sink_deliveries so a human SEES whether an escalation landed or
  // went nowhere. Distinct from system.alert_center (the localStorage
  // subscription watchlist over the findings feed).
  | 'system.escalations'
  | 'system.report_export'
  //   (system.tenant_view DELETED — multitenancy is ingestion-only, not baked)
  // The Inspector — the unified detail surface (redesign Move 1, the keystone)
  | 'system.inspector'
  // The Journal — Legba's reflective voice + navigable index over the product
  // (JOURNAL_ASSESSOR_PLAN §9, Wave 3)
  | 'system.journal'
  // v4 visual workspace panels (geotemporal / flow / provenance)
  //   (v4.case Casework Board DELETED in S7-T2 — shelved, no pin board reachable)
  | 'v4.map'
  | 'v4.flow'
  | 'v4.why'
  | 'v4.assessment'
  // Mission-control default-layout surfaces (S7-T2): the KPI glance strip and
  // the global banded Timeline lanes — self-fetching singletons.
  | 'v4.kpi'
  | 'v4.timeline'

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
}

/** JWT scope claims — populated by `auth/jwt.ts`. */
export interface AuthClaims {
  sub: string
  mode: Mode
  roles: ReadonlyArray<'admin' | 'analyst' | 'viewer' | 'operator'>
  exp: number
}
