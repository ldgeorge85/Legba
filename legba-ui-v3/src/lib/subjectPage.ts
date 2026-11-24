/**
 * subjectPage — the model behind `subject.page` (UI-B3; UI-D1 §2.1, Table A).
 *
 * One bound page per SUBJECT of any frame kind: a country desk, a lane or a
 * flow, a region, a situation, a regime or a counterparty. Nothing here parses
 * an id (the UI-B4 geometry-contract rule): what a subject is comes from what
 * its descriptor DECLARES (`lib/subjectIndex.ts` — its `scope.tags`), and what
 * a unit is comes from the profile (`lib/profileUi.ts` — ROSTER-2's
 * `rosters.units[]`, with the projection's cadence where the plane answers).
 *
 * Three questions the page asks and this module answers, all pure:
 *
 *   1. **Which tabs** — Brief · Evidence · Claims · Signals · Situations ·
 *      Sources · Graph · Runtime, platform words (UI-D1 Table A).
 *   2. **Which units are THIS subject's roster** ({@link subjectRoster}) — the
 *      evidence table's denominator. A subject on a scored leg carries the
 *      scored subjects' units (the Python twin is
 *      `legba.data.rosters.scored_subject_units`); a subject on another leg
 *      carries only the units scoped to a tag it declares (a supply-chain
 *      lane carries its lane unit and nothing country-shaped); a profile that
 *      sets no leg apart fans every unscoped unit over every subject. A
 *      subject whose descriptor has not been read has NO roster yet, and says
 *      so — it never borrows a country desk's.
 *   3. **Which records compose it** ({@link compositionProducers}) — every
 *      frame the profile declares with a producer. The newest such row on the
 *      subject is its composition; none → "no composition on record for this
 *      subject" (the absence contract).
 */
import type { PanelTabDef } from '@/components/PanelTabs'
import { declaredSubject, type SubjectIndex } from '@/lib/deskNames'
import { getSubjectIndex } from '@/lib/subjectIndex'
import { UNIT_CADENCE_HOURS } from '@/lib/gapStripModel'
import {
  PROFILE_UI,
  profileUnits,
  subjectUnits,
  type ProfileUi,
  type ProfileUnit,
  type ProjectionRow,
} from '@/lib/profileUi'

// ---------------------------------------------------------------------------
// 1 · the tabs
// ---------------------------------------------------------------------------

// One line: `tests/data_pkg/test_consult_case_schema.py` reads this union as
// the drive vocabulary's tab enum for `subject.page` (the drift guard).
export type SubjectTab = 'brief' | 'evidence' | 'claims' | 'signals' | 'situations' | 'sources' | 'graph' | 'runtime'

/** UI-D1 Table A's order: the read first, the machinery last. */
export const SUBJECT_TABS: readonly PanelTabDef[] = [
  { id: 'brief', label: 'Brief' },
  { id: 'evidence', label: 'Evidence' },
  // Label per the vocabulary proposal (DATA_LAYERS 11-14): the tab lists the
  // subject's FINDINGS (analyst_outputs kind 'finding'), so it says so; the id
  // stays `claims` (hashes, saved layouts, recorded cases and the drive
  // vocabulary name it).
  { id: 'claims', label: 'Findings' },
  { id: 'signals', label: 'Signals' },
  { id: 'situations', label: 'Situations' },
  { id: 'sources', label: 'Sources' },
  { id: 'graph', label: 'Graph' },
  { id: 'runtime', label: 'Runtime' },
]

export const SUBJECT_DEFAULT_TAB: SubjectTab = 'brief'

/**
 * The modes each tab ships in — the retired kind's own `modes`, carried over
 * so a merge never widens a surface's audience: Signals and Sources were
 * personal-only kinds and stay personal-only tabs.
 */
const TAB_MODES: Readonly<Record<SubjectTab, readonly string[]>> = {
  brief: ['personal', 'cis'],
  evidence: ['personal', 'cis'],
  claims: ['personal', 'cis'],
  signals: ['personal'],
  situations: ['personal', 'cis'],
  sources: ['personal'],
  graph: ['personal', 'cis'],
  runtime: ['personal', 'cis'],
}

/** The tabs a mode shows (every tab when the mode is unknown). */
export function subjectTabsFor(mode: string | null | undefined): PanelTabDef[] {
  return SUBJECT_TABS.filter((t) => !mode || (TAB_MODES[t.id as SubjectTab] ?? []).includes(mode))
}

/** The key a subject page's tab requests ride (`state/panelParams.ts`): the
 *  bound page's own Dockview id, so opening one subject's page on a tab never
 *  moves another subject's page; the unbound (stance-seeded) page is the kind. */
export function subjectPageKey(boundSubject: string | null): string {
  return boundSubject ? `subject.page:${boundSubject}` : 'subject.page'
}

// ---------------------------------------------------------------------------
// 2 · the subject's own unit roster
// ---------------------------------------------------------------------------

/** One unit on a subject's roster — the evidence table's row key. */
export interface SubjectRosterUnit {
  id: string
  label: string
  short: string
  /** Hours between scheduled fires: the projection's declared cadence when the
   *  plane answered, else the units' shared descriptor cadence. */
  cadenceHours: number
  /** The profile's standing question for the unit, or null. */
  question: string | null
  scopeNote: string | null
}

