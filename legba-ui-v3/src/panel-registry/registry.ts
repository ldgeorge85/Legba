/**
 * Bundle-time panel registry.
 *
 * Per L-108 §1, two registries cooperate:
 *  - this one (static, declares every panel kind we ship), and
 *  - the runtime descriptor registry (dynamic, lists *instances*).
 *
 * Adding a new panel kind = adding a row here AND a component under
 * `src/panels/`. Adding a new panel instance (e.g. a new country target)
 * is descriptor-only — no code change.
 *
 * The map is keyed on `PanelKind` so the panel-loader dispatches in O(1).
 */

import { lazy } from 'react'
import type { ComponentType } from 'react'
import type { PanelKind, PanelKindDefinition, PanelProps } from '@/types'

export interface RegistryEntry {
  definition: PanelKindDefinition
  /** Lazy-loaded React component for the panel kind. */
  Component: ComponentType<PanelProps>
}

// ---------------------------------------------------------------------------
// Lazy component imports — one per panel module.
// ---------------------------------------------------------------------------

const TargetOverview = lazy(() => import('@/panels/target/Overview'))
const TargetSignals = lazy(() => import('@/panels/target/Signals'))
const TargetFindings = lazy(() => import('@/panels/target/Findings'))
const TargetSituations = lazy(() => import('@/panels/target/Situations'))
const TargetSources = lazy(() => import('@/panels/target/Sources'))
const TargetMap = lazy(() => import('@/panels/target/Map'))
const TargetGraph = lazy(() => import('@/panels/target/Graph'))
const TargetTimeline = lazy(() => import('@/panels/target/Timeline'))
const TargetClaims = lazy(() => import('@/panels/target/Claims'))

const AnalystRuns = lazy(() => import('@/panels/analyst/Runs'))
const AnalystOutputs = lazy(() => import('@/panels/analyst/Outputs'))
const AnalystCrossTarget = lazy(() => import('@/panels/analyst/CrossTarget'))
const AnalystCritiques = lazy(() => import('@/panels/analyst/Critiques'))

const SystemFindings = lazy(() => import('@/panels/system/Findings'))
const SystemBudget = lazy(() => import('@/panels/system/Budget'))
const SystemOptimizer = lazy(() => import('@/panels/system/Optimizer'))
const SystemDeadLetter = lazy(() => import('@/panels/system/DeadLetter'))
// U-3 merge — Deep Consult folds into a depth toggle on Consult; the merged
// wrapper mounts both original, unmodified components (merged/Consult.tsx).
const SystemConsultMerged = lazy(() => import('@/panels/merged/Consult'))
const SystemSettings = lazy(() => import('@/panels/system/Settings'))

const RegistryTargets = lazy(() => import('@/panels/registry/Targets'))
const RegistryAnalysts = lazy(() => import('@/panels/registry/Analysts'))
const RegistryStack = lazy(() => import('@/panels/registry/Stack'))
const RegistryActionPacks = lazy(() => import('@/panels/registry/ActionPacks'))

// Source-first surfaces (UI-2 / Tier C — pivot)
const SourceRegistry = lazy(() => import('@/panels/source/SourceRegistry'))
const SourceDetail = lazy(() => import('@/panels/source/SourceDetail'))
const SourceSubscriptionBuilder = lazy(() => import('@/panels/source/SubscriptionBuilder'))
const SourceSubscriptionPolicy = lazy(() => import('@/panels/source/SubscriptionPolicy'))
const SourceFanout = lazy(() => import('@/panels/source/FanoutExplorer'))

