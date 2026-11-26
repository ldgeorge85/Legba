/**
 * The absence vocabulary — ONE closed list of the phrases a surface may print
 * where a reading is missing, each with what it means (UI-D1 §6.3).
 *
 * Absence renders as absence, never as 0; a MEASURED zero stays a zero. That
 * rule only holds if "absent" is said the same way everywhere and every way of
 * saying it means one thing. These phrases already live in the tree, each
 * argued for in the model that prints it (cited per entry); this module is the
 * place they are listed together, so a new surface picks a phrase from here
 * rather than inventing a fifth way to say "nothing", and a reader who learns
 * a phrase once can trust it on every surface.
 *
 * CLOSED: adding a phrase is a design decision (the vocabulary test pins the
 * id list); re-wording one is a byte-identity change on every surface that
 * prints it (UI-D1 §9) — do neither casually.
 *
 * Platform terms: "subject", "unit", "floor". A profile's own nouns (the
 * exemplar's "desk", "region") are substituted by the surface that knows the
 * profile, never baked in here.
 */

export interface AbsencePhrase {
  /** The exact words printed. Byte-identical across surfaces. */
  phrase: string
  /** What the phrase asserts — and what it does NOT. */
  meaning: string
  /** Where the phrase is argued for in the tree (the model that prints it). */
  source: string
}

export const ABSENCE_VOCABULARY = {
  not_measured: {
    phrase: 'not measured',
    meaning:
      'No measurement was taken for this reading: the route did not run it, or ran it without the input it needs. ' +
      'It says nothing about the value — not zero, not low. Used in a sentence ("not measured for this subject").',
    source: 'lib/absenceModel.ts absenceHeadline; state/consultSession.ts (census)',
  },
  unmeasured: {
    phrase: 'unmeasured',
    meaning:
      'The single-word form of "not measured", for a figure slot: a route that did not answer, a mean with no ' +
      'samples behind it, a roster that has never run. Never printed as 0; a measured zero prints as 0.',
    source: 'lib/coverageFooter.ts UNMEASURED (the honesty footer, the reference implementation)',
  },
  carried_ungraded: {
    phrase: 'carried, ungraded',
    meaning:
      'A memory-only verdict: every citation was the unit\'s own prior read or window ledger, so its score is a ' +
      'capped gate number, not a grade. Counted beside the means ("N carried, ungraded"), never averaged into them.',
    source: 'lib/evalOps.ts CARRIED_UNGRADED',
  },
  floor_honoured: {
    phrase: 'floor honoured',
    meaning:
      'Every group of the composition\'s floor grain that offered a candidate had at least one carried into the ' +
      'record. It asserts coverage of the groups that OFFERED, not of every group on the roster.',
    source: 'lib/assemblyModel.ts floorSentence (W4 F-3)',
  },
  kept_by_floor: {
    phrase: 'kept by the floor',
    meaning:
      'This block is in the record because the floor promoted it to keep its group represented, not because it ' +
      'ranked in on its own order. The surface names the profile\'s floor grain when it knows it ' +
      '(the exemplar prints "kept by the region floor").',
    source: 'lib/assemblyModel.ts floorSentence; v4/read/RecordBlock.tsx',
  },
  last_known_absence: {
    phrase: 'last known absence, not re-checked',
    meaning:
      'A typed absence whose re-check fell due and has not run: it is the last known state, not a current one. ' +
      'It does not say the absence still holds, and it does not say it lifted.',
    source: 'lib/absenceModel.ts absenceHeadline; lib/framingModel.ts',
  },
  no_typed_absence: {
    phrase: 'no typed absence is recorded',
    meaning:
      'The record was read and holds no typed absence for this match. A real answer, not a missing one — it is ' +
      'never dressed up as coverage and never collapsed into "not measured".',
    source: 'lib/absenceModel.ts; lib/framingModel.ts',
  },
  absent_not_contradicted: {
    phrase: 'absent, not contradicted',
    meaning:
      'The units that did not name a claim are silent on it. Silence is not dissent: a unit that does not ' +
      'mention a claim has not argued against it.',
    source: 'panels/analysis/CrossFraming.tsx (the silence row)',
  },
  no_read_yet: {
    phrase: 'no read yet',
    meaning:
      'The unit has produced no read for this subject. It is not a read that found nothing, and not a failure.',
    source: 'lib/deskBriefPage.ts (no_read); v4/why/CountryUnitsAssessment.tsx',
  },
  dash: {
    phrase: '—',
    meaning:
      'The slot has nothing to show and the surrounding label already says why (a delta with no prior, a column ' +
      'a row does not carry). An em dash, never a 0 and never a blank cell.',
    source: 'UI-D1 §6 (Table A: "a delta with no prior → —"); the tables across panels/',
  },
  writes_none: {
    phrase: 'writes: none',
    meaning:
      'A turn armed for writes ended with no write entry: nothing executed, staged or refused. A measured ' +
      'nothing — the turn\'s write bag was read and is empty. A turn not armed for writes prints no line at all.',
    source: 'lib/consultWrites.ts (the truth line, C-2a §9)',
  },
} as const satisfies Record<string, AbsencePhrase>

export type AbsencePhraseId = keyof typeof ABSENCE_VOCABULARY

/** Every phrase id, in declaration order. */
export const ABSENCE_PHRASE_IDS = Object.keys(ABSENCE_VOCABULARY) as AbsencePhraseId[]

/** The exact words for a phrase id. */
export function absencePhrase(id: AbsencePhraseId): string {
  return ABSENCE_VOCABULARY[id].phrase
}

/** What a phrase id means (for a tooltip or a docs table). */
export function absenceMeaning(id: AbsencePhraseId): string {
  return ABSENCE_VOCABULARY[id].meaning
}
