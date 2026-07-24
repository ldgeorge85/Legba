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
const SystemLineage = lazy(() => import('@/panels/system/Lineage'))
const SystemBudget = lazy(() => import('@/panels/system/Budget'))
const SystemOptimizer = lazy(() => import('@/panels/system/Optimizer'))
const SystemDeadLetter = lazy(() => import('@/panels/system/DeadLetter'))
const SystemConsult = lazy(() => import('@/panels/system/Consult'))
const SystemDeepConsult = lazy(() => import('@/panels/system/DeepConsult'))
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
const SystemOptimizerDiff = lazy(() => import('@/panels/system/OptimizerDiff'))
const SystemGovernorEvents = lazy(() => import('@/panels/system/GovernorEvents'))
const SystemAuditChain = lazy(() => import('@/panels/system/AuditChain'))
const SystemStreamLag = lazy(() => import('@/panels/system/StreamLag'))
const SystemActorHealth = lazy(() => import('@/panels/system/ActorHealth'))
// System Status — the at-a-glance per-layer health view (#89 ops surface).
const SystemStatus = lazy(() => import('@/panels/system/SystemStatus'))
// Product surfaces (UI-6 / Tier G — pivot)
const SystemSearch = lazy(() => import('@/panels/system/Search'))
const SystemEntities = lazy(() => import('@/panels/system/Entities'))
const SystemEntityGraph = lazy(() => import('@/panels/system/EntityGraph'))
const SystemNotableStructure = lazy(() => import('@/panels/system/NotableStructure'))
const SystemAlertCenter = lazy(() => import('@/panels/system/AlertCenter'))
const SystemEscalations = lazy(() => import('@/panels/system/Escalations'))
const SystemReportExport = lazy(() => import('@/panels/system/ReportExport'))

// The Inspector — unified detail surface (redesign Move 1, the keystone).
const SystemInspector = lazy(() => import('@/components/inspector/InspectorPanel'))

// The Journal — Legba's reflective voice + navigable index (Wave 3).
const SystemJournal = lazy(() => import('@/panels/system/Journal'))

