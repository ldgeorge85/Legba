/**
 * Citations data layer (P1-T3 — the cited assessment card).
 *
 * Two citation shapes coexist in a finding's `data.citations` list, told apart
 * by the presence of `ref_kind` (new) vs `signal_id` (old/unit):
 *
 *   UNIT (inline_target, real signals) — `[N]` prose markers:
 *     { marker:'[N]', signal_id:'<signal uuid>', title?, source? }
 *
 *   COMPOSITION (meta_findings_synthesizer — country_composition + world) —
 *   `[[ref:N]]` ordinal prose markers that cite a SUB-CLAIM finding:
 *     { marker:'[[ref:N]]', ordinal:N, ref_id:'<finding uuid>', ref_kind:'finding',
 *       title?, source?, evidence_text?, effective_confidence?, derived_from? }
 *
 * The finding payload NESTS its own data under the `analyst_outputs.data`
 * envelope, so for a row read through the lineage root (`root.body`) or
 * `/findings` (`row.data`) the citation list lives at `<merged-body>.data.citations`
 * and the cited PROSE (with the inline markers) lives at the envelope top level
 * as `<merged-body>.body`. This module reads both DEFENSIVELY — a legacy /
 * uncited finding simply has no `.data.citations`, in which case
 * `extractCitations` returns `[]` and the card renders its prose plainly with an
 * honest "uncited" marker (NO fabricated anchor).
 *
 * A citation carries which KIND it drills: a composition finding-ref
 * (`refKind:'finding'`, from `ref_id`) drills to the sub-claim finding card, a
 * unit/legacy signal-ref (`refKind:'signal'`, from `signal_id`) drills to the
 * signal — back-compatible: an old row with only `signal_id` reads as a signal
 * ref exactly as before.
 *
 * ONE EXCEPTION to "ordinal N is entry N": a `region_rollup.v1` row written
 * before the 2026-09-07 producer fix carries its citations in the SLICE order
 * while its body numbers member sections in the ROSTER order. `extractCitations`
 * re-maps those onto the rendered order — see {@link rollupAlignedCitations},
 * the client half of `export_api._rollup_aligned_citations`. Every other row
 * passes through untouched.
 *
 * Pure, DOM-free: the card component composes these helpers.
 */

/**
 * The DESK GROUNDING block kinds (backend
 * `provenance.kinds.GROUNDING_REF_KINDS`). A grounding citation's ordinal
 * indexes a block the desk was SHOWN — its own prior read, its trailing
 * window ledger, the open-situation register, the desk baseline, the standing
 * open questions, the desk's open events — not a slice row. The synthetic
 * ones carry NO `ref_id` by design (minting one so a drill link resolves
 * would be a fabricated anchor); `prior_read` has a real `analyst_outputs`
 * id, and that id is a FINDING, never a signal.
 *
 * `observation` (7g-2) is the odd one and the only one that is not about this
 * platform: it names ONE row of a curated historical holding, with a real
 * uuid of its own and the whole row carried on the citation (see
 * {@link CitationObservation}). It is a grounding kind because it has no
 * `signal_id` and is graded on its own captured text — not because it is
 * memory.
 */
export type GroundingKind =
  | 'prior_read'
  | 'window_ledger'
  | 'situation_register'
  | 'desk_baseline'
  | 'open_questions'
  | 'open_events'
  | 'observation'

const GROUNDING_KINDS: ReadonlySet<string> = new Set<GroundingKind>([
  'prior_read',
  'window_ledger',
  'situation_register',
  'desk_baseline',
  'open_questions',
  'open_events',
  'observation',
])

/** The structural mark `unit_grounding` stamps on every grounding citation. */
export const MARKER_CLASS_GROUNDING = 'desk_grounding'

/** Human labels for the chip / hover card. Kind-labeled, never "Unresolved". */
const GROUNDING_LABEL: Record<GroundingKind, string> = {
  prior_read: 'prior read',
  window_ledger: 'window ledger',
  situation_register: 'situation register',
  desk_baseline: 'desk baseline',
  open_questions: 'open questions',
  open_events: 'open events',
  observation: 'historical observation',
}

