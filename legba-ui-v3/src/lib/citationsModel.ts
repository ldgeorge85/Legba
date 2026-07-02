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

/** The record kind a citation drills into. */
export type CitationRefKind = 'finding' | 'signal'

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
    const refKind: CitationRefKind = str(o['ref_kind']) === 'finding' ? 'finding' : 'signal'
    if (!marker || !refId) continue
    out.push({
      marker,
      refId,
      refKind,
      signalId: refId, // deprecated compat alias (see Citation.signalId)
      title: str(o['title']),
      source: str(o['source']),
    })
  }
  return out
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

/** One token of cited prose: a run of plain text, or a marker that resolves to
 *  a known citation. An unmatched marker (`[N]` or `[[ref:N]]` with no citation
 *  entry) stays a `text` token — we never invent an anchor for it. */
export type ProseToken =
  | { kind: 'text'; text: string }
  | { kind: 'marker'; marker: string; citation: Citation }

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
