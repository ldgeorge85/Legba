/**
 * The per-kind ABSENCE column (UI-D1 §6.5): every surface states what it
 * prints where a reading is missing, and a surface without a contract does not
 * register (`absenceContracts.test.ts` walks `PANEL_REGISTRY`).
 *
 * The contracts are UI-D1 Table A's last column, verbatim, keyed by the 31
 * SURVIVOR kinds of the 62 → 31 plan (§2.5). Every kind registered today
 * resolves to its survivor through `KIND_SURVIVOR` (Table B): a kind that
 * survives maps to itself; a kind that will retire into a tab of a survivor
 * answers to that survivor's contract, because that is the surface it will
 * become. A RETIRED id (`aliases.ts`) answers to its survivor's contract
 * through {@link absenceContractForId}. One survivor does not exist as a kind
 * yet (`unit.page` — UI-B3's unit half; `subject.page` landed in W11); its
 * contract is here so the lane that builds it inherits its acceptance line (UI-B8's docs pass reads
 * this table). UI-B1/merges built `system.register`, `system.ledgers` and
 * `system.registry` and retired twenty kinds into tabs, so their rows left
 * `KIND_SURVIVOR` for the alias table.
 *
 * A side table rather than a `registry.ts` column for one wave only: the
 * shell lane owns `registry.ts` in W7. Folding it in is a mechanical move once
 * both land. A Table A row whose contract is "—" is stored as "—": the design
 * note declares no absence case for it, and that is recorded, not invented.
 *
 * The exemplar's own words never appear in a contract; a surface substitutes
 * the profile's nouns (PLATFORM_VS_EXEMPLAR §1).
 */
import type { PanelKind } from '@/types'
import { resolveKind } from './aliases'

/** The 31 survivor kinds of UI-D1 Table A. */
export type SurvivorKind =
  | 'subject.page'
  | 'unit.page'
  | 'v4.morning_read'
  | 'system.wall'
  | 'system.findings'
  | 'v4.map'
  | 'system.timeline'
  | 'system.alerts_watches'
  | 'system.inspector'
  | 'system.consult'
  | 'system.entities'
  | 'system.graph_walk'
  | 'system.provenance'
  | 'system.register'
  | 'system.eval_scorecard'
  | 'system.eval_boards'
  | 'system.judge_stats'
  | 'system.source_health'
  | 'system.layer_divergence'
  | 'analysis.cross_framing'
  | 'analysis.contentions'
  | 'system.optimizer'
  | 'system.goldset'
  | 'system.journal_gate'
  | 'system.journal'
  | 'system.report_export'
  | 'system.read_scoreboard'
  | 'system.status'
  | 'system.ledgers'
  | 'system.registry'
  | 'system.settings'
  | 'system.flow'

/** The palette's `/` substrate family — where `system.search` retires (Table B). */
export const PALETTE_SEARCH = 'palette:/' as const

/** UI-D1 Table A, the "Absence contract" column, verbatim. */
export const ABSENCE_CONTRACTS: Record<SurvivorKind | typeof PALETTE_SEARCH, string> = {
  'subject.page':
    'a unit with no read shows its typed absence or *no read yet*; no composition → "no composition on record for this subject"; correctness absent → *unmeasured* beside its coverage; a layer not declared → "no layer map is declared"',
  'unit.page':
    'memory-only verdicts are *N carried, ungraded*, out of means; no runs in window → "no run in window", never 0',
  'v4.morning_read':
    'the floor line *N carried, floor honoured*; Gaps by kind with *last known absence, not re-checked*; a kind not read → *not measured for this subject*',
  'system.wall': 'a band not assessed → *unassessed*; a delta with no prior → "—"',
  'system.findings': 'an empty filter says it is a filter, not "nothing happened"',
  'v4.map':
    'no geometry → absent from seeds, refused by name; per-layer aperture `i`: present / absent / unmeasured / undeclared',
  'system.timeline': 'valid time and record time both shown; a history citation keeps its tense marker verbatim',
  'system.alerts_watches': 'no deliveries in window → stated, not 0%',
  'system.inspector': 'uncited prose under an explicit *uncited* marker',
  'system.consult': 'census classes *not measured*; *writes: none*; replay notice',
  'system.entities': 'the disclosure strip states what is withheld',
  'system.graph_walk': 'withheld edges counted',
  'system.provenance': 'a hop with no receipt says so',
  'system.register': 'intensity read on its stated scale; a pre-rebase value is not compared',
  'system.eval_scorecard':
    'a generated *what this page does not publish*; a null share is *unmeasured*, a measured 0.0 stays 0.0',
  'system.eval_boards': 'sigma comparable only within one scale era (stamp)',
  'system.judge_stats': 'pooled-graders caveat across a pipeline change',
  'system.source_health':
    '`empty`/`ungraded` muted; *staleness not measured* for a non-active source',
  'system.layer_divergence': 'not evaluable / no response / failed / never ran are four answers',
  'analysis.cross_framing': '*absent, not contradicted*; *no typed absence is recorded*',
  'analysis.contentions': 'an empty list "is not a claim that nothing is contested"',
  'system.optimizer': 'the delta reads "not paired" when unpaired',
  'system.goldset': '—',
  'system.journal_gate': '—',
  'system.journal': 'a lens without a stamp shows no prior line',
  'system.report_export': 'a missing date in an endnote stated in words',
  'system.read_scoreboard': 'an empty log is a stated finding',
  'system.status': '—',
  'system.ledgers': 'actors as `bearer:<12 hex>` / `consult_write:<id>`, never a token',
  'system.registry': '—',
  'system.settings': '—',
  'system.flow':
    'a relation the registry does not declare is listed as *undeclared*, never drawn; no stored run → "no run of this analyst is stored"; a prompt never recorded → said so, never rendered fresh',
  [PALETTE_SEARCH]: '—',
}