/** Fallback titles for a grounding block whose stored entry carries none. */
const GROUNDING_TITLE: Record<GroundingKind, string> = {
  prior_read: "Prior read (this unit's previous verified read)",
  window_ledger: "Window ledger (this unit's trailing 14-day record)",
  situation_register: 'Open-situation register',
  desk_baseline: 'Desk baseline',
  open_questions: 'Standing open questions',
  open_events: "Open events on this desk's open frames",
  observation: 'Historical observation',
}

/**
 * The cited HISTORICAL OBSERVATION's own row, carried on the citation by the
 * producer (`analysts.history_grounding.observation_citation`) so the chip
 * resolves the number WITHOUT a round trip — which is what makes it legible
 * in an exported document, where most citations are actually read.
 *
 * `staleTense` is the backend's rendered marker, verbatim: `(historical:
 * valid YYYY..YYYY, recorded YYYY-MM)`. It is never re-derived here — one
 * string, one producer, three surfaces (prompt, endnote, chip), so a past
 * figure reads as past identically on all of them.
 */
export interface CitationObservation {
  collectionId?: string
  seriesId?: string
  provider?: string
  indicatorName?: string
  subject?: string
  value?: string
  unit?: string
  validFrom?: string
  validTo?: string
  recordTime?: string
  sourceUrl?: string
  sha256?: string
  licenceClass?: string
  staleTense?: string
}

/** The record kind a citation drills into, or the grounding block it names. */
export type CitationRefKind = 'finding' | 'signal' | GroundingKind

/** One citation: a marker that maps to the record it cites. */
export interface Citation {
  /** The inline marker exactly as it appears in the prose, e.g. "[8]" or
   *  "[[ref:3]]". */
  marker: string
  /** The cited record's substrate id (a finding uuid for a composition ref, a
   *  signal uuid for a unit/legacy ref). */
  refId: string
  /** Which record the ref drills — a composition sub-claim finding or a signal. */
  refKind: CitationRefKind
  /** The cited source title, when present. */
  title?: string
  /** The cited source URL / origin, when present. */
  source?: string
  /**
   * The cited OUTLET ref (`signals.source_id` — "source.bbc.world") on a unit
   * citation. Absent on a composition citation (whose `source` is the cited
   * desk's analyst id) and on every row written before V-H1 stamped it.
   * The masthead a reader recognises is derived from this first — see
   * {@link citationMasthead}.
   */
  sourceId?: string
  /**
   * The cited head's own `produced_at` (D-2b), ISO-8601. Composition-only and
   * guarded: a head with no timestamp simply carries none, so the source tag
   * prints a masthead with no date rather than an invented one.
   */
  producedAt?: string
  /**
   * 7d — the cited signals behind THIS claim fold to ONE outlet. Written by
   * the producer only when TRUE (`source_independence.independence_of`), so
   * absent means "two or more independent outlets, or UNKNOWN" (a composition
   * origin cites findings, not signals) and NEVER "cleared".
   */
  singleSource?: boolean
  /**
   * 7d — at least one outlet behind this claim was absorbed by the wire /
   * same-publisher fold. The citation carries the BOOLEAN; the COUNT lives on
   * the rendered record's corroboration block (see `lib/claimFold.ts`), which
   * is computed off the identical cited-signal list, so the two cannot
   * disagree about a block.
   */
  wireFolded?: boolean
  /**
   * True when the producer wrote a `derived_from` key that is EMPTY — the
   * cited sub-claim records no basis at all. Distinct from an ABSENT key (a
   * unit/legacy citation, which says nothing about basis): `basis == []` is
   * the platform's own insufficient-evidence shape (`scorecard_banding`: "An
   * insufficient verdict always carries ``basis=[]``"), so it is carried as
   * its own bit rather than collapsed into `derivedFrom === undefined`.
   */
  emptyBasis?: boolean
  /**
   * The cited PASSAGE — a composition sub-claim's `evidence_text` or a unit
   * signal's `snippet`. The point-in-time text the citation rests on, surfaced
   * in the hover-card so the reader can check the marker without a drill. Absent
   * on a legacy/uncited citation (honest — the card shows no passage, never a
   * fabricated one).
   */
  evidenceText?: string
  /**
   * The cited sub-claim's `effective_confidence` (already `min(confidence,
   * faithfulness)` from its own verify pass) — the analytic credibility ceiling
   * this citation carries. Composition-only; absent (undefined) for a unit/legacy
   * citation, never coerced to 0.
   */
  effectiveConfidence?: number
  /** The cited sub-claim's underlying lineage/signal ids (composition-only). */
  derivedFrom?: string[]
  /**
   * The producer's structural mark — `'desk_grounding'` when this ordinal
   * indexes a DESK GROUNDING block. Present only on rows written on or after
   * the `2026-08-30/1` stamp; ABSENT (undefined) on every earlier row and on
   * every signal / sub-claim ref, so it is read as "grounding when present",
   * never as a field every citation must supply. `refKind` remains the
   * fallback discriminator for the pre-stamp population — see
   * {@link isGroundingCitation}.
   */
  markerClass?: string
  /**
   * The SET this ordinal is a position in, as the producer spelled it
   * (`'data.citations'` for a grounding block). Carried through rather than
   * inferred, because inferring it is exactly the step that produced the
   * falsified 08-27 "53.6% unresolved citations" red.
   */
  resolvesAgainst?: string
  /**
   * 7g-2 — the cited HISTORICAL OBSERVATION's own row. Present ONLY on an
   * `observation` citation; absent everywhere else, so it is read as
   * "an observation when present" rather than as a field every citation must
   * supply.
   */
  observation?: CitationObservation
  /**
   * @deprecated Back-compat alias for `refId`, kept so existing signal-only
   * consumers (e.g. the evidence EntityGraph) compile unchanged. Prefer
   * `refId` + `refKind`; for a unit/signal citation this equals the signal id
   * exactly as before.
   */
  signalId: string
}

