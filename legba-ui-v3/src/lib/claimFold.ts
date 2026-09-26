/**
 * claimFold — WHY a quoted claim is weaker than it looks (Program 7, 7b-i).
 *
 * A composed read renders its sentences with `[[ref:N]]` ordinals. The record
 * already knows, per ordinal, what is actually behind the claim — how many
 * outlets survived the wire fold, whether the verify judge flagged it, whether
 * the cited head recorded any basis at all — and until this module none of it
 * reached the sentence. The reader had to leave the claim to find out.
 *
 * This is the derivation half: pure, DOM-free, and a READER of the row only.
 * Nothing here recomputes independence, re-runs a judge, or infers a fold from
 * counts — every chip quotes a key the producer already stamped:
 *
 *   * `single-source`        ← the citation's `single_source: true` (7d,
 *     `source_independence.independence_of`), or the rendered record's
 *     corroboration block saying the same thing about the same block.
 *   * `wire-folded`          ← the corroboration block's `wire_folded` COUNT,
 *     falling back to the citation's own boolean when the record's block is
 *     not in hand (a country composition carries no assembly payload).
 *   * `unsupported`          ← the critique's per-claim verdict for THIS
 *     ordinal (`lib/claimVerdicts`), i.e. a judge verdict that lowered the
 *     read's confidence. Advisory flags (hedge laundering, double counting)
 *     chip as `flagged`, contradiction as `contradicted` — the chip never
 *     understates the verdict it is quoting.
 *   * `contested by retrieval` ← a `claim_contentions` row (7a): the contrary-
 *     evidence pass formulated the claim's COUNTER-query, ran it on the free
 *     rung, and FETCHED a page that states the opposite (or narrows it). The
 *     chip quotes that retrieval and never adjudicates it — "a page we hold
 *     says the opposite" is not "the claim is false", and the drill puts the
 *     page one click away so a reader can decide for themselves.
 *   * `insufficient evidence` ← the cited head recorded an EMPTY basis
 *     (`derived_from: []`). The platform's own equivalence: a banded read
 *     "always carries ``basis=[]``" when the band is `insufficient-evidence`
 *     (`deterministic_handlers/scorecard_banding.py`; `lib/evalOps.ts` states
 *     the same iff). An ABSENT `derived_from` says nothing about basis and
 *     therefore chips nothing.
 *
 * HONESTY CONTRACT, same as the rest of the reading kit: absence is absence.
 * A citation with none of these keys yields NO chips and the claim renders
 * exactly as it did before this module existed. A fold key is read as "true
 * when present" and never defaulted to false, because a composition origin
 * cites findings rather than signals and its independence is UNKNOWN — not
 * cleared.
 */
import type { Citation } from './citationsModel'
import type { ClaimVerdict } from './claimVerdicts'
import { contentionReason, type ContentionRow } from './contentionsModel'

/** The five fold reasons a chip can state. */
export type ClaimFoldKind =
  | 'single-source'
  | 'wire-folded'
  | 'unsupported'
  | 'insufficient-evidence'
  | 'contested-by-retrieval'

/** How loudly a chip reads. Maps to the panel's accent tokens at render. */
export type ClaimFoldTone = 'critical' | 'warning' | 'muted'

/** One rendered fold reason. `reason` is the tooltip: ONE sentence, from the
 *  data, never a template that outruns what the row said. */
export interface ClaimFoldChip {
  kind: ClaimFoldKind
  /** The chip's face — short, lower-case, the vocabulary the record uses. */
  label: string
  reason: string
  tone: ClaimFoldTone
  /**
   * The record this chip DRILLS to, when it has one — the k5b/absence idiom:
   * a `SelectionKind` and an id the Inspector's resolver can fetch. Absent on
   * every chip whose evidence is already on this row (a wire fold, an empty
   * basis), because a drill that re-showed the same row would be a dead end.
   */
  drill?: { kind: 'contention'; id: string; label: string }
}

/**
 * `blocks[].corroboration` — what the RENDERED record says about the sourcing
 * behind one ordinal (`source_independence.corroboration_block`). `n_sources`
 * keeps counting OUTLETS; the fold is stated beside it rather than subtracted
 * from it in silence, so `wireFolded` is an EXPLANATION of a number that did
 * not move.
 */
export interface CitationCorroboration {
  nSources: number | null
  /** Outlets left after the fold — written only when something folded. */
  nIndependentSources: number | null
  /** How many outlets the fold absorbed — written only when non-zero. */
  wireFolded: number | null
  singleSource: boolean
}

function obj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function intOrNull(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? Math.trunc(v) : null
}

/**
 * The record's corroboration blocks, keyed by the ordinal their `[[ref:N]]`
 * marker uses — `|blocks| == |citations| == |distinct markers|` holds by
 * construction on an assembly row (`assembly_render.render_assembly_body`), so
 * block ordinal N IS citation `[[ref:N]]`.
 *
 * Reads the merged finding body the Inspector already holds, at the same two
 * envelope levels {@link import('./citationsModel').extractCitations} reads
 * (`body.data.assembly` first, then a top-level `body.assembly`). A deliberate
 * ~20-line local read rather than a call into `lib/assemblyModel`: that module
 * projects a `/findings` ROW (not a merged body), imports this file's sibling
 * `citationsModel`, and drops the fold keys in its own projection — so reusing
 * it here would mean a cycle, a shape adapter and a widened projection for two
 * integers. Anything that is not a well-formed block list yields an EMPTY map,
 * and every chip then falls back to the citation's own stamps.
 */
