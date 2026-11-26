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
import { PROFILE_UI } from '@/lib/profileUi'
import { PANEL_DESCRIPTIONS } from '@/panel-registry/descriptions'

export interface RegistryEntry {
  definition: PanelKindDefinition
  /** Lazy-loaded React component for the panel kind. */
  Component: ComponentType<PanelProps>
}

// ---------------------------------------------------------------------------
// Lazy component imports — one per panel module.
// ---------------------------------------------------------------------------

// UI-B3 — the subject page: one bound page per subject of any frame kind. Its
// eight tabs mount the retired per-subject reads, unmodified
// (the panels/subject/ files; the brief tab is the desk brief generalized).
const SubjectPage = lazy(() => import('@/panels/subject/SubjectPage'))
const TargetMap = lazy(() => import('@/panels/target/Map'))
const TargetTimeline = lazy(() => import('@/panels/target/Timeline'))

const AnalystRuns = lazy(() => import('@/panels/analyst/Runs'))
const AnalystOutputs = lazy(() => import('@/panels/analyst/Outputs'))
const AnalystCrossTarget = lazy(() => import('@/panels/analyst/CrossTarget'))
const AnalystCritiques = lazy(() => import('@/panels/analyst/Critiques'))

const SystemFindings = lazy(() => import('@/panels/system/Findings'))
// UI-B1/merges — the Optimizer folds its prompt-module diff drill in as a tab
// (merged/Optimizer.tsx mounts both original, unmodified components).
const SystemOptimizerMerged = lazy(() => import('@/panels/merged/Optimizer'))
// U-3 merge — Deep Consult folds into a depth toggle on Consult; the merged
// wrapper mounts both original, unmodified components (merged/Consult.tsx).
const SystemConsultMerged = lazy(() => import('@/panels/merged/Consult'))
const SystemSettings = lazy(() => import('@/panels/system/Settings'))

// UI-B1/merges — the Engine's three merged wrappers (UI-D1 Table A). Each
// mounts its retired kinds' original, unmodified components as tabs:
//   Registry → registry.{targets,analysts,sources,stack,action_packs},
//              source.{detail,subscription_builder,subscription_policy}, v4.flow
//   Ledgers  → system.{governor,audit,budget}
//   Status   → system.status + system.{production_gauge,dead_letter,
//              actor_health,stream_lag}
const SystemRegistryMerged = lazy(() => import('@/panels/merged/Registry'))
const SystemLedgersMerged = lazy(() => import('@/panels/merged/Ledgers'))
const SystemStatusMerged = lazy(() => import('@/panels/merged/Status'))
// UI-B1/merges — the frame register: Situations · Narratives (re-pointed off
// Provenance's old Trajectory / Narratives tabs).
const SystemRegisterMerged = lazy(() => import('@/panels/merged/Register'))
// N-J — the Flow: a read-only canvas over the descriptor heads (React Flow +
// ELK). Every edge is a declared relation with its basis; nothing is edited.
const SystemFlow = lazy(() => import('@/panels/flow/FlowPanel'))

// Eval + Ops surfaces (UI-5 / Tiers E+F — appended)
const SystemEvalScorecard = lazy(() => import('@/panels/system/EvalScorecard'))
// Correctness gold-set weekly labeling worksheet (P2-5).
const SystemGoldset = lazy(() => import('@/panels/system/Goldset'))
// The Timeline — U-3 merge: one "Timeline" panel with an Events/Validity mode
// switch (merged/Timeline.tsx), folding v4.timeline (event lanes) + the
// original system.timeline (validity windows, imported inside the wrapper).
const SystemTimelineMerged = lazy(() => import('@/panels/merged/Timeline'))
// The Wall — UI-B1/merges: the At-a-Glance band over Overview · Movers
// (merged/Wall.tsx folds v4.kpi + system.wall_movers, unmodified).
const SystemWallMerged = lazy(() => import('@/panels/merged/Wall'))
// Product surfaces (UI-6 / Tier G — pivot)
const SystemSearch = lazy(() => import('@/panels/system/Search'))
const SystemEntities = lazy(() => import('@/panels/system/Entities'))
const SystemGraphWalk = lazy(() => import('@/panels/system/GraphWalk'))
const SystemReportExport = lazy(() => import('@/panels/system/ReportExport'))