function str(v: unknown): string | undefined {
  return typeof v === 'string' && v.length > 0 ? v : undefined
}

/** Normalize the marker to the bracketed form the prose uses. A raw `N` or `8`
 *  is wrapped to `[8]`; an already-bracketed `[8]` or `[[ref:3]]` is kept.
 *  Empty → undefined. */
export function normalizeMarker(raw: unknown): string | undefined {
  const s = str(raw)
  if (!s) return undefined
  const t = s.trim()
  if (t.startsWith('[') && t.endsWith(']')) return t
  return `[${t}]`
}

/**
 * Pull the citation list out of a merged finding body. Reads the nested
 * envelope path first (`body.data.citations` — the live shape) and falls back
 * to a top-level `body.citations` for forward-compatibility. Each entry reads
 * `ref_id` + `ref_kind` (a composition finding-ref) with a `signal_id` fallback
 * (a unit/legacy signal-ref). Anything without a marker + a resolvable id is
 * skipped (never throws, never fabricates).
 */
export function extractCitations(body: Record<string, unknown> | null | undefined): Citation[] {
  if (!body || typeof body !== 'object') return []
  const inner = body['data']
  const nested =
    inner && typeof inner === 'object'
      ? (inner as Record<string, unknown>)['citations']
      : undefined
  const raw = Array.isArray(nested)
    ? nested
    : Array.isArray(body['citations'])
      ? (body['citations'] as unknown[])
      : []
  const out: Citation[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const o = item as Record<string, unknown>
    const marker = normalizeMarker(o['marker'])
    const refId = str(o['ref_id']) ?? str(o['signal_id']) ?? str(o['signalId'])
    const rawKind = str(o['ref_kind'])
    const markerClass = str(o['marker_class'])
    // A DESK GROUNDING block. All five are real citations the verify plane
    // scores as SUPPORTED evidence; four carry no `ref_id` at all, so the
    // `!refId` skip below used to drop them and the reading kit rendered an
    // amber "Unresolved citation" chip OVER evidence the grader accepted —
    // and typed `prior_read` (which does have an id) as a SIGNAL, drilling to
    // a signal row that cannot exist. Keyed on the producer's `marker_class`
    // first, falling back to the registered kind vocabulary for rows written
    // before that mark existed.
    if (markerClass === MARKER_CLASS_GROUNDING || (rawKind && GROUNDING_KINDS.has(rawKind))) {
      if (!marker) continue
      // A row marked `desk_grounding` whose `ref_kind` is not in the registry
      // (a sixth kind this bundle predates) is carried VERBATIM rather than
      // guessed into one of the five. Defaulting it to `prior_read` would be
      // the worst possible guess — that is the one grounding kind that drills,
      // so an unknown block would render a link to whatever id it happened to
      // carry. Unknown → no drill, and the kind string is its own label.
      const kind = (rawKind || MARKER_CLASS_GROUNDING) as GroundingKind
      // Only the prior read has a drill target, and it is an analyst_outputs
      // row — a FINDING. The other four keep an empty id so no surface can
      // mint a link out of them.
      // 7g-2: `observation` joins `prior_read` in carrying a real id — an
      // observations row has one, so keeping it is not a fabricated anchor.
      const groundingId =
        kind === 'prior_read' || kind === 'observation' ? (refId ?? '') : ''
      const cite: Citation = {
        marker,
        refId: groundingId,
        refKind: kind,
        signalId: '', // never a signal — the deprecated alias stays empty
        title: str(o['title']) ?? GROUNDING_TITLE[kind] ?? 'Desk grounding block',
        source: undefined,
      }
      if (kind === 'observation') {
        // The source URL is a REAL external target (the provider file the
        // number was read out of), so it rides the chip's `source` slot
        // exactly as a signal's canonical URL does.
        cite.source = str(o['source'])
        const obs = o['observation']
        if (obs && typeof obs === 'object') cite.observation = readObservation(obs)
      }
      if (markerClass) cite.markerClass = markerClass
      const target = str(o['resolves_against'])
      if (target) cite.resolvesAgainst = target
      const passage = str(o['evidence_text'])
      if (passage) cite.evidenceText = passage
      out.push(cite)
      continue
    }
    const refKind: CitationRefKind = rawKind === 'finding' ? 'finding' : 'signal'
    if (!marker || !refId) continue
    const cite: Citation = {
      marker,
      refId,
      refKind,
      signalId: refId, // deprecated compat alias (see Citation.signalId)
      title: str(o['title']),
      source: str(o['source']),
    }
    // Hover-card fields — set ONLY when the payload carries them, so an
    // uncited/legacy citation never gains a fabricated passage or credibility.
    // Composition sub-claims carry `evidence_text`; unit signals carry `snippet`.
    const passage = str(o['evidence_text']) ?? str(o['snippet'])
    if (passage) cite.evidenceText = passage
    const eff = o['effective_confidence']
    if (typeof eff === 'number' && Number.isFinite(eff)) cite.effectiveConfidence = eff
    const derived = o['derived_from']
    if (Array.isArray(derived) && derived.length > 0) {
      cite.derivedFrom = derived.filter((d): d is string => typeof d === 'string')
    } else if (Array.isArray(derived)) {
      // Present AND empty — the cited head records no basis. Carried as its
      // own bit so "no key at all" (a unit citation) stays distinguishable
      // from "a key that says nothing is behind this".
      cite.emptyBasis = true
    }
    // 7b-i — the cited SOURCE beside the claim: the outlet ref and the cited
    // head's date, both read verbatim off the row. Guarded like every other
    // hover-card field, so a legacy citation gains neither.
    const sourceId = str(o['source_id'])
    if (sourceId) cite.sourceId = sourceId
    const producedAt = str(o['produced_at'])
    if (producedAt) cite.producedAt = producedAt
    // 7d — the fold stamps. Both are written by the producer ONLY when true,
    // and both are read as "true when present", never defaulted to false: a
    // composition origin's independence is UNKNOWN, not cleared.
    if (o['single_source'] === true) cite.singleSource = true
    const folded = o['wire_folded']
    // `composition_citations` stamps the citation's key as a BOOLEAN while the
    // record's corroboration block stamps a COUNT; a positive number here is
    // accepted as the same statement rather than dropped on the shape.
    if (folded === true || (typeof folded === 'number' && folded > 0)) cite.wireFolded = true
    out.push(cite)
  }
  return rollupAlignedCitations(body, out)
}

