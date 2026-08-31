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
 * Pure, DOM-free: the card component composes these helpers.
 */

/**
 * The five DESK GROUNDING block kinds (backend
 * `provenance.kinds.GROUNDING_REF_KINDS`). A grounding citation's ordinal
 * indexes a block the desk was SHOWN — its own prior read, its trailing
 * window ledger, the open-situation register, the desk baseline, the standing
 * open questions — not a slice row. Four of the five are synthetic and carry
 * NO `ref_id` by design (minting one so a drill link resolves would be a
 * fabricated anchor); only `prior_read` has a real `analyst_outputs` id, and
 * that id is a FINDING, never a signal.
 */
export type GroundingKind =
  | 'prior_read'
  | 'window_ledger'
  | 'situation_register'
  | 'desk_baseline'
  | 'open_questions'

const GROUNDING_KINDS: ReadonlySet<string> = new Set<GroundingKind>([
  'prior_read',
  'window_ledger',
  'situation_register',
  'desk_baseline',
  'open_questions',
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
}

/** Fallback titles for a grounding block whose stored entry carries none. */
const GROUNDING_TITLE: Record<GroundingKind, string> = {
  prior_read: "Prior read (this unit's previous verified read)",
  window_ledger: "Window ledger (this unit's trailing 14-day record)",
  situation_register: 'Open-situation register',
  desk_baseline: 'Desk baseline',
  open_questions: 'Standing open questions',
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
      const groundingId = kind === 'prior_read' ? (refId ?? '') : ''
      const cite: Citation = {
        marker,
        refId: groundingId,
        refKind: kind,
        signalId: '', // never a signal — the deprecated alias stays empty
        title: str(o['title']) ?? GROUNDING_TITLE[kind] ?? 'Desk grounding block',
        source: undefined,
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
    }
    out.push(cite)
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
  return null
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