// Eval + Ops surfaces (UI-5 / Tiers E+F — appended)
const SystemEvalScorecard = lazy(() => import('@/panels/system/EvalScorecard'))
// Correctness gold-set weekly labeling worksheet (P2-5).
const SystemGoldset = lazy(() => import('@/panels/system/Goldset'))
const SystemOptimizerDiff = lazy(() => import('@/panels/system/OptimizerDiff'))
const SystemGovernorEvents = lazy(() => import('@/panels/system/GovernorEvents'))
const SystemAuditChain = lazy(() => import('@/panels/system/AuditChain'))
const SystemStreamLag = lazy(() => import('@/panels/system/StreamLag'))
const SystemActorHealth = lazy(() => import('@/panels/system/ActorHealth'))
// System Status — the at-a-glance per-layer health view (#89 ops surface).
const SystemStatus = lazy(() => import('@/panels/system/SystemStatus'))
// The Timeline — U-3 merge: one "Timeline" panel with an Events/Validity mode
// switch (merged/Timeline.tsx), folding v4.timeline (event lanes) + the
// original system.timeline (validity windows, imported inside the wrapper).
const SystemTimelineMerged = lazy(() => import('@/panels/merged/Timeline'))
// The Wall — the mission-control anchor tile (P1-7).
const SystemWall = lazy(() => import('@/panels/system/Wall'))
// U-4 — standalone mount of the Wall's movers-since-last-visit quadrant,
// boot-seeded so cold boot answers "what changed" (see types.ts / App.tsx).
const SystemWallMovers = lazy(() => import('@/panels/system/WallMovers'))
// Product surfaces (UI-6 / Tier G — pivot)
const SystemSearch = lazy(() => import('@/panels/system/Search'))
const SystemEntities = lazy(() => import('@/panels/system/Entities'))
const SystemGraphWalk = lazy(() => import('@/panels/system/GraphWalk'))
const SystemReportExport = lazy(() => import('@/panels/system/ReportExport'))

// The Inspector — unified detail surface (redesign Move 1, the keystone).
const SystemInspector = lazy(() => import('@/components/inspector/InspectorPanel'))

// The Journal — Legba's reflective voice + navigable index (Wave 3).
const SystemJournal = lazy(() => import('@/panels/system/Journal'))
// GLASS-2 — the three unconsumed API surfaces get their consumers.
const SystemJournalGate = lazy(() => import('@/panels/system/JournalGate'))
// GLASS-3 — the ops deck.
const SystemProductionGauge = lazy(() => import('@/panels/system/ProductionGauge'))
const SystemJudgeStats = lazy(() => import('@/panels/system/JudgeStats'))
const SystemSourceHealth = lazy(() => import('@/panels/system/SourceHealth'))
const SystemEvalBoards = lazy(() => import('@/panels/system/EvalBoards'))
const SystemReadScoreboard = lazy(() => import('@/panels/system/ReadScoreboard'))

// v4 visual workspace panels (selection-linked singletons).
const V4Map = lazy(() => import('@/panels/v4/MapPanel'))
const V4Assessment = lazy(() => import('@/panels/v4/AssessmentPanel'))
const V4Kpi = lazy(() => import('@/panels/v4/KpiPanel'))

// U-3 merge — Provenance folds v4.why + system.lineage + v4.flow into one
// tabbed surface (merged/Provenance.tsx mounts all three, unmodified).
const SystemProvenance = lazy(() => import('@/panels/merged/Provenance'))
// U-3 merge — Alerts & Watches folds system.watchlist + system.alert_center +
// system.escalations into one tabbed surface (merged/AlertsWatches.tsx).
const SystemAlertsWatches = lazy(() => import('@/panels/merged/AlertsWatches'))

// ---------------------------------------------------------------------------
// Registry table.
// ---------------------------------------------------------------------------