// The Inspector — unified detail surface (redesign Move 1, the keystone).
const SystemInspector = lazy(() => import('@/components/inspector/InspectorPanel'))

// The Journal — Legba's reflective voice + navigable index.
const SystemJournal = lazy(() => import('@/panels/system/Journal'))
// GLASS-2 — the three unconsumed API surfaces get their consumers.
const SystemJournalGate = lazy(() => import('@/panels/system/JournalGate'))
// GLASS-3 — the ops deck.
const SystemJudgeStats = lazy(() => import('@/panels/system/JudgeStats'))
const SystemSourceHealth = lazy(() => import('@/panels/system/SourceHealth'))
const SystemEvalBoards = lazy(() => import('@/panels/system/EvalBoards'))
const SystemReadScoreboard = lazy(() => import('@/panels/system/ReadScoreboard'))

// The divergence map.
const SystemLayerDivergence = lazy(() => import('@/panels/system/LayerDivergence'))

// The cross-framing comparison, scoped to one CLAIM.
const AnalysisCrossFraming = lazy(() => import('@/panels/analysis/CrossFraming'))
// W2 — the contrary-evidence pass's records as a list (`/v3/contentions`).
const AnalysisContentions = lazy(() => import('@/panels/analysis/Contentions'))

// v4 visual workspace panels (selection-linked singletons).
const V4Map = lazy(() => import('@/panels/v4/MapPanel'))
const V4Assessment = lazy(() => import('@/panels/v4/AssessmentPanel'))
const V4MorningRead = lazy(() => import('@/panels/v4/MorningReadPanel'))

// U-3 merge, re-cut by UI-B1/merges — Provenance: Why · Lineage · Fan-out
// (merged/Provenance.tsx mounts v4.why + system.lineage + source.fanout,
// unmodified).
const SystemProvenance = lazy(() => import('@/panels/merged/Provenance'))
// U-3 merge — Alerts & Watches folds system.watchlist + system.alert_center +
// system.escalations into one tabbed surface (merged/AlertsWatches.tsx).
const SystemAlertsWatches = lazy(() => import('@/panels/merged/AlertsWatches'))

// ---------------------------------------------------------------------------
// Registry table.
// ---------------------------------------------------------------------------