export function corroborationByOrdinal(
  body: Record<string, unknown> | null | undefined,
): Map<number, CitationCorroboration> {
  const out = new Map<number, CitationCorroboration>()
  const root = obj(body)
  if (!root) return out
  for (const level of [obj(root['data']), root]) {
    if (!level) continue
    const assembly = obj(level['assembly'])
    const blocks = assembly ? assembly['blocks'] : undefined
    if (!Array.isArray(blocks)) continue
    for (const raw of blocks) {
      const block = obj(raw)
      if (!block) continue
      const ordinal = intOrNull(block['ordinal'])
      const corr = obj(block['corroboration'])
      if (ordinal === null || !corr || out.has(ordinal)) continue
      out.set(ordinal, {
        nSources: intOrNull(corr['n_sources']),
        nIndependentSources: intOrNull(corr['n_independent_sources']),
        wireFolded: intOrNull(corr['wire_folded']),
        singleSource: corr['single_source'] === true,
      })
    }
    if (out.size > 0) return out
  }
  return out
}

/** The verdict kinds that LOWERED this read's confidence — the ones worth a
 *  chip. `supported` / `not-flagged` / the two honest absences chip nothing:
 *  an absence of measurement is not a weakness of the claim. */
const VERDICT_CHIP_LABEL: Partial<Record<ClaimVerdict['kind'], string>> = {
  contradicted: 'contradicted',
  unsupported: 'unsupported',
  flagged: 'flagged',
}

export interface ClaimFoldInput {
  /** The per-claim verdict for this citation's ordinal, when the critique is
   *  loaded beside the read. Absent/null → no verdict chip, never a guess. */
  verdict?: ClaimVerdict | null
  /**
   * 7a — the contrary-evidence record for this ordinal, when the pass has
   * contended it and the retrieval settled something. Absent/null → no chip:
   * `none_found` is the pass's expected common answer and it is not a
   * weakness of the claim, so it chips nothing at all.
   */
  contention?: ContentionRow | null
  /** The rendered record's corroboration block for this ordinal, when the row
   *  carries one. Absent → the citation's own stamps are the whole story. */
  corroboration?: CitationCorroboration | null
}

/**
 * The fold chips for ONE citation, worst first: a recorded judge failure leads,
 * then an empty basis, then the sourcing folds. Returns `[]` for a citation
 * that states none of them — the no-fold read renders exactly as it did
 * before.
 */
export function claimFoldChips(c: Citation, input: ClaimFoldInput = {}): ClaimFoldChip[] {
  const chips: ClaimFoldChip[] = []
  const corr = input.corroboration ?? null

  // 7a — LEADS, because it is the only chip whose evidence is OUTSIDE this
  // tower. Every other fold reason describes how the platform assembled the
  // claim; this one says a page on the open web, which the platform fetched and
  // holds, states the opposite. An expired record still chips — it is read
  // "expired, not re-checked" in the tooltip — because hiding it would make a
  // pass that stopped running look like a week with nothing to contend.
  const contention = input.contention ?? null
  if (contention && (contention.stance === 'contradicts' || contention.stance === 'qualifies')) {
    const contradicts = contention.stance === 'contradicts'
    chips.push({
      kind: 'contested-by-retrieval',
      label: contradicts ? 'contested by retrieval' : 'narrowed by retrieval',
      reason: contentionReason(contention),
      // A qualification narrows a claim; it does not pull against it. Reading
      // the two at one volume would overstate every qualification the pass
      // finds, and those are its commonest real result.
      tone: contradicts && contention.live ? 'critical' : 'warning',
      drill: {
        kind: 'contention',
        id: contention.claim_id,
        label: contradicts ? 'contested by retrieval' : 'narrowed by retrieval',
      },
    })
  }

  const verdict = input.verdict ?? null
  const verdictLabel = verdict ? VERDICT_CHIP_LABEL[verdict.kind] : undefined
  if (verdict && verdictLabel) {
    chips.push({
      kind: 'unsupported',
      label: verdictLabel,
      reason: `verdict: ${verdict.label}, confidence lowered`,
      tone: verdict.kind === 'contradicted' ? 'critical' : 'warning',
    })
  }

  if (c.emptyBasis) {
    chips.push({
      kind: 'insufficient-evidence',
      label: 'insufficient evidence',
      reason: 'the cited read records no basis behind this claim',
      tone: 'warning',
    })
  }

  if (c.singleSource || corr?.singleSource) {
    chips.push({
      kind: 'single-source',
      label: 'single-source',
      reason: 'cited signals fold to one outlet',
      tone: 'warning',
    })
  }

  // The COUNT when the record's block is in hand, the citation's boolean
  // otherwise — the same statement at two resolutions, never a fabricated
  // number standing in for the boolean.
  const foldedCount = corr && corr.wireFolded && corr.wireFolded > 0 ? corr.wireFolded : null
  if (foldedCount !== null || c.wireFolded) {
    chips.push({
      kind: 'wire-folded',
      label: foldedCount !== null ? `${foldedCount} wire-folded` : 'wire-folded',
      reason:
        foldedCount !== null
          ? `${foldedCount} wire cop${foldedCount === 1 ? 'y' : 'ies'} folded into one outlet`
          : 'wire copies folded into one outlet',
      tone: 'muted',
    })
  }

  return chips
}