// ---------------------------------------------------------------------------
// region_rollup.v1 — the historical citation-order re-map, read side
// ---------------------------------------------------------------------------

/** `data.rollup` — where a rollup row keeps its member roster. */
const ROLLUP_PAYLOAD_KEY = 'rollup'
/** The schema token that identifies a rollup payload (`provenance.kinds`). */
const ROLLUP_PAYLOAD_SCHEMA = 'region_rollup.v1'
/** `members[].lead_source` for a member whose lead block WAS carried — the
 *  token that selects the rendered member sections, in order. */
const ROLLUP_LEAD_CARRIED = 'carried'

/** The rollup payload on either envelope level, or null. Mirrors the server's
 *  `is_deterministic_rollup`, which accepts `data.data.rollup` and `data.rollup`
 *  because its callers sit on both sides of that boundary. */
function rollupPayload(
  body: Record<string, unknown> | null | undefined,
): Record<string, unknown> | null {
  if (!body || typeof body !== 'object') return null
  const inner = body['data']
  for (const level of [inner, body]) {
    if (!level || typeof level !== 'object') continue
    const rollup = (level as Record<string, unknown>)[ROLLUP_PAYLOAD_KEY]
    if (
      rollup &&
      typeof rollup === 'object' &&
      (rollup as Record<string, unknown>)['schema'] === ROLLUP_PAYLOAD_SCHEMA
    ) {
      return rollup as Record<string, unknown>
    }
  }
  return null
}