export const PANEL_REGISTRY: Record<PanelKind, RegistryEntry> = {
  // --- Target panels (T1–T10) ---
  'target.overview': {
    definition: def('target.overview', 'target_overview', 'target', 'target_id', 'Target Overview', true, ['personal', 'cis'], 'Target'),
    Component: TargetOverview,
  },
  'target.signals': {
    definition: def('target.signals', 'target_signals', 'target', 'target_id', 'Target Signals', true, ['personal'], 'Radio'),
    Component: TargetSignals,
  },
  'target.findings': {
    definition: def('target.findings', 'target_findings', 'target', 'target_id', 'Target Findings', true, ['personal', 'cis'], 'Flag'),
    Component: TargetFindings,
  },
  'target.situations': {
    definition: def('target.situations', 'target_situations', 'target', 'target_id', 'Target Situations', true, ['personal', 'cis'], 'AlertTriangle'),
    Component: TargetSituations,
  },
  'target.sources': {
    definition: def('target.sources', 'target_sources', 'target', 'target_id', 'Target Sources', true, ['personal'], 'Database'),
    Component: TargetSources,
  },
  'target.map': {
    definition: def('target.map', 'target_map', 'target', 'target_id', 'Target Map', true, ['personal', 'cis'], 'Map'),
    Component: TargetMap,
  },
  'target.graph': {
    definition: def('target.graph', 'target_graph', 'target', 'target_id', 'Target Graph', true, ['personal', 'cis'], 'Share2'),
    Component: TargetGraph,
  },
  'target.timeline': {
    definition: def('target.timeline', 'target_timeline', 'target', 'target_id', 'Target Timeline', true, ['personal', 'cis'], 'Clock'),
    Component: TargetTimeline,
  },
  'target.claims': {
    definition: def('target.claims', 'target_claims', 'target', 'target_id', 'Target Claims', true, ['personal', 'cis'], 'Quote'),
    Component: TargetClaims,
  },

  // --- Analyst panels (A1–A5) ---
  'analyst.runs': {
    definition: def('analyst.runs', 'analyst_runs', 'analyst', 'analyst_id', 'Analyst Runs', true, ['personal'], 'History'),
    Component: AnalystRuns,
  },
  'analyst.outputs': {
    definition: def('analyst.outputs', 'analyst_outputs', 'analyst', 'analyst_id', 'Analyst Outputs', true, ['personal', 'cis'], 'Activity'),
    Component: AnalystOutputs,
  },
  'analyst.cross_target': {
    definition: def('analyst.cross_target', 'analyst_cross_target', 'analyst', 'analyst_id', 'Cross-target Analyst', true, ['personal', 'cis'], 'Globe'),
    Component: AnalystCrossTarget,
  },
  'analyst.critiques': {
    definition: def('analyst.critiques', 'analyst_critiques', 'analyst', 'analyst_id', 'Critic Scores', true, ['personal'], 'Gavel'),
    Component: AnalystCritiques,
  },

  // (Cross-target dashboards D1–D3 removed — S7-T2 consolidation.)

  // --- Operator (O1–O5) ---
  'registry.targets': {
    definition: def('registry.targets', 'registry_targets', 'operator', null, 'Target Registry', false, ['personal'], 'FolderTree'),
    Component: RegistryTargets,
  },
  'registry.analysts': {
    definition: def('registry.analysts', 'registry_analysts', 'operator', null, 'Analyst Registry', false, ['personal'], 'Users'),
    Component: RegistryAnalysts,
  },
  'registry.stack': {
    definition: def('registry.stack', 'registry_stack', 'operator', null, 'Stack Registry', false, ['personal'], 'Layers'),
    Component: RegistryStack,
  },
  'registry.action_packs': {
    definition: def('registry.action_packs', 'registry_action_packs', 'operator', null, 'Action-Pack Grants', false, ['personal'], 'KeyRound'),
    Component: RegistryActionPacks,
  },
  // (registry.wirings / registry.mutations / registry.discovery removed — S7-T2.)

  // --- System (S1–S8 + P-1 cross-target findings + consult) ---
  'system.findings': {
    // #90 feed merge — THE single feed: findings + signals in one list, a Live
    // on/off button + Source (All/Findings/Signals) filter + findings-only
    // clustering. Subsumes + replaces the former `v4.feed` rail (deleted).
    definition: def('system.findings', 'system_findings', 'system', null, 'Live Feed', false, ['personal', 'cis'], 'Radio'),
    Component: SystemFindings,
  },
  'system.budget': {
    definition: def('system.budget', 'system_budget', 'system', null, 'Budget Ledger', false, ['personal'], 'DollarSign'),
    Component: SystemBudget,
  },
  'system.optimizer': {
    definition: def('system.optimizer', 'system_optimizer', 'system', null, 'Optimizer Candidates', false, ['personal'], 'Sparkles'),
    Component: SystemOptimizer,
  },
  'system.dead_letter': {
    definition: def('system.dead_letter', 'system_dead_letter', 'system', null, 'Dead-letter Inspector', false, ['personal'], 'AlertOctagon'),
    Component: SystemDeadLetter,
  },
  // (system.runtime → merged into system.actor_health; system.streams / system.users
  //  removed — S7-T2 consolidation.)
  'system.consult': {
    // U-3 merge — Deep Consult is a depth toggle here (merged/Consult.tsx);
    // `system.deep_consult` retired into the alias table (→ tab "deep").
    definition: def('system.consult', 'system_consult', 'system', null, 'Consult', false, ['personal'], 'MessageSquare'),
    Component: SystemConsultMerged,
  },
  'system.settings': {
    definition: def('system.settings', 'system_settings', 'operator', null, 'Model Stack Settings', false, ['personal'], 'Settings'),
    Component: SystemSettings,
  },

  // --- Source-first surfaces (UI-2 / Tier C — pivot) ---
  // Operator-category panels are personal-only per L-108 §6 (registry.test.ts
  // enforces modes === ['personal']).
  'registry.sources': {
    definition: def('registry.sources', 'registry_sources', 'operator', null, 'Source Registry', false, ['personal'], 'Database'),
    Component: SourceRegistry,
  },
  'source.detail': {
    definition: def('source.detail', 'source_detail', 'operator', null, 'Source Detail', false, ['personal'], 'Activity'),
    Component: SourceDetail,
  },
  'source.subscription_builder': {
    definition: def('source.subscription_builder', 'source_subscription_builder', 'operator', null, 'Subscription Builder', false, ['personal'], 'Filter'),
    Component: SourceSubscriptionBuilder,
  },
  'source.subscription_policy': {
    definition: def('source.subscription_policy', 'source_subscription_policy', 'operator', null, 'Subscription Policy', false, ['personal'], 'Lock'),
    Component: SourceSubscriptionPolicy,
  },
  'source.fanout': {
    definition: def('source.fanout', 'source_fanout', 'operator', null, 'Fan-out Explorer', false, ['personal'], 'Network'),
    Component: SourceFanout,
  },

  // --- Eval + Ops surfaces (UI-5 / Tiers E+F — appended) ---
  'system.eval_scorecard': {
    definition: def('system.eval_scorecard', 'system_eval_scorecard', 'system', null, 'Eval Scorecard', false, ['personal'], 'ClipboardCheck'),
    Component: SystemEvalScorecard,
  },
  // Correctness gold-set weekly labeling worksheet (P2-5) — personal-only:
  // the operator judges the week's pinned sample; verdicts grow the eval
  // scoreboard's per-unit operator-correctness n. Renamed "Weekly Grading"
  // (U-3 §4) — the prior "Correctness Gold Set" name described the DATA
  // structure, not the operator's weekly task.
  'system.goldset': {
    definition: def('system.goldset', 'system_goldset', 'system', null, 'Weekly Grading', false, ['personal'], 'ClipboardPen'),
    Component: SystemGoldset,
  },
  'system.optimizer.diff': {
    definition: def('system.optimizer.diff', 'system_optimizer_diff', 'system', null, 'Prompt-Module Diff', false, ['personal'], 'GitCompare'),
    Component: SystemOptimizerDiff,
  },
  'system.governor': {
    definition: def('system.governor', 'system_governor', 'system', null, 'Governor Events', false, ['personal'], 'ShieldAlert'),
    Component: SystemGovernorEvents,
  },
  'system.audit': {
    definition: def('system.audit', 'system_audit', 'system', null, 'Audit-Chain Browser', false, ['personal'], 'FileLock2'),
    Component: SystemAuditChain,
  },
  'system.stream_lag': {
    definition: def('system.stream_lag', 'system_stream_lag', 'system', null, 'Consumer-Lag Monitor', false, ['personal'], 'Gauge'),
    Component: SystemStreamLag,
  },
  'system.actor_health': {
    definition: def('system.actor_health', 'system_actor_health', 'system', null, 'Actor Health', false, ['personal'], 'HeartPulse'),
    Component: SystemActorHealth,
  },
  // System Status — the at-a-glance per-layer health view (acquisition / analysis
  // / queues / infra in one page) the operator has repeatedly asked for. Live
  // tier; System group via the system.* → Operate prefix fallback (navGroups.ts).
  'system.status': {
    definition: def('system.status', 'system_status', 'system', null, 'System Status', false, ['personal'], 'Gauge'),
    Component: SystemStatus,
  },
  // The Timeline — U-3 merge (the two panels literally both named "Timeline"):
  // an Events (v4 lanes) / Validity (P4-4 ranged-item, [valid_from,
  // valid_until) / lifecycle / [produced_at,superseded_at) + supersession
  // edges, brushable + zoomable ms→months) mode switch. Desk-scoped to the
  // unified selection; ships personal + cis.
  'system.timeline': {
    definition: def('system.timeline', 'system_timeline', 'system', null, 'Timeline', false, ['personal', 'cis'], 'GanttChartSquare'),
    Component: SystemTimelineMerged,
  },
  // The Wall (P1-7) — the mission-control anchor: world-at-a-glance band grid
  // + movers-since-last-visit + newest high-severity verified + health corner.
  // Ships in personal + cis. The MORNING READ workspace mounts this whole
  // panel as its top band (lib/workspaces.ts) — the standalone movers tile
  // below is no longer seeded, because mounting a quadrant beside its own
  // parent was the duplication the holistic design called out.
  'system.wall': {
    definition: def('system.wall', 'system_wall', 'system', null, 'The Wall', false, ['personal', 'cis'], 'LayoutGrid'),
    Component: SystemWall,
  },
  // U-4 (COHERENCE_WAVES_PLAN_2026-07-28) — standalone mount of JUST the
  // Wall's movers-since-last-visit quadrant. Kept registered + hidden
  // (HIDDEN_KINDS below) rather than aliased onto the Wall: a saved layout
  // holding this tile keeps rendering the quadrant it asked for.
  'system.wall_movers': {
    definition: def('system.wall_movers', 'system_wall_movers', 'system', null, 'Movers Since Last Visit', false, ['personal', 'cis'], 'TrendingUp'),
    Component: SystemWallMovers,
  },
  // --- Product surfaces (UI-6 / Tier G — pivot) ---
  // System-category singletons; ship in personal + cis (the Travis-ASM
  // multi-tenant model uses cis). No binding — all four scope themselves.
  'system.search': {
    definition: def('system.search', 'system_search', 'system', null, 'Global Search', false, ['personal', 'cis'], 'Search'),
    Component: SystemSearch,
  },
  'system.entities': {
    // U-3 merge — Entity Graph + Notable Structure live here as tabs
    // (Entities.tsx renders them unmodified). Both original kinds RETIRED into
    // panel-registry/aliases.ts, which lands a deep-link on the right tab.
    definition: def('system.entities', 'system_entities', 'operator', null, 'Entities', false, ['personal'], 'Boxes'),
    Component: SystemEntities,
  },
  'system.graph_walk': {
    // K-G4 — the graph WALK, and the one graph surface that is NOT folded into
    // Entities: it is an interactive verb (anchor, expand, inspect an edge's
    // evidence) rather than a rendered projection, and it reads the reified
    // `entity_edges` store that no other panel touches.
    definition: def('system.graph_walk', 'system_graph_walk', 'operator', null, 'Graph Walk', false, ['personal'], 'Network'),
    Component: SystemGraphWalk,
  },
  // --- U-3 merges — the new visible tabbed surfaces themselves ---
  // Provenance — folds v4.why + system.lineage + v4.flow into one tabbed
  // surface (Why / Lineage / Flow), all three mounted unmodified.
  'system.provenance': {
    definition: def('system.provenance', 'system_provenance', 'system', null, 'Provenance', false, ['personal', 'cis'], 'GitBranch'),
    Component: SystemProvenance,
  },
  // Alerts & Watches — folds system.watchlist + system.alert_center +
  // system.escalations into one tabbed surface (Watches / Triggers /
  // Deliveries), all three mounted unmodified. Overall tier stays 'live'
  // (most tabs are); the Triggers tab alone labels itself 'preview'.
  'system.alerts_watches': {
    definition: def('system.alerts_watches', 'system_alerts_watches', 'system', null, 'Alerts & Watches', false, ['personal', 'cis'], 'Bell'),
    Component: SystemAlertsWatches,
  },
  'system.report_export': {
    definition: def('system.report_export', 'system_report_export', 'system', null, 'Report Export', false, ['personal', 'cis'], 'FileDown'),
    Component: SystemReportExport,
  },
  // (system.tenant_view removed — S7-T2; multitenancy is ingestion-only.)

  // --- The Inspector (singleton; selection-linked; the keystone) ---
  'system.inspector': {
    definition: def('system.inspector', 'system_inspector', 'system', null, 'Inspector', false, ['personal', 'cis'], 'PanelRight'),
    Component: SystemInspector,
  },

  // --- The Journal — reflective voice + navigable product index (Wave 3) ---
  'system.journal': {
    definition: def('system.journal', 'system_journal', 'system', null, 'Journal', false, ['personal', 'cis'], 'BookOpen'),
    Component: SystemJournal,
  },

  // --- GLASS-2: consumers for the three API surfaces that had none ---
  // The Journal Gate — `journal_proposals` accept/reject, the human gate that
  // the standing rule requires and that was reachable only by curl until now.
  // Engine Room (navGroups KIND_GROUP), beside Weekly Grading: both are
  // operator DUTIES over a queue, not analytical reads. Personal-only — a
  // gate that applies registry writes is single-operator by construction.
  'system.journal_gate': {
    definition: def('system.journal_gate', 'system_journal_gate', 'system', null, 'Journal Gate', false, ['personal'], 'Gavel'),
    Component: SystemJournalGate,
  },

  // --- GLASS-3: the ops deck --------------------------------------------
  // Four VISIBLE Engine Room rows, deliberately not hidden and not folded into
  // tabs. The ≤23-row budget (navGroups.test.ts) measures NON-Engine-Room rows
  // only — Engine Room's own rows collapse behind one header — so these cost
  // the budget nothing, and the budget's "earn it, fold, or hide" terms do not
  // bind here the way they bound `system.situations`/`system.narratives`.
  // Personal-only: every one of them reads operational internals (provider
  // routing, source ledgers, run timings) that the `cis` mode has no use for.
  //
  // The whole-engine production read + its integrity/metering bricks. Leads the
  // deck because it is the one that PAGES: its `pages` flag is the same
  // predicate the alert plane's production_deficit trigger fires on, so this
  // panel and the operator's phone cannot disagree.
  'system.production_gauge': {
    definition: def('system.production_gauge', 'system_production_gauge', 'system', null, 'Production Gauge', false, ['personal'], 'Gauge'),
    Component: SystemProductionGauge,
  },
  // The judge's verdict mix by serving provider — the track's one new API.
  'system.judge_stats': {
    definition: def('system.judge_stats', 'system_judge_stats', 'system', null, 'Judge Stats', false, ['personal'], 'Scale'),
    Component: SystemJudgeStats,
  },
  // Source quality + staleness debt in one rollup, with a per-source drill.
  'system.source_health': {
    definition: def('system.source_health', 'system_source_health', 'system', null, 'Source Health', false, ['personal'], 'HeartPulse'),
    Component: SystemSourceHealth,
  },
  // Desk baselines / band trajectory / analyst runtime. A separate kind rather
  // than three more tabs on `system.eval_scorecard`: that panel is already 833
  // lines and answers "is this ANALYST getting better", while these three are
  // engine-level boards (a desk's statistical band, a verdict's drift, a run's
  // wall time) that an operator reads on a different errand.
  'system.eval_boards': {
    definition: def('system.eval_boards', 'system_eval_boards', 'system', null, 'Eval Boards', false, ['personal'], 'LayoutDashboard'),
    Component: SystemEvalBoards,
  },
  // THE READ SCOREBOARD (D2e) — the only panel on the deck that measures the
  // OPERATOR instead of the engine. Sits with the ops panels because it is
  // read on the same errand ("is this thing working?") and because the answer
  // it gives is the one the 90-day oracle wager is graded on.
  'system.read_scoreboard': {
    definition: def('system.read_scoreboard', 'system_read_scoreboard', 'system', null, 'Read Scoreboard', false, ['personal'], 'Eye'),
    Component: SystemReadScoreboard,
  },

  // --- v4 visual workspace panels (singletons; selection-linked) ---
  'v4.map': {
    definition: def('v4.map', 'v4_map', 'system', null, 'World Map', false, ['personal', 'cis'], 'Globe2'),
    Component: V4Map,
  },
  'v4.assessment': {
    definition: def('v4.assessment', 'v4_assessment', 'system', null, 'World Assessment', false, ['personal', 'cis'], 'ScrollText'),
    Component: V4Assessment,
  },
  // (v4.case Casework Board removed — S7-T2; shelved, no pin board reachable.)
  // Renamed "At a Glance" (U-3 §4) — "KPI Strip" named the widget, not what an
  // operator uses it FOR.
  'v4.kpi': {
    definition: def('v4.kpi', 'v4_kpi', 'system', null, 'At a Glance', false, ['personal', 'cis'], 'Gauge'),
    Component: V4Kpi,
  },
}

