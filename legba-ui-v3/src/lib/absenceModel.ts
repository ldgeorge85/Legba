/**
 * absenceModel — the reader-side model of TYPED ABSENCE (lane k5).
 *
 * `GET /api/v1/v3/absence?scope=<desk>` answers one desk's absences in a single
 * named vocabulary of SEVEN kinds, each item carrying a PROOF (what was
 * checked, when, and the ref that holds the record of that look) and a CLOCK
 * (when it was measured, when it stops being current, and whether that moment
 * has already passed). Those two things are the whole difference between a
 * typed absence and a blank: a blank says nothing was shown; an absence says
 * what was looked at, when, and whether anyone has looked since.
 *
 * This module is DOM-free and holds what the UI needs on both sides of the
 * drill:
 *
 *   * the wire types and the kind labels (pinned against the route's own
 *     vocabulary by `tests/data_pkg/test_absence_api.py` — a kind the reader
 *     cannot name would render as the blank this lane exists to abolish);
 *   * `matchAbsence`, the cell → item lookup the gap strip's "Why absent" drill
 *     uses, and `absenceHeadline`, which states a stale item as LAST KNOWN,
 *     NOT RE-CHECKED rather than letting an old absence read as a current one;
 *   * the selection-id encoding that carries a click to the Inspector without
 *     inventing a record id (there is no row to select — an absence is
 *     precisely the case where the record does not exist).
 *
 * WHY AN ID AND NOT A PREVIEW. The Inspector resolves detail from the shared
 * selection, so a drill has to be expressible as `(kind, id)`. `scope|kind|
 * subject` is that id: every component is drawn from a charset that excludes
 * `|` (a `target_id` matches `[A-Za-z0-9_.-]`, the absence kind is a closed
 * vocabulary, and a subject is a unit id, a layer name, a `source.*` id or a
 * hex claim key), so the encoding round-trips exactly and `parseAbsenceId`
 * refuses anything that does not.
 *
 * NOTHING IS DERIVED LOCALLY. The strip classifies its own cells
 * (`gapStripModel.ts`) and the ROUTE classifies and stamps absences; this
 * module only matches one to the other and reports honestly when there is no
 * match — "no typed absence is recorded" is a real answer and is never dressed
 * up as a proof that was not produced.
 */

/** The closed kind vocabulary — mirrors `ABSENCE_KINDS` in `absence_api.py`. */
export type AbsenceKind =
  | 'not_collected'
  | 'collected_but_silent'
  | 'source_stale'
  | 'searched_found_nothing'
  | 'search_failed'
  | 'below_floor'
  | 'layer_declared_absent'
  | 'history_gap'

export interface AbsenceProof {
  what_was_checked: string
  checked_at: string | null
  ref: string | null
  /** What `ref` IS, said by the route rather than inferred from its shape —
   *  so a drill offers a link only where one resolves. */
  ref_kind: 'finding' | 'scorecard' | 'source' | 'map_version' | 'collection_load' | null
}

export interface AbsenceItem {
  kind: AbsenceKind
  subject: string
  /** The instant the absence has held from, when one is on record. */
  since: string | null
  /** The bounded period, when there is no instant to point at. */
  window: string | null
  reason: string
  /** When the absence was MEASURED — a run / receipt / scan instant, never
   *  the moment the page asked. `null` only when no such instant exists. */
  as_of: string | null
  /** Which instant `as_of` is, in words. Always present. */
  as_of_basis: string
  /** When it stops being current: the next scheduled run of the thing that
   *  measured it. `null` where no schedule governs it (see `review`). */
  expires_at: string | null
  /** What would supersede it instead of a schedule (a map revision). */
  review?: string | null
  /** `expires_at` is already past: last known, not re-checked. */
  stale: boolean
  proof: AbsenceProof
}

export interface AbsenceResponse {
  version: string
  scope: string
  /** When this READ happened — never an item's `as_of`. */
  read_at: string
  /** The eight kinds with the route's own one-line meaning for each. */
  kinds: Record<string, string>
  absences: AbsenceItem[]
  /** Kinds that could not be read for this scope, each with its reason.
   *  A kind in neither this list nor `absences` was read and found nothing. */
  not_measured: string[]
}

/** Short human labels for the eight kinds — the glossary's own wording. */
export const ABSENCE_KIND_LABEL: Record<AbsenceKind, string> = {
  not_collected: 'not collected',
  collected_but_silent: 'collected but silent',
  source_stale: 'source stale',
  searched_found_nothing: 'searched, found nothing',
  search_failed: 'search failed',
  below_floor: 'below floor',
  layer_declared_absent: 'layer declared absent',
  history_gap: 'history gap',
}

/**
 * The eight kinds in the ROUTE's own declaration order, explicit rather than
 * inferred from an object's key order — a reader surface that renders the
 * vocabulary must render all of it, in the order the vocabulary is published,
 * and a silently reordered `Object.keys` is not that guarantee.
 * `tests/data_pkg/test_absence_api.py` pins this list against `ABSENCE_KINDS`.
 *
 * `history_gap` (7g-2) is the eighth and the only one about the PAST: a
 * curated collection declares a series-and-subject for this desk and the
 * observations table does not hold it. Its proof is a LOAD receipt
 * (`ref_kind: 'collection_load'`), which no reader surface resolves to a
 * selection — so it renders as text, exactly as `scorecard` and `map_version`
 * already do, rather than as a click that would 404.
 */
export const ABSENCE_KIND_ORDER: readonly AbsenceKind[] = [
  'not_collected',
  'collected_but_silent',
  'source_stale',
  'searched_found_nothing',
  'search_failed',
  'below_floor',
  'layer_declared_absent',
  'history_gap',
]