/**
 * Re-map a `region_rollup.v1` row's citations onto the order its body RENDERS.
 *
 * THE HISTORY FIX, client half. Rows are append-only, so the rollup rows written
 * between 2026-09-05T23:45Z and the 2026-09-07 producer fix carry `citations[]`
 * in the SLICE order while their body numbers member sections in the ROSTER
 * order — on `region_americas` the Argentina section's own `[[ref:1]]` resolved
 * to the UNITED STATES' head. `export_api._rollup_aligned_citations` already
 * repairs that on the EXPORT path; this is the same permutation applied where
 * the workstation and the mobile surface actually read, so the two paths agree
 * and a pre-fix regional read stops being mislabelled in the app.
 *
 * A PURE FUNCTION OF THE ROW, which is the only reason it is allowed to exist.
 * The rendered order is not inferred and not re-derived from anything outside
 * the row: `rollup.members[]` is the exact walk the body renders, and each
 * carried member's `assembly_id` IS the citation's `ref_id`. So the re-map is a
 * permutation the row itself specifies. No fetch, no clock, no flag.
 *
 * TOTAL OR NOTHING. Not a rollup, counts disagree, two citations share a
 * `refId`, or a carried member has no matching citation ⇒ the list is returned
 * UNTOUCHED. A partial re-map would mix two orderings inside one ordinal space,
 * which is the defect itself.
 *
 * A no-op on every row written after the producer fix, because there the two
 * orders are already the same permutation — one code path, no flag, and it
 * stays correct as the old rows age out.
 */