/**
 * Why the roster is what it is:
 *   - `scored`     — the subject is on a scored leg: the scored subjects' units;
 *   - `leg`        — the subject is on another leg: the units scoped to its tags;
 *   - `every`      — the profile sets no leg apart: every unscoped unit, plus
 *                    the units scoped to a tag the subject declares;
 *   - `unresolved` — the subject's descriptor has not been read (or the
 *                    registry does not serve the id): no roster is guessed.
 */
export type RosterBasis = 'scored' | 'leg' | 'every' | 'unresolved'

export interface SubjectRoster {
  units: SubjectRosterUnit[]
  basis: RosterBasis
  /** The basis in one sentence, the profile's nouns substituted. */
  reason: string
}

function rosterUnit(u: ProfileUnit, cadence: ReadonlyMap<string, number> | undefined): SubjectRosterUnit {
  return {
    id: u.id,
    label: u.label,
    short: u.short,
    cadenceHours: cadence?.get(u.id) ?? UNIT_CADENCE_HOURS,
    question: u.question ?? null,
    scopeNote: u.scopeNote,
  }
}

/** The projection's `units` rows → unit id → declared cadence (hours). */
export function cadenceFromProjection(rows: readonly ProjectionRow[]): Map<string, number> {
  const out = new Map<string, number>()
  for (const r of rows) {
    const v = r.value && typeof r.value === 'object' ? (r.value as Record<string, unknown>) : {}
    const h = v.cadence_hours
    if (typeof h === 'number' && Number.isFinite(h) && h > 0) out.set(r.key, h)
  }
  return out
}

/** The profile's subject noun, lower-case (the exemplar's "desk"). */
export function subjectNoun(ui: ProfileUi = PROFILE_UI): string {
  return ui.subject.singular.toLowerCase()
}

/** The profile's unit noun, plural (the exemplar's "units"). */
function unitsNoun(ui: ProfileUi): string {
  return ui.unit_noun.plural.toLowerCase()
}

/**
 * THIS subject's bounded units, roster order (see the module header). The
 * index and the profile are injectable for tests; production reads the
 * session's subject roster and this build's profile.
 */
export function subjectRoster(
  subjectId: string | null | undefined,
  opts: { index?: SubjectIndex; ui?: ProfileUi; cadence?: ReadonlyMap<string, number> } = {},
): SubjectRoster {
  const ui = opts.ui ?? PROFILE_UI
  const noun = subjectNoun(ui)
  const subject = declaredSubject(subjectId, opts.index ?? getSubjectIndex())
  if (!subject) {
    return {
      units: [],
      basis: 'unresolved',
      reason:
        `this ${noun}'s descriptor has not been read, so its ${unitsNoun(ui)} are not resolved — ` +
        `no roster is assumed`,
    }
  }
  const tags = new Set(subject.tags)
  const scored = ui.rosters.scored_subject_tags
  const map = (list: ProfileUnit[]) => list.map((u) => rosterUnit(u, opts.cadence))
  if (scored.some((t) => tags.has(t))) {
    return {
      units: map(subjectUnits(ui)),
      basis: 'scored',
      reason: `the ${unitsNoun(ui)} every scored ${noun} carries`,
    }
  }
  if (scored.length === 0) {
    return {
      units: map(profileUnits(ui).filter((u) => u.scopeTag === null || tags.has(u.scopeTag))),
      basis: 'every',
      reason: `the profile sets no ${noun} leg apart: every unscoped ${ui.unit_noun.singular.toLowerCase()} fans out over every ${noun}`,
    }
  }
  const own = profileUnits(ui).filter((u) => u.scopeTag !== null && tags.has(u.scopeTag))
  return {
    units: map(own),
    basis: 'leg',
    reason:
      own.length > 0
        ? `the ${unitsNoun(ui)} scoped to a tag this ${noun} declares (${subject.tags.join(', ') || 'none'})`
        : `no ${ui.unit_noun.singular.toLowerCase()} fans out over this ${noun}'s tags (${subject.tags.join(', ') || 'none'})`,
  }
}

// ---------------------------------------------------------------------------
// 3 · the records that compose a subject
// ---------------------------------------------------------------------------

/** Every producer a profile frame declares — the composition candidates. */
export function compositionProducers(ui: ProfileUi = PROFILE_UI): string[] {
  const out: string[] = []
  for (const f of ui.frames) if (f.producer && !out.includes(f.producer)) out.push(f.producer)
  return out
}

/** The absence line for a subject with no composition (UI-D1 Table A, verbatim
 *  with the profile's noun), and the sentence that says what was asked. */
export function noCompositionLines(
  subjectId: string,
  producers: readonly string[],
  ui: ProfileUi = PROFILE_UI,
): { headline: string; detail: string } {
  const noun = subjectNoun(ui)
  return {
    headline: `No composition on record for this ${noun}.`,
    detail:
      producers.length > 0
        ? `No ${producers.join(' / ')} row has been published for ${subjectId}, so there is no record to read this ${noun} from. The ${unitsNoun(ui)} below still state what each has read.`
        : `This instance declares no composition producer on any frame, so no record composes ${subjectId}. The ${unitsNoun(ui)} below still state what each has read.`,
  }
}