// v4 visual workspace panels (selection-linked singletons).
const V4Map = lazy(() => import('@/panels/v4/MapPanel'))
const V4Flow = lazy(() => import('@/panels/v4/FlowPanel'))
const V4Why = lazy(() => import('@/panels/v4/WhyPanel'))
const V4Assessment = lazy(() => import('@/panels/v4/AssessmentPanel'))
const V4Kpi = lazy(() => import('@/panels/v4/KpiPanel'))
const V4Timeline = lazy(() => import('@/panels/v4/TimelinePanel'))

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
  'system.lineage': {
    definition: def('system.lineage', 'system_lineage', 'system', null, 'Provenance Lineage', false, ['personal', 'cis'], 'Network'),
    Component: SystemLineage,
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
    definition: def('system.consult', 'system_consult', 'system', null, 'Consult', false, ['personal'], 'MessageSquare'),
    Component: SystemConsult,
  },
  'system.deep_consult': {
    definition: def('system.deep_consult', 'system_deep_consult', 'system', null, 'Deep Consult', false, ['personal'], 'BrainCircuit'),
    Component: SystemDeepConsult,
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
  // --- Product surfaces (UI-6 / Tier G — pivot) ---
  // System-category singletons; ship in personal + cis (the Travis-ASM
  // multi-tenant model uses cis). No binding — all four scope themselves.
  'system.search': {
    definition: def('system.search', 'system_search', 'system', null, 'Global Search', false, ['personal', 'cis'], 'Search'),
    Component: SystemSearch,
  },
  'system.entities': {
    definition: def('system.entities', 'system_entities', 'operator', null, 'Entities', false, ['personal'], 'Boxes'),
    Component: SystemEntities,
  },
  'system.entity_graph': {
    definition: def('system.entity_graph', 'system_entity_graph', 'operator', null, 'Entity Graph', false, ['personal'], 'Share2'),
    Component: SystemEntityGraph,
  },
  'system.notable_structure': {
    definition: def('system.notable_structure', 'system_notable_structure', 'system', null, 'Notable Structure', false, ['personal', 'cis'], 'Spline'),
    Component: SystemNotableStructure,
  },
  'system.alert_center': {
    definition: def('system.alert_center', 'system_alert_center', 'system', null, 'Alert Center', false, ['personal', 'cis'], 'Bell'),
    Component: SystemAlertCenter,
  },
  // Escalation Deliveries — the human-visible alert edge (audit finding C3 /
  // decision D1). Renders alert_sink_deliveries: did each escalation LAND
  // (delivered) or go NOWHERE (failed / logged_only)? Live route
  // (`GET /api/v1/v3/system/escalations`), so NOT a preview surface.
  'system.escalations': {
    definition: def('system.escalations', 'system_escalations', 'system', null, 'Escalation Deliveries', false, ['personal', 'cis'], 'Siren'),
    Component: SystemEscalations,
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

  // --- v4 visual workspace panels (singletons; selection-linked) ---
  'v4.map': {
    definition: def('v4.map', 'v4_map', 'system', null, 'World Map', false, ['personal', 'cis'], 'Globe2'),
    Component: V4Map,
  },
  'v4.flow': {
    definition: def('v4.flow', 'v4_flow', 'system', null, 'Flow Canvas', false, ['personal', 'cis'], 'Workflow'),
    Component: V4Flow,
  },
  'v4.why': {
    definition: def('v4.why', 'v4_why', 'system', null, 'Why · Provenance', false, ['personal', 'cis'], 'GitBranch'),
    Component: V4Why,
  },
  'v4.assessment': {
    definition: def('v4.assessment', 'v4_assessment', 'system', null, 'World Assessment', false, ['personal', 'cis'], 'ScrollText'),
    Component: V4Assessment,
  },
  // (v4.case Casework Board removed — S7-T2; shelved, no pin board reachable.)
  'v4.kpi': {
    definition: def('v4.kpi', 'v4_kpi', 'system', null, 'KPI Strip', false, ['personal', 'cis'], 'Gauge'),
    Component: V4Kpi,
  },
  'v4.timeline': {
    definition: def('v4.timeline', 'v4_timeline', 'system', null, 'Timeline', false, ['personal', 'cis'], 'Clock'),
    Component: V4Timeline,
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
//   * system.search / alert_center / report_export / tenant_view — client-only
//                              product surfaces (no dedicated backend route yet)
const PREVIEW_KINDS: ReadonlySet<PanelKind> = new Set([
  'system.optimizer.diff',
  'system.search',
  'system.alert_center',
  'system.report_export',
])
for (const k of PREVIEW_KINDS) {
  PANEL_REGISTRY[k].definition.tier = 'preview'
}

// Hidden-but-registered set — merge/consolidation targets kept in the bundle
// (so any saved layout or ⌘K deep-link still resolves them) but dropped from the
// sidebar so the workstation catalog stays ~25-30 GOOD panels.  The S7-T1 §6
// DROP set (system.pulse/eval/users/streams, registry.wirings/mutations,
// dashboard.dynamic, registry.discovery, system.backfill/runtime/tenant_view,
// system.targets.roster, v4.case) was DELETED outright in S7-T2 — those kinds no
// longer exist.  What remains hidden here are LIVE panels merged into a peer:
//   * system.report_export       — the Report panel's own Download supersedes it
//   * system.optimizer.diff       — operator review aid folded under Optimizer
//   * source.subscription_builder — niche source-config; reachable via ⌘K
//   * source.subscription_policy  — niche source-config; reachable via ⌘K
//   * source.fanout               — niche explorer; reachable via ⌘K
//   * system.stream_lag           — rolled into the System Status at-a-glance view
const HIDDEN_KINDS: ReadonlySet<PanelKind> = new Set<PanelKind>([
  'system.report_export',
  'system.optimizer.diff',
  'source.subscription_builder',
  'source.subscription_policy',
  'source.fanout',
  'system.stream_lag',
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