export function rollupAlignedCitations(
  body: Record<string, unknown> | null | undefined,
  entries: Citation[],
): Citation[] {
  const rollup = rollupPayload(body)
  if (!rollup) return entries
  const members = rollup['members']
  if (!Array.isArray(members)) return entries
  const carried = members.filter(
    (m): m is Record<string, unknown> =>
      !!m &&
      typeof m === 'object' &&
      (m as Record<string, unknown>)['lead_source'] === ROLLUP_LEAD_CARRIED,
  )
  const byRef = new Map<string, Citation>()
  for (const entry of entries) {
    if (entry.refId && !byRef.has(entry.refId)) byRef.set(entry.refId, entry)
  }
  if (carried.length === 0 || carried.length !== entries.length) return entries
  if (byRef.size !== entries.length) return entries

  const out: Citation[] = []
  const used = new Set<string>()
  for (let i = 0; i < carried.length; i++) {
    const ref = str(carried[i]['assembly_id']) ?? ''
    const entry = byRef.get(ref)
    // Each stored citation must be consumed exactly ONCE. Anything else is not
    // a permutation of the stored list, and re-numbering a non-permutation
    // would invent a pairing rather than recover one.
    if (!entry || used.has(ref)) return entries
    used.add(ref)
    out.push({ ...entry, marker: `[[ref:${i + 1}]]` })
  }
  return out
}

/**
 * The short DISPLAY label for a citation chip — the clean bracketed ordinal
 * `[N]`. Both marker forms collapse to it so a chip reads identically on every
 * surface: a composition `[[ref:3]]` renders `[3]`, a unit `[8]` renders `[8]`.
 * This is DISPLAY ONLY — the underlying `marker` (and its tokenization +
 * `[[ref:N]]`→citation mapping + hover card) is untouched. A marker with no
 * extractable ordinal falls back to itself (never fabricated).
 */
export function citationLabel(marker: string): string {
  const m = /(\d+)/.exec(marker)
  return m ? `[${m[1]}]` : marker
}

/** True iff this citation names a DESK GROUNDING block rather than a slice
 *  row or a sub-claim. Reads the producer's `markerClass` when the row carries
 *  it, else the registered kind vocabulary (pre-stamp rows). */
export function isGroundingCitation(c: Citation): boolean {
  return c.markerClass === MARKER_CLASS_GROUNDING || GROUNDING_KINDS.has(c.refKind)
}

/**
 * The honest kind label for a chip / hover card / evidence row. Every kind
 * gets one, so no citation is ever rendered as "Unresolved" when the record
 * plainly says what it is.
 */
export function citationKindLabel(c: Citation): string {
  if (c.refKind === 'finding') return 'sub-claim'
  if (c.refKind === 'signal') return 'signal'
  return GROUNDING_LABEL[c.refKind as GroundingKind] ?? c.refKind
}

/** Read the producer's `observation` block off a citation entry, camel-cased.
 *  Every field is optional and absent stays absent — a missing period renders
 *  as nothing, never as a guessed year. */
function readObservation(raw: object): CitationObservation {
  const o = raw as Record<string, unknown>
  const out: CitationObservation = {}
  const map: Array<[keyof CitationObservation, string]> = [
    ['collectionId', 'collection_id'],
    ['seriesId', 'series_id'],
    ['provider', 'provider'],
    ['indicatorName', 'indicator_name'],
    ['subject', 'subject'],
    ['value', 'value'],
    ['unit', 'unit'],
    ['validFrom', 'valid_from'],
    ['validTo', 'valid_to'],
    ['recordTime', 'record_time'],
    ['sourceUrl', 'source_url'],
    ['sha256', 'sha256'],
    ['licenceClass', 'licence_class'],
    ['staleTense', 'stale_tense'],
  ]
  for (const [key, wire] of map) {
    const value = str(o[wire])
    if (value) out[key] = value
  }
  return out
}

/**
 * Where a chip click should drill, or `null` when the citation has NO drill
 * target and a link would be a dead end.
 *
 * The `prior_read` block's `ref_id` is an `analyst_outputs` row — a FINDING.
 * Typing it as a signal (which the pre-2026-08-30 fallback did, because the
 * `ref_kind` was unrecognized) produced a chip labelled "signal" that drilled
 * to a signal id that has never existed. The other four grounding kinds are
 * synthetic and resolve against the finding's own citation record, so they
 * drill nowhere and are rendered as labeled non-links.
 */
