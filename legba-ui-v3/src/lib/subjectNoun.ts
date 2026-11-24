/**
 * subjectNoun — the ONE word the screen uses for the thing an analyst studies
 * (X3/D3; review D §C4: "desk, target, subject, lane, country, region — six
 * words for one job").
 *
 * The thing has four names in code — `subject` (the platform noun), `target`
 * (the registry family and the selection kind), `desk` and `country` (the
 * exemplar's words). On screen it has ONE: the profile's subject noun
 * (`PROFILE_UI.subject`, the noun docs/UI.md already prefers — "every subject
 * word on the page is the profile's noun"; the geopolitics exemplar's profile
 * exports "Desk", the platform default is "subject"). The other words are
 * aliases in code only; {@link nounTitle} rewrites a registry title or a
 * frame label that still spells one (a drill titled Target Map, the
 * exemplar's country frame) into the profile's noun before it reaches the
 * screen.
 *
 * A leaf module (it reads the packaged profile and nothing else), so any
 * render path — the Inspector's record links included — can use it without
 * pulling the command registry into its import graph.
 */
import { PROFILE_UI } from '@/lib/profileUi'

/** The profile's subject noun, as the profile spells it ("Desk"). */
export const SUBJECT_NOUN = PROFILE_UI.subject.singular
export const SUBJECT_NOUN_PLURAL = PROFILE_UI.subject.plural

/** The subject's read surface — its page, opened on the brief. */
export const SUBJECT_READ_KIND = 'subject.page'
export const SUBJECT_READ_TAB = 'brief'

/**
 * The words for the studied thing that collapse to the profile's noun —
 * aliases in code only. Exactly one of them is ever on screen.
 */
export const SUBJECT_ALIAS_WORDS = ['subject', 'desk', 'target', 'country'] as const

const ALIAS_RE = /\b(subjects?|desks?|targets?|countr(?:y|ies))\b/gi

/** The collapsed nouns found in `text`, singular, lower-case, deduplicated. */
export function subjectWordsIn(text: string): string[] {
  const out = new Set<string>()
  for (const m of text.matchAll(ALIAS_RE)) {
    const w = m[1].toLowerCase()
    out.add(w.startsWith('countr') ? 'country' : w.replace(/s$/, ''))
  }
  return [...out]
}

function caseLike(model: string, word: string): string {
  if (model.length > 1 && model === model.toUpperCase()) return word.toUpperCase()
  if (model[0] === model[0].toUpperCase()) return word.charAt(0).toUpperCase() + word.slice(1)
  return word.toLowerCase()
}

/**
 * Rewrite any of the alias words in a display string into the profile's noun,
 * keeping the case and the number ("Target Map" → "Desk Map",
 * "Cross-target Analyst" → "Cross-desk Analyst", "countries" → "desks").
 * Never applied to a DECLARED name (a subject's own name is data).
 */
export function nounTitle(text: string): string {
  const singular = SUBJECT_NOUN.toLowerCase()
  const plural = SUBJECT_NOUN_PLURAL.toLowerCase()
  return text.replace(ALIAS_RE, (m) => caseLike(m, /s$/i.test(m) ? plural : singular))
}

/**
 * A selection kind as the screen names it: `target` is the profile's subject
 * noun; every other kind keeps its own word.
 */
export function selectionKindLabel(kind: string): string {
  return kind === 'target' ? SUBJECT_NOUN.toLowerCase() : kind
}