/**
 * Every kind registered today → the Table A survivor whose contract it
 * answers to (UI-D1 Table B). A kind that survives maps to itself.
 */
export const KIND_SURVIVOR: Record<PanelKind, SurvivorKind | typeof PALETTE_SEARCH> = {
  // (The eight per-subject reads UI-B3 retired onto `subject.page`'s tabs are
  //  alias rows now and answer to its contract through `absenceContractForId`.)
  // → the map and the clock, with a subject param
  'target.map': 'v4.map',
  'target.timeline': 'system.timeline',
  // → unit.page
  'analyst.runs': 'unit.page',
  'analyst.outputs': 'unit.page',
  'analyst.critiques': 'unit.page',
  'analyst.cross_target': 'unit.page',
  // (The twenty Table B kinds UI-B1/merges retired — registry.*, source.*,
  //  the four status reads, the three ledgers, the optimizer diff, v4.kpi,
  //  system.wall_movers, system.navigator — are alias rows now and answer to
  //  their survivor through `absenceContractForId`.)
  // → v4.morning_read
  'v4.assessment': 'v4.morning_read',
  // → the palette's `/` family
  'system.search': PALETTE_SEARCH,
  // survivors, as themselves
  'subject.page': 'subject.page',
  'system.registry': 'system.registry',
  'system.ledgers': 'system.ledgers',
  'system.register': 'system.register',
  'v4.morning_read': 'v4.morning_read',
  'system.wall': 'system.wall',
  'system.findings': 'system.findings',
  'v4.map': 'v4.map',
  'system.timeline': 'system.timeline',
  'system.alerts_watches': 'system.alerts_watches',
  'system.inspector': 'system.inspector',
  'system.consult': 'system.consult',
  'system.entities': 'system.entities',
  'system.graph_walk': 'system.graph_walk',
  'system.provenance': 'system.provenance',
  'system.eval_scorecard': 'system.eval_scorecard',
  'system.eval_boards': 'system.eval_boards',
  'system.judge_stats': 'system.judge_stats',
  'system.source_health': 'system.source_health',
  'system.layer_divergence': 'system.layer_divergence',
  'analysis.cross_framing': 'analysis.cross_framing',
  'analysis.contentions': 'analysis.contentions',
  'system.optimizer': 'system.optimizer',
  'system.goldset': 'system.goldset',
  'system.journal_gate': 'system.journal_gate',
  'system.journal': 'system.journal',
  'system.report_export': 'system.report_export',
  'system.read_scoreboard': 'system.read_scoreboard',
  'system.status': 'system.status',
  'system.settings': 'system.settings',
  'system.flow': 'system.flow',
}

/** The absence contract a registered kind answers to. */
export function absenceContractFor(kind: PanelKind): string {
  return ABSENCE_CONTRACTS[KIND_SURVIVOR[kind]]
}

/**
 * The absence contract ANY id answers to — a registered kind, or a retired id
 * resolved through the alias table onto its survivor. Undefined for an id
 * that is neither.
 */
export function absenceContractForId(id: string): string | undefined {
  const resolved = resolveKind(id)
  return resolved ? absenceContractFor(resolved.kind) : undefined
}