export function citationDrill(c: Citation): { kind: 'finding' | 'signal'; id: string } | null {
  if (!c.refId) return null
  if (c.refKind === 'prior_read') return { kind: 'finding', id: c.refId }
  if (c.refKind === 'finding') return { kind: 'finding', id: c.refId }
  if (c.refKind === 'signal') return { kind: 'signal', id: c.refId }
  // 7g-2 — an `observation` has a real id and NO resolver: there is no
  // observations panel and none is being added, so a drill here would be a
  // click that 404s. The chip resolves it the other way — the whole row is
  // carried on the citation and rendered in the card — which is the drill a
  // reader of a historical number actually wants, and the one that also works
  // in an exported document.
  return null
}

/**
 * The MASTHEAD beside a claim — who the cited record is, in a reader's words.
 *
 * Derived, in order, from what the row actually carries:
 *   1. `source_id` — the platform's canonical OUTLET ref ("source.bbc.world")
 *      on a unit citation. The `source.` prefix is plumbing; the rest is the
 *      outlet ("BBC world").
 *   2. `source` when it is NOT a URL — a composition citation's `source` is
 *      the cited DESK's analyst id ("escalation"), which is the honest answer
 *      to "who says this" one tier up.
 *   3. the host of `source` when it IS a URL, `www.` stripped.
 *
 * `null` when the citation names none of the three — the tag then renders
 * nothing rather than a fabricated masthead. No lookup table: a descriptor's
 * `identity.name` ("38 North — Korea Analysis") is NOT on the row, and
 * inventing one here is exactly the class of fabrication the reading kit
 * exists to refuse.
 */
