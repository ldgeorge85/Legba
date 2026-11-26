/**
 * readerVocabulary — the one place that maps the engine's words onto the
 * words a reader sees.
 *
 * Readers (and contributors on reader pages) see plain words. The engine's
 * own terms — the check's internals, the per-claim scores, route paths,
 * producer ids — stay on the operator side or inside the collapsed "How this
 * was produced" readout, where each figure is labelled here in plain words.
 *
 * `engineWords.json` beside this file is the list of engine terms with the
 * reader words that replace them. The same file feeds the source lint
 * (`tools/lint-engine-words.mjs`) and the browser walk, so the three can never
 * disagree about what counts as an engine word.
 *
 * Domain nouns ("desk", "country") are never written here: they come from the
 * profile (`lib/profileUi.ts`).
 */
import raw from '@/lib/engineWords.json'
import { PROFILE_UI, profileUnits, rosterLabel, type ProfileUi } from '@/lib/profileUi'
import { rosterTagOf, type SubjectIndex } from '@/lib/deskNames'
import { getSubjectIndex } from '@/lib/subjectIndex'

export interface EngineTerm {
  term: string
  pattern: RegExp
  /** What a reader sees instead, or where the figure lives now. */
  reader: string
}

interface RawTerm {
  term: string
  pattern: string
  flags: string
  reader: string
}

export const ENGINE_TERMS: readonly EngineTerm[] = (raw.terms as RawTerm[]).map((t) => ({
  term: t.term,
  pattern: new RegExp(t.pattern, t.flags),
  reader: t.reader,
}))

/** Visible texts that look like engine words but are reader words. */
export const READER_ALLOW: ReadonlySet<string> = new Set(raw.allow.texts)

/** The engine terms found in one visible text (empty: none). */
export function engineWordsIn(text: string): string[] {
  const t = text.trim()
  if (!t || READER_ALLOW.has(t)) return []
  return ENGINE_TERMS.filter((e) => e.pattern.test(t)).map((e) => e.term)
}

// ---------------------------------------------------------------------------
// The reader words themselves.
// ---------------------------------------------------------------------------

/**
 * Labels for the instrument readout. Each says what the figure measures, so
 * two figures over different things never sit side by side as bare numbers.
 */
export const READER_WORDS = {
  howProduced: 'How this was produced',
  likelihood: 'How likely',
  confidence: 'How well supported',
  citations: 'Sources cited',
  /** The check of each claim against the sources it cites. */
  heldAgainstSources: 'Claims held against their cited sources',
  /** The same check, over the producer's earlier reads. */
  producerRecord: 'Earlier reads on the same question',
  /** A person read it and said whether it was right. */
  gradedByPerson: 'Graded by a person',
  /** Claims checked against a reference outside this system. */
  outsideReference: 'Checked against an outside reference',
  scale: 'Scale',
  scaleMissing: 'no scale given',
  gaps: 'gaps',
  readsUsed: 'reads used',
  asOf: 'As of',
  recordTitle: 'Record title',
  producer: 'Written by',
} as const

/** `n thing` / `n things`. */
export function countOf(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`
}

/** A share in [0,1] as a whole percent; null stays null (never a zero). */
export function percent(x: number | null | undefined): string | null {
  return typeof x === 'number' && Number.isFinite(x) ? `${Math.round(x * 100)}%` : null
}

/**
 * A producer id said as the profile names it: a read tier's producer is the
 * tier's label ("World"), a unit is its declared label ("Escalation"), and
 * anything else is its id with the underscores gone — never the raw id.
 */
export function producerLabel(id: string | null | undefined, ui: ProfileUi = PROFILE_UI): string | null {
  if (!id) return null
  const frame = ui.frames.find((f) => f.producer === id)
  if (frame) return frame.label
  const unit = profileUnits(ui).find((u) => u.id === id)
  if (unit) return unit.label
  const plain = id.replace(/_/g, ' ').trim()
  return plain ? plain.charAt(0).toUpperCase() + plain.slice(1) : null
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** An instant as a short UTC day, `24 Nov`; null on a non-date. */
export function readableDay(iso: string | null | undefined): string | null {
  if (!iso) return null
  const ms = Date.parse(iso)
  if (!Number.isFinite(ms)) return null
  const d = new Date(ms)
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`
}

/**
 * A subject page's title: the subject's name and a readable day,
 * `Burkina Faso · 24 Nov`. The name is the profile's (the caller passes what
 * `subjectName` resolved); with no date the name stands alone.
 */
export function subjectDayTitle(name: string, iso: string | null | undefined): string {
  const day = readableDay(iso)
  return day ? `${name} · ${day}` : name
}

/**
 * THE ONE CLOCK. Every time the console prints is UTC with a "Z", the way
 * every record states its instants: `18:22Z`. The status line used the
 * browser's local time beside Z-times everywhere else.
 */
export function utcClock(d: Date): string {
  return `${d.toISOString().slice(11, 16)}Z`
}

/**
 * The noun for one subject's KIND, from the profile: a subject on a scored
 * roster leg is the profile's subject noun ("desk"); one on another declared
 * leg is that leg's label ("supply chain"); anything else is the subject noun.
 * Lower case, for a hint ("supply chain · 22m ago").
 */
export function subjectKindNoun(
  id: string,
  index: SubjectIndex = getSubjectIndex(),
  ui: ProfileUi = PROFILE_UI,
): string {
  const tag = rosterTagOf(id, index, ui)
  if (tag && !ui.rosters.scored_subject_tags.includes(tag)) return rosterLabel(tag, ui).toLowerCase()
  return ui.subject.singular.toLowerCase()
}

/** An instant as a short UTC stamp, `2026-11-24 18:22Z`; the input unchanged when it is not a date. */
export function utcStamp(iso: string): string {
  const ms = Date.parse(iso)
  return Number.isFinite(ms) ? `${new Date(ms).toISOString().slice(0, 16).replace('T', ' ')}Z` : iso
}