function def(
  kind: PanelKind,
  panelId: string,
  category: PanelKindDefinition['category'],
  scopeKey: PanelKindDefinition['scopeKey'],
  defaultTitle: string,
  requiresBinding: boolean,
  modes: PanelKindDefinition['modes'],
  iconName?: string,
  hidden?: boolean,
): PanelKindDefinition {
  // tier defaults to 'live'; the PREVIEW_KINDS set below promotes the
  // guarded-preview surfaces to 'preview' in one place (mirrors HIDDEN_KINDS).
  return { kind, panelId, category, scopeKey, defaultTitle, requiresBinding, modes, iconName, hidden, tier: 'live' }
}

// Panels whose backend route is a guarded preview / honest pending-state, or
// whose data is operator-experimental — registered + usable but flagged so
// operators know they're not the everyday product surface. See docs/UI.md
// §"Panel tiers".
//   * system.backfill        — backend POST is an honest 501 (cross-plane
//                              runtime trigger not exposed through the registry)
//   * system.optimizer.diff  — operator review aid over the GEPA loop
//   * system.search          — client-only product surface (no dedicated
//                              backend route yet)
// (system.report_export left this set in A10 — it now fronts the real
// POST /api/v1/v3/export collection-basket route, shipped on the same train.)
// (system.alert_center left this set when it RETIRED into the alias table —
//  it is now Alerts & Watches' "Triggers" tab, which carries the preview badge
//  on the tab itself; see panels/merged/AlertsWatches.tsx.)
const PREVIEW_KINDS: ReadonlySet<PanelKind> = new Set([
  'system.optimizer.diff',
  'system.search',
])
for (const k of PREVIEW_KINDS) {
  PANEL_REGISTRY[k].definition.tier = 'preview'
}

