/**
 * The engine words, for the walk: read from the console's one list
 * (`src/lib/engineWords.json`), which the console (`lib/readerVocabulary.ts`)
 * and the source lint (`tools/lint-engine-words.mjs`) read too.
 *
 * A reader surface walked AS A READER must show none of them; the same
 * surface walked as an admin may (an admin sees the engine's readout), and is
 * only reported. Pure: no browser, no fixtures.
 */
import fs from 'node:fs'

/** The roles below admin: a reader, a contributor (on reader pages). */
export const READER_ROLES = new Set(['reader', 'read', 'reader-own-rows', 'contributor'])

/** What `/auth/me` answers for the walk's reader pass (an invented account). */
export const READER_IDENTITY = {
  kind: 'user',
  actor: 'user:ex_reader',
  role: 'reader',
  user: { id: 'ex_reader', actor: 'user:ex_reader', email: null, display_name: 'Example reader', role: 'reader', status: 'active', auth_provider: 'password', last_sign_in_at: null },
  session_rejected: false,
}

export function loadEngineWords(file) {
  const doc = JSON.parse(fs.readFileSync(file, 'utf8'))
  const ENGINE_WORDS = doc.terms.map((t) => new RegExp(t.pattern, t.flags))
  return { ENGINE_WORDS, READER_ALLOW: new Set(doc.allow?.texts ?? []), terms: doc.terms }
}

/** The engine words seen on reader surfaces by a reader: `[{ where, words }]`. */
export function readerEngineHits(surfaces) {
  return (surfaces ?? [])
    .filter((s) => s.reader && READER_ROLES.has(s.role) && (s.engineWords ?? []).length > 0)
    .map((s) => ({ where: `${s.name} ${s.width}`, words: s.engineWords }))
}

/** How many reader surfaces were measured as a reader (0: the budget had nothing to hold). */
export function readerSurfacesMeasured(surfaces) {
  return (surfaces ?? []).filter((s) => s.reader && READER_ROLES.has(s.role)).length
}