/**
 * The ONE sentence every absence surface carries back to the glossary entry.
 *
 * Byte-identical to `GLOSSARY_NOTE` in `registry/export_absences.py` — the
 * printed desk brief carries the server's copy and the Morning Read carries
 * this one, and `tests/data_pkg/test_export_absences.py` fails if they drift.
 * A reader who meets typed absence on two surfaces must not be told two
 * slightly different things about what it is.
 */
export const TYPED_ABSENCE_GLOSSARY_NOTE =
  'Typed absence (docs/GLOSSARY.md): an absence with a scope, a kind, a ' +
  'proof and a shelf life, never a blank. Seven kinds, one closed ' +
  'vocabulary; every item says what was looked at, when, and whether ' +
  'anyone has looked since.'

const SEPARATOR = '|'

/** Encode a drill target as a selection id: `scope|kind|subject`. */
export function absenceId(scope: string, kind: AbsenceKind, subject: string): string {
  return [scope, kind, subject].join(SEPARATOR)
}

export interface ParsedAbsenceId {
  scope: string
  kind: AbsenceKind
  subject: string
}

/**
 * Decode a selection id. Returns `null` for anything that is not exactly three
 * non-empty `|`-separated parts naming a known kind — a malformed id must
 * degrade to the Inspector's raw-selection fallback, never to a half-parsed
 * lookup that silently matches the wrong absence.
 */
export function parseAbsenceId(id: string): ParsedAbsenceId | null {
  const parts = id.split(SEPARATOR)
  if (parts.length !== 3) return null
  const [scope, kind, subject] = parts
  if (!scope || !kind || !subject) return null
  if (!(kind in ABSENCE_KIND_LABEL)) return null
  return { scope, kind: kind as AbsenceKind, subject }
}

/** The one item in a response that answers `(kind, subject)`, or `null`. */
export function matchAbsence(
  resp: AbsenceResponse | null | undefined,
  kind: AbsenceKind,
  subject: string,
): AbsenceItem | null {
  if (!resp) return null
  return resp.absences.find((a) => a.kind === kind && a.subject === subject) ?? null
}

/**
 * The first item for a subject under ANY kind.
 *
 * The gap strip knows which UNIT a cell is for but not which kind the route
 * will have classed it under — a silent unit is `not_collected` or
 * `source_stale` depending on whether it ever ran, and that is the route's
 * call, not the cell's. Matching by subject keeps the two from having to agree
 * on a classification neither owns alone.
 */
export function matchAbsenceSubject(
  resp: AbsenceResponse | null | undefined,
  subject: string,
  kinds?: AbsenceKind[],
): AbsenceItem | null {
  if (!resp) return null
  return (
    resp.absences.find(
      (a) => a.subject === subject && (!kinds || kinds.includes(a.kind)),
    ) ?? null
  )
}

/**
 * The `not_measured` entry covering a kind, when there is one.
 *
 * This is what lets a reader surface distinguish "this kind was not checked
 * for this desk" from "it was checked and nothing is absent" — two answers a
 * blank panel would collapse into one.
 *
 * THE MATCH IS PER KIND, NOT A PREFIX. The wire format is `"<kinds…>: <why>"`
 * and ONE entry can cover two kinds, each optionally qualified:
 * `"not_collected / source_stale (units): …"`. A prefix match finds the first
 * kind named and silently misses the second, which leaves a kind the route
 * said it could not read rendering as one it read and found nothing under —
 * exactly the collapse this function exists to prevent. So each `/`-separated
 * part of the head is reduced to its leading identifier and compared.
 *
 * Mirrors `_not_measured_reason` in `registry/export_absences.py`.
 */
export function notMeasuredReason(
  resp: AbsenceResponse | null | undefined,
  kind: AbsenceKind,
): string | null {
  // A response with no `not_measured` block is one that named no unread kind;
  // it must read as "nothing unmeasured", never throw on a reader surface.
  const entries = resp?.not_measured
  if (!Array.isArray(entries)) return null
  for (const entry of entries) {
    if (typeof entry !== 'string') continue
    const colon = entry.indexOf(':')
    const head = colon >= 0 ? entry.slice(0, colon) : entry
    const covered = head
      .split('/')
      .some((part) => part.trim().split(/\s+/, 1)[0] === kind)
    if (!covered) continue
    const why = colon >= 0 ? entry.slice(colon + 1).trim() : ''
    return why || head.trim()
  }
  return null
}

/** The extent line for an item: its `since`, else its `window`, else absent. */
export function absenceExtent(item: AbsenceItem): string {
  if (item.since) return `since ${new Date(item.since).toLocaleString()}`
  if (item.window) return item.window
  return 'extent not recorded'
}

/**
 * The one line that states WHEN this absence was established and whether it
 * still holds. A stale item says so in words: the measurement that produced it
 * was due to repeat and has not, so it is the last known state and not a
 * current one.
 */
export function absenceHeadline(item: AbsenceItem): string {
  const measured = item.as_of
    ? `measured ${new Date(item.as_of).toLocaleString()} (${item.as_of_basis})`
    : `not measured — ${item.as_of_basis}`
  if (item.stale && item.expires_at) {
    return `${measured} · last known absence, not re-checked (due ${new Date(
      item.expires_at,
    ).toLocaleString()})`
  }
  if (item.expires_at) {
    return `${measured} · current until ${new Date(item.expires_at).toLocaleString()}`
  }
  return `${measured} · ${item.review ? `revised by ${item.review}` : 'no recheck scheduled'}`
}