export function citationMasthead(c: Citation): string | null {
  const outlet = c.sourceId?.trim()
  if (outlet) {
    const words = outlet
      .replace(/^source[._-]/i, '')
      .replace(/[._-]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim()
    if (words) return words.charAt(0).toUpperCase() + words.slice(1)
  }
  const src = c.source?.trim()
  if (!src) return null
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(src)) {
    try {
      const host = new URL(src).hostname.replace(/^www\./i, '')
      return host || null
    } catch {
      return null
    }
  }
  const words = src.replace(/[._-]+/g, ' ').replace(/\s+/g, ' ').trim()
  if (!words) return null
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/**
 * The cited record's DATE, `YYYY-MM-DD`, or `null`. Read off `produced_at`
 * verbatim — the ISO date part only, never reformatted through a locale and
 * never derived from anything else on the page. An unparseable or absent
 * stamp yields `null`, and the tag prints the masthead alone.
 */
export function citationDate(c: Citation): string | null {
  const raw = c.producedAt?.trim()
  if (!raw) return null
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(raw)
  return m ? m[1] : null
}

/** Marker → Citation lookup (last write wins on a duplicate marker). */
export function citationsByMarker(citations: Citation[]): Map<string, Citation> {
  const m = new Map<string, Citation>()
  for (const c of citations) m.set(c.marker, c)
  return m
}

/** Stable DOM anchor id for a citation's evidence row, so a chip can scroll to
 *  it. Namespaced to avoid collisions with other ids on the page. */
export function evidenceAnchorId(refId: string): string {
  return `evidence-${refId}`
}

/**
 * The evidence-row anchor for a citation, id-less kinds included. Four of the
 * five grounding blocks carry no `refId` at all, so keying the anchor on the
 * id alone collapsed them onto one `evidence-` element (and every chip
 * scrolled to whichever rendered first). The marker is unique within a
 * finding, so it is the honest fallback — no id is invented.
 */
export function citationAnchorId(c: Citation): string {
  return c.refId ? evidenceAnchorId(c.refId) : `evidence-marker-${c.marker}`
}

/** One token of cited prose: a run of plain text, or a marker that resolves to
 *  a known citation. An unmatched marker (`[N]` or `[[ref:N]]` with no citation
 *  entry) stays a `text` token — we never invent an anchor for it. */
export type ProseToken =
  | { kind: 'text'; text: string }
  | { kind: 'marker'; marker: string; citation: Citation }

/** A prose token that ALSO distinguishes an UNRESOLVED marker (a `[N]` /
 *  `[[ref:N]]` in the prose with no backing citation) from plain text — so a
 *  surface can render it as an explicit "unresolved" chip rather than silently
 *  leaving it as literal `[N]` text (the S7-T3 honesty contract: a dangling
 *  marker is shown AS dangling, never fabricated into an anchor and never hidden). */
export type ProseTokenEx =
  | { kind: 'text'; text: string }
  | { kind: 'marker'; marker: string; citation: Citation }
  | { kind: 'unresolved'; marker: string }

// Composition ordinal marker (`[[ref:N]]`) FIRST, then the unit marker (`[N]`).
// The two are provably disjoint: in `[[ref:5]]` the digit is preceded by `:`,
// so `\[\d+\]` matches nothing inside it, and `\[\[ref:` never matches `[5]`.
const MARKER_RE = /\[\[ref:\d+\]\]|\[\d+\]/g

// Full-width / variant brackets that a core-plane model (gpt-oss / Qwen)
// non-deterministically wraps a citation ordinal in — 【N】 or ［N］ instead of
// ASCII [N] (mirrors the backend `inline_target._normalize_citation_markers`).
const VARIANT_MARKER_RE = /[【［]\s*(ref:\s*)?(\d+)\s*[】］]/g

/**
 * Normalize variant (full-width) citation brackets to ASCII so `MARKER_RE`
 * (ASCII-only) can match them. Only a bracket pair that WRAPS an ordinal
 * (optionally `ref:N`) is rewritten — prose that merely contains a stray
 * full-width bracket is left untouched, and an unresolved ASCII marker still
 * stays literal downstream (the honesty contract: never fabricate an anchor).
 */
export function normalizeCitationMarkers(text: string): string {
  if (!text || typeof text !== 'string') return text
  return text.replace(VARIANT_MARKER_RE, (_m, ref, n) => (ref ? `[ref:${n}]` : `[${n}]`))
}

/**
 * Split a prose string into text + marker tokens. Only markers that resolve in
 * `byMarker` become `marker` tokens (clickable chips); an unknown marker of
 * either form is left as literal text so the card never fabricates an evidence
 * link.
 */
export function splitProse(text: string, byMarker: Map<string, Citation>): ProseToken[] {
  const tokens: ProseToken[] = []
  let last = 0
  for (const match of text.matchAll(MARKER_RE)) {
    const marker = match[0]
    const start = match.index ?? 0
    const citation = byMarker.get(marker)
    if (!citation) continue // leave unknown markers embedded in the text run
    if (start > last) tokens.push({ kind: 'text', text: text.slice(last, start) })
    tokens.push({ kind: 'marker', marker, citation })
    last = start + marker.length
  }
  if (last < text.length) tokens.push({ kind: 'text', text: text.slice(last) })
  return tokens
}

/**
 * Like {@link splitProse}, but a marker with NO backing citation becomes an
 * `unresolved` token instead of being folded back into the text run. This is the
 * tokenizer the reading kit uses so a dangling `[N]` / `[[ref:N]]` can be shown
 * as an explicit muted "unresolved" chip — visible, honest, never a fabricated
 * anchor and never literal `[N]` noise.
 */
export function tokenizeProse(text: string, byMarker: Map<string, Citation>): ProseTokenEx[] {
  const tokens: ProseTokenEx[] = []
  let last = 0
  for (const match of text.matchAll(MARKER_RE)) {
    const marker = match[0]
    const start = match.index ?? 0
    if (start > last) tokens.push({ kind: 'text', text: text.slice(last, start) })
    const citation = byMarker.get(marker)
    if (citation) tokens.push({ kind: 'marker', marker, citation })
    else tokens.push({ kind: 'unresolved', marker })
    last = start + marker.length
  }
  if (last < text.length) tokens.push({ kind: 'text', text: text.slice(last) })
  return tokens
}
