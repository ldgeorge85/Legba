/**
 * Panel descriptions — one line per kind, what the tile is FOR (X3/D2).
 *
 * The `＋` catalog listed thirty bare names ("Register", "Ledgers", "Eval
 * Boards") with nothing to say what any of them reads. Each kind now carries a
 * one-line `description` on its registry definition (`PanelKindDefinition.
 * description`, applied in `registry.ts`), and the catalog prints it under the
 * name. Platform words only: no instance's subjects, sources or numbers
 * (planning/PLATFORM_VS_EXEMPLAR_2026-09-29 §1) — a profile renames the tile,
 * never what it is for.
 *
 * `registry.test.ts`-style guard: `descriptions.test.ts` holds that every
 * catalog kind has one and that no description names a retired kind.
 */
import type { PanelKind } from '@/types'

export const PANEL_DESCRIPTIONS: Readonly<Partial<Record<PanelKind, string>>> = {
  // --- the read and its neighbours ---
  'v4.morning_read': "The day's read, in order: what changed, what was checked, the gaps, what is due.",
  'v4.assessment': 'The composed one-page assessment, as prose — the A/B partner of the read.',
  'system.wall': 'The dashboard: the at-a-glance band, movers since your last visit, newest verified.',
  'system.findings': 'Live Feed — findings and signals in one list, filterable, newest first.',
  'system.consult': 'Ask a question; every answer cites the records it used. Deep mode for long runs.',
  'v4.map': 'The map: signals, findings, situations and events on the shared clock.',
  'system.timeline': 'Events and validity windows on one timeline, brushable, on the shared clock.',
  'system.inspector': 'The selected record in full — its claims, sources, verdicts and lineage.',
  // --- investigate ---
  'system.entities': 'Entities and their graph — who and what the records name, and how they connect.',
  'system.graph_walk': 'Walk the entity graph from one anchor, edge by edge, with the evidence for each.',
  'system.provenance': 'Why a record says what it says — its inputs, lineage and flow.',
  'system.register': 'The frame register: situations and narratives, tracked over time.',
  'system.search': 'Search across records by text (preview — client-side only).',
  'system.alerts_watches': 'Your watches, the triggers that fire on them, and what was delivered.',
  'system.report_export': 'Collect records into a basket and export them as a report.',
  'system.journal': "The system's reflective journal — entries and the products they index.",
  // --- trust ---
  'system.eval_scorecard': 'Accuracy: the forecast ledger and its scorecard, calibration by band, the grader roster.',
  'system.judge_stats': "The faithfulness judge's verdict mix, by serving provider.",
  'system.source_health': 'Source quality and staleness in one rollup, with a per-source drill.',
  'system.eval_boards': 'Engine-level eval boards: baselines, band trajectory, run timings.',
  'system.layer_divergence': 'Where the layers disagree — the gap between one layer and the next, over time.',
  'analysis.contentions': 'The contrary-evidence pass: claims it contested, and on what evidence.',
  'system.read_scoreboard': 'How the reader uses the workstation — reads, opens, dwell — the one board that measures you.',
  // --- the gate ---
  'system.journal_gate': 'Accept or reject the journal\'s proposed changes — the human gate.',
  'system.goldset': "This week's grading sample — your verdicts grow the correctness record.",
  'system.optimizer': 'Prompt-module candidates from the optimizer, with their diffs, awaiting review.',
  // --- engine ---
  'system.status': 'Is the machine running: acquisition, analysis, queues, failures, actors.',
  'system.registry': 'Every descriptor family — subjects, units, sources, stack, packs — and their builders.',
  'system.ledgers': 'The append-only engine ledgers: governor, audit and budget.',
  'system.settings': 'The model stack: which model serves which plane.',
  'system.flow': 'The agents as a graph: what each one reads and writes as declared, its prompt, its runs. Read-only.',
  // --- drills (opened from their object, never the catalog) ---
  'analysis.cross_framing': 'One claim against every unit that touches it, and what no unit says.',
  'subject.page': "One subject's page: brief, evidence, claims, signals, situations, sources, graph.",
  'target.map': "One subject's map.",
  'target.timeline': "One subject's timeline.",
  'analyst.runs': "One unit's runs, newest first.",
  'analyst.outputs': "One unit's outputs.",
  'analyst.cross_target': "One unit's reads across subjects.",
  'analyst.critiques': "One unit's critic scores.",
}