// Hidden-but-registered set — SHRUNK to the surfaces that have no survivor yet.
//
// The twelve U-3/GLASS-2 merge originals that used to live here (v4.timeline,
// v4.why, system.lineage, v4.flow, system.situations, system.narratives,
// system.watchlist, system.alert_center, system.escalations,
// system.deep_consult, system.entity_graph, system.notable_structure) are GONE
// from this file entirely: they are now rows in `panel-registry/aliases.ts`,
// which resolves a retired id onto the survivor that already renders it —
// without a registry row, a component import, or a `hidden` flag to remember
// (UI_HOLISTIC_DESIGN_2026-08-24 §4.4). Twelve rows of catalog that existed
// only to be invisible became twelve lines of data.
//
// What is still hidden-but-registered is the set an alias CANNOT yet describe,
// because no shipped surface renders it. Each needs a merge train first, and
// hiding is the honest holding position until then — pointing an alias at an
// approximation would silently lose the capability:
//   * system.optimizer.diff       — the Optimizer's candidate drill (opened by
//                                   its event bridge, not by a sidebar row)
//   * source.subscription_builder — niche source-config; reachable via ⌘K
//   * source.subscription_policy  — niche source-config; reachable via ⌘K
//   * source.fanout               — niche explorer; reachable via ⌘K
//   * system.stream_lag           — consumer-lag detail; System Status shows
//                                   the QUEUES rollup, not this drill
//   * system.wall_movers          — the standalone movers tile. Not a merge
//                                   original: the Wall carries movers as its
//                                   own quadrant, so the landing workspace
//                                   mounts the WALL and this tile stays
//                                   reachable for anyone whose saved layout
//                                   holds it.
const HIDDEN_KINDS: ReadonlySet<PanelKind> = new Set<PanelKind>([
  'system.optimizer.diff',
  'source.subscription_builder',
  'source.subscription_policy',
  'source.fanout',
  'system.stream_lag',
  'system.wall_movers',
])
for (const k of HIDDEN_KINDS) {
  PANEL_REGISTRY[k].definition.hidden = true
}

/** Map panel-id (descriptor surface) → panel-kind (frontend surface). */
export const PANEL_ID_TO_KIND: Record<string, PanelKind> = (() => {
  const out: Record<string, PanelKind> = {}
  for (const [kind, entry] of Object.entries(PANEL_REGISTRY) as Array<[PanelKind, RegistryEntry]>) {
    out[entry.definition.panelId] = kind
  }
  return out
})()

/**
 * Singleton panels (no binding required) — added to layout at boot
 * regardless of registry rows.  Hidden panels (§6 DROP set) are filtered
 * out so the sidebar's "Always-on" section only shows live + soon-to-be-
 * live panels.
 */
export const SINGLETON_PANELS: PanelKind[] = (() => {
  const out: PanelKind[] = []
  for (const [kind, entry] of Object.entries(PANEL_REGISTRY) as Array<[PanelKind, RegistryEntry]>) {
    if (entry.definition.requiresBinding) continue
    if (entry.definition.hidden) continue
    out.push(kind)
  }
  return out
})()