export const PANEL_REGISTRY: Record<PanelKind, RegistryEntry> = {
  // --- The subject page (UI-B3, UI-D1 Table A) ---
  // Bound to one subject of ANY frame kind (a country desk, a lane or flow, a
  // region, a situation, a regime). Tabs Brief · Evidence · Claims · Signals ·
  // Situations · Sources · Graph · Runtime; the eight retired `target.*` ids
  // alias onto them (aliases.ts). The title is the profile's subject noun.
  // Mounted UNBOUND by the Subject stance (id `desk`), it follows the subject
  // the reader selected (`kindPlan.ts::SUBJECT_FOLLOWING_KINDS`).
  'subject.page': {
    definition: def('subject.page', 'subject_page', 'target', 'target_id', PROFILE_UI.subject.singular, true, ['personal', 'cis'], 'Target'),
    Component: SubjectPage,
  },
  // --- Per-target reads pending their survivors (Table B: `v4.map` and
  //     `system.timeline` with a subject param) ---
  'target.map': {
    definition: def('target.map', 'target_map', 'target', 'target_id', 'Target Map', true, ['personal', 'cis'], 'Map'),
    Component: TargetMap,
  },
  'target.timeline': {
    definition: def('target.timeline', 'target_timeline', 'target', 'target_id', 'Target Timeline', true, ['personal', 'cis'], 'Clock'),
    Component: TargetTimeline,
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
  // UI-B1/merges — the five `registry.*` kinds and the three `source.*`
  // authoring kinds are tabs of `system.registry` (Engine stance, below); their
  // ids resolve through aliases.ts. (registry.wirings / registry.mutations /
  // registry.discovery removed — S7-T2.)

  // --- System (S1–S8 + P-1 cross-target findings + consult) ---
  'system.findings': {
    // #90 feed merge — THE single feed: findings + signals in one list, a Live
    // on/off button + Source (All/Findings/Signals) filter + findings-only
    // clustering. Subsumes + replaces the former `v4.feed` rail (deleted).
    definition: def('system.findings', 'system_findings', 'system', null, 'Live Feed', false, ['personal', 'cis'], 'Radio'),
    Component: SystemFindings,
  },
  // (system.navigator RETIRED in UI-B1/merges — the products index is the
  //  object rail's Reads section now; the id aliases onto the read it opened.)
  // UI-B1/merges — Candidates · Diff: the prompt-module diff drill is the
  // queue's second tab (merged/Optimizer.tsx; `system.optimizer.diff` aliases
  // onto it and its candidate rides `state/panelParams`).
  'system.optimizer': {
    definition: def('system.optimizer', 'system_optimizer', 'system', null, 'Optimizer Candidates', false, ['personal'], 'Sparkles'),
    Component: SystemOptimizerMerged,
  },
  // (system.runtime → merged into system.actor_health; system.streams / system.users
  //  removed — S7-T2 consolidation. system.budget / system.dead_letter are tabs
  //  of system.ledgers / system.status since UI-B1/merges.)
  'system.consult': {
    // U-3 merge — Deep Consult is a depth toggle here (merged/Consult.tsx);
    // `system.deep_consult` retired into the alias table (→ tab "deep").
    // Decision 3 — modes widened `['personal']` → `['personal','cis']`. Consult
    // is the landing's docked-right centre; leaving it personal-only made the
    // operator's main working surface invisible for half their sessions.
    definition: def('system.consult', 'system_consult', 'system', null, 'Consult', false, ['personal', 'cis'], 'MessageSquare'),
    Component: SystemConsultMerged,
  },
  'system.settings': {
    definition: def('system.settings', 'system_settings', 'operator', null, 'Model Stack Settings', false, ['personal'], 'Settings'),
    Component: SystemSettings,
  },

  // --- The Engine's merged wrappers (UI-B1/merges, UI-D1 Table A) ---
  // Operator-category panels are personal-only per L-108 §6 (registry.test.ts
  // enforces modes === ['personal']).
  //
  // Registry — the descriptor families as one kind with a family switch:
  // Subjects · Units · Sources (+ the per-source detail) · Stack · Packs ·
  // Builder · Policy · Canvas. Absorbs the five `registry.*` kinds,
  // `source.detail`, `source.subscription_builder`,
  // `source.subscription_policy` and (re-pointed) `v4.flow`.
  'system.registry': {
    definition: def('system.registry', 'system_registry', 'operator', null, 'Registry', false, ['personal'], 'FolderTree'),
    Component: SystemRegistryMerged,
  },
  // The Flow (N-J v0) — what each agent is, reads and writes (as the
  // registry declares it) and what a real run did. Operator: it shows prompts
  // and run internals. Read-only; layout computed, never saved.
  'system.flow': {
    definition: def('system.flow', 'system_flow', 'operator', null, 'Flow', false, ['personal'], 'Workflow'),
    Component: SystemFlow,
  },
  // Ledgers — Governor · Audit · Budget, the three append-only engine ledgers.
  'system.ledgers': {
    definition: def('system.ledgers', 'system_ledgers', 'system', null, 'Ledgers', false, ['personal'], 'BookLock'),
    Component: SystemLedgersMerged,
  },
  // Register — the frame register: Situations · Narratives. Both were
  // Provenance tabs (GLASS-2) for the old sidebar budget; Table A gives them
  // their own kind and re-points `system.situations` / `system.narratives`.
  // Ships personal + cis, like the Provenance surface it came off.
  'system.register': {
    definition: def('system.register', 'system_register', 'system', null, 'Register', false, ['personal', 'cis'], 'Frame'),
    Component: SystemRegisterMerged,
  },

  // --- Eval + Ops surfaces (UI-5 / Tiers E+F — appended) ---
  'system.eval_scorecard': {
    definition: def('system.eval_scorecard', 'system_eval_scorecard', 'system', null, 'Accuracy', false, ['personal'], 'ClipboardCheck'),
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
  // System Status — UI-D1 Table A "Engine: Status · Production · Failures ·
  // Actors · Queues". The per-layer health page (acquisition / analysis /
  // queues / infra) is the first tab; the production gauge, the dead-letter
  // inspector, actor health and consumer lag are the other four
  // (merged/Status.tsx, each unmodified; their ids alias onto their tabs).
  'system.status': {
    definition: def('system.status', 'system_status', 'system', null, 'System Status', false, ['personal'], 'Gauge'),
    Component: SystemStatusMerged,
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
  // Ships in personal + cis. UI-B1/merges: the At-a-Glance strip (`v4.kpi`) is
  // its header band and the standalone movers quadrant (`system.wall_movers`,
  // U-4) its Movers tab (merged/Wall.tsx; both ids alias here).
  'system.wall': {
    definition: def('system.wall', 'system_wall', 'system', null, 'The Wall', false, ['personal', 'cis'], 'LayoutGrid'),
    Component: SystemWallMerged,
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

  // --- The Journal — reflective voice + navigable product index ---
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
  // (The whole-engine production gauge — the deck's paging read — is System
  // Status' Production tab since UI-B1/merges; `system.production_gauge`
  // aliases onto it.)
  //
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
  // The DIVERGENCE MAP. It writes no table: the series and the
  // receipt ride the analyst payload, and a quiet run is suppressed to
  // trace-only, so until this panel there was no way to see that the instrument
  // was running at all. An ANALYSIS kind, not Engine Room: a layer-to-layer gap
  // moving is a reading about the country, not about the plumbing.
  'system.layer_divergence': {
    definition: def('system.layer_divergence', 'system_layer_divergence', 'system', null, 'Layer Divergence', false, ['personal'], 'Layers'),
    Component: SystemLayerDivergence,
  },
  // WAVE P LANE B — CROSS-FRAMING. One claim against the units that touch it,
  // the layer-divergence receipt, and what no unit says. A DRILL surface, not a
  // deck row: it is opened from the claim it is about (the Claims panel's
  // action, a situation row, ⌘K) and has nothing to show without one, which is
  // why it is a DRILL (kindPlan.ts SINGLETON_DRILLS) and never in the `＋`
  // catalog.
  'analysis.cross_framing': {
    definition: def('analysis.cross_framing', 'analysis_cross_framing', 'system', null, 'Cross-framing', false, ['personal'], 'Columns3'),
    Component: AnalysisCrossFraming,
  },
  // W2 S-batch — CONTENTIONS. The contrary-evidence pass's records as a LIST:
  // until this row a `claim_contentions` record was reachable only one claim at
  // a time (the chip on its sentence, the Cross-framing drill), and the route's
  // own fleet-wide "what did the pass find last night" read had no surface.
  // It was hidden for the old sidebar budget; with the tree gone it is an
  // ordinary `＋` catalog kind, mounted by the Trust stance (UI-D1 Table A).
  'analysis.contentions': {
    definition: def('analysis.contentions', 'analysis_contentions', 'system', null, 'Contentions', false, ['personal'], 'Scale'),
    Component: AnalysisContentions,
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
    definition: def('v4.map', 'v4_map', 'system', null, PROFILE_UI.map.title, false, ['personal', 'cis'], 'Globe2'),
    Component: V4Map,
  },
  'v4.assessment': {
    definition: def('v4.assessment', 'v4_assessment', 'system', null, PROFILE_UI.labels.read_title, false, ['personal', 'cis'], 'ScrollText'),
    Component: V4Assessment,
  },
  // D-4 — the assembly reader. Registered beside `v4.assessment` rather than
  // replacing it: while `LEGBA_COMPOSITION_ASSEMBLY` is off the rows carry no
  // `data.data.assembly` and the legacy prose one-pager is still the right
  // render of them (spec §5.2 — the old path stays runnable for the whole
  // program). This surface renders a legacy run honestly as prose too, so the
  // two overlap safely rather than fighting.
  'v4.morning_read': {
    definition: def('v4.morning_read', 'v4_morning_read', 'system', null, 'Morning Read', false, ['personal', 'cis'], 'Newspaper'),
    Component: V4MorningRead,
  },
  // (v4.case Casework Board removed — S7-T2; shelved, no pin board reachable.)
  // (v4.kpi "At a Glance" is the Wall's header band since UI-B1/merges.)
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
): PanelKindDefinition {
  // tier defaults to 'live'; the PREVIEW_KINDS set below promotes the
  // guarded-preview surfaces to 'preview' in one place.
  return { kind, panelId, category, scopeKey, defaultTitle, requiresBinding, modes, iconName, tier: 'live' }
}

// Panels whose backend route is a guarded preview / honest pending-state, or
// whose data is operator-experimental — registered + usable but flagged so
// operators know they're not the everyday product surface. See docs/UI.md
// §"Panel tiers".
//   * system.backfill        — backend POST is an honest 501 (cross-plane
//                              runtime trigger not exposed through the registry)
//   * system.search          — client-only product surface (no dedicated
//                              backend route yet)
// (system.report_export left this set in A10 — it now fronts the real
// POST /api/v1/v3/export collection-basket route, shipped on the same train.)
// (system.alert_center left this set when it RETIRED into the alias table —
//  it is now Alerts & Watches' "Triggers" tab, which carries the preview badge
//  on the tab itself; see panels/merged/AlertsWatches.tsx. system.optimizer.diff
//  left it the same way in UI-B1/merges: the Optimizer's Diff tab carries the
//  badge; see panels/merged/Optimizer.tsx.)
const PREVIEW_KINDS: ReadonlySet<PanelKind> = new Set([
  'system.search',
])
for (const k of PREVIEW_KINDS) {
  PANEL_REGISTRY[k].definition.tier = 'preview'
}

// X3/D2 — every kind's one-line description (the catalog prints it under the
// name). The data is its own module so the rows above stay one line each.
for (const [k, text] of Object.entries(PANEL_DESCRIPTIONS) as Array<[PanelKind, string]>) {
  if (PANEL_REGISTRY[k]) PANEL_REGISTRY[k].definition.description = text
}

// (HIDDEN_KINDS is GONE — UI-B1/shell, UI-D1 §2.2. "Hidden" was a flag the
// sidebar budget needed; it is now a DERIVED property: a kind whose params need
// a ref is a DRILL (`panel-registry/kindPlan.ts::isDrill`), never listed in the
// `＋` catalog and opened from its parent object. Every other kind is listed.
// The kinds the flag used to park are a drill (cross-framing), retired into a
// survivor's tab (aliases.ts — the optimizer diff and four others, UI-B1/merges)
// or a catalog kind with a stance (contentions).)

/** Map panel-id (descriptor surface) → panel-kind (frontend surface). */
export const PANEL_ID_TO_KIND: Record<string, PanelKind> = (() => {
  const out: Record<string, PanelKind> = {}
  for (const [kind, entry] of Object.entries(PANEL_REGISTRY) as Array<[PanelKind, RegistryEntry]>) {
    out[entry.definition.panelId] = kind
  }
  return out
})()

/**
 * Singleton panels — every kind that needs no binding. (The `＋` catalog is
 * this minus the singleton drills: `kindPlan.ts::catalogKinds`.)
 */
export const SINGLETON_PANELS: PanelKind[] = (() => {
  const out: PanelKind[] = []
  for (const [kind, entry] of Object.entries(PANEL_REGISTRY) as Array<[PanelKind, RegistryEntry]>) {
    if (entry.definition.requiresBinding) continue
    out.push(kind)
  }
  return out
})()
