/**
 * framingModel — CROSS-FRAMING: one claim against the units that touch it,
 * where the layers diverge, and what no unit says (wave P lane B).
 *
 * ## The gap this closes
 *
 * A country desk is a roster of bounded units. Each answers ONE narrow question
 * over the same slice, and every reader surface we ship reads them SEPARATELY —
 * as composition inputs, as rows in a feed, as cells in the gap strip. Nothing
 * puts one claim against the units that touch it at once, so the reader cannot
 * see that three units rest on the same signal and frame it three different
 * ways, or that a fourth unit is not disagreeing but ABSENT.
 *
 * ## The join is RECORDED, never inferred
 *
 * The one thing this module must not do is invent a framing. So a unit only
 * appears as framing a claim when the RECORD says it touches the same evidence:
 *
 *   * `assembly.blocks[].signals[].signal_id` — the signals a block cites, and
 *   * `assembly.blocks[].signals[].also_cited_by[]` — the producer's own stamp
 *     naming the other desks citing that exact signal.
 *
 * A span's `markers` name which of its block's signals THIS claim rests on
 * (`AssemblySpan.markers` are the desk's own `[N]`), so "the units that touch
 * this claim" is the set of blocks citing at least one of those signal ids —
 * a shared-evidence link written by the producer, checkable by id, and never a
 * similarity score. The framing a unit then shows is its OWN sentence,
 * verbatim, with its own markers; nothing is paraphrased across units.
 *
 * A unit with no block in the record is ABSENT, and absence is typed: the desk's
 * `/v3/absence` answer is matched on the unit's subject and rendered in the
 * ROUTE's own words with its proof and its clock, so a stale absence reads as
 * "last known absence, not re-checked" rather than passing for a current one.
 * A unit with no matching absence item says exactly that — "no typed absence is
 * recorded" — which is a real answer and the one state the old surfaces
 * collapsed into a blank.
 *
 * ## What is derived here and what is not
 *
 * Derived: set membership, counts of lists the payload already holds, and which
 * of a closed set of states a row is in. NOT derived: any statistic, any
 * verdict, any framing text, any faithfulness number. `faithfulness` is the
 * block's own `verify.overall_score`; `judgeScore` on a lens is the entry's own
 * `verify_score`; the divergence pair rows travel verbatim out of
 * `/v3/layers/divergence`. Absence renders as absence — `null` here, never 0 —
 * and every figure the panel prints carries its unit and its as-of.
 *
 * DOM-free and fetch-free on purpose: the panel supplies the rows the routes
 * already serve, this decides what they mean, and `framingModel.test.ts` pins
 * every row state without a browser.
 */
import {
  ABSENCE_KIND_LABEL,
  absenceHeadline,
  type AbsenceItem,
  type AbsenceResponse,
} from '@/lib/absenceModel'
import type {
  AssemblyBlock,
  AssemblyPayload,
  AssemblySignal,
  AssemblySpan,
  AssemblyTension,
  CoverageRow,
  DropRow,
} from '@/lib/assemblyModel'
import type { Citation } from '@/lib/citationsModel'
import {
  claimFoldChips,
  type CitationCorroboration,
  type ClaimFoldChip,
} from '@/lib/claimFold'
import { claimVerdictForMarker, markerOrdinal, type ClaimVerdict } from '@/lib/claimVerdicts'
import { STANCE_BEARING, type ContentionRow } from '@/lib/contentionsModel'
import { GAP_STRIP_UNITS } from '@/lib/gapStripModel'
import { humanizeId } from '@/lib/deskNames'
import type {
  JournalEntry,
  LayerDivergenceDesk,
  LayerDivergenceFired,
  LayerDivergencePair,
  LayerDivergenceResponse,
} from '@/lib/api'
import { pairState, type PairState } from '@/lib/layerDivergence'
import type { Scope } from '@/state/scope'

// ---------------------------------------------------------------------------
// The six stance-typed lenses ("leans"), with the prior each one declares.
// ---------------------------------------------------------------------------

/**
 * The leans and their declared priors, mirrored from the persona modules'
 * own `--- DECLARED PRIOR (…) ---` headers (`src/legba/prompts/lens_<id>/`)
 * and named in docs/GLOSSARY.md § "leans (stance-typed lenses)".
 *
 * MIRRORED, NOT DERIVED, and that is a deliberate cost: the prior lives in the
 * persona module, the descriptor's content hash IS its version, and neither
 * travels on the journal row the panel reads. So the roster is pinned here in
 * ONE place with its source named, `framingModel.test.ts` holds it to six
 * members, and a lean whose id is not on this list still renders — under its
 * own id, with its prior stated as unnamed — rather than vanishing from a
 * surface whose whole job is to show who spoke.
 */
export const LEAN_LENSES: readonly { id: string; name: string; prior: string }[] = [
  { id: 'lens_left', name: 'Structuralist', prior: 'who pays, who gains' },
  { id: 'lens_right', name: 'Sovereigntist', prior: 'order, deterrence, enforcement' },
  { id: 'lens_centre', name: 'Institutionalist', prior: 'who must agree, under which rules' },
  { id: 'lens_pragmatist', name: 'Executor', prior: 'funded, staffed, shipped, enforced' },
  { id: 'lens_militarist', name: 'Strategist', prior: 'who holds the next rung' },
  { id: 'lens_isolationist', name: 'Retrencher', prior: 'stated aim vs measured effect' },
]

const LEAN_BY_ID = new Map(LEAN_LENSES.map((l) => [l.id, l]))

/** The unit label roster the gap strip already publishes, by unit id. */
const UNIT_LABEL = new Map(GAP_STRIP_UNITS.map((u) => [u.id, u.label]))

/** A unit's display label — the record's own name first, never an invention. */
export function unitLabel(unit: string, coverage?: CoverageRow | null): string {
  return coverage?.unit_name || UNIT_LABEL.get(unit) || humanizeId(unit)
}

// ---------------------------------------------------------------------------
// The claim
// ---------------------------------------------------------------------------

/** Which span of which block the panel is reading. */
export interface FramingClaimRef {
  ordinal: number
  spanIndex: number
}

/** What the header states about the claim under examination. */
export interface FramingClaim {
  ref: FramingClaimRef
  /** The sentence VERBATIM, as the record carries it. */
  text: string
  role: string
  /** The unit whose read the claim is quoted from. */
  unit: string
  unitLabel: string
  /** The origin desk head the span was quoted out of (`span.origin.head_id`). */
  headId: string
  /** The record (composition finding) the claim is published in. */
  recordId: string
  /** The desk's own `[N]` markers inside the span. */
  markers: string[]
  /** The signal ids this claim's markers cite — the whole cross-unit join. */
  signalIds: string[]
  /** The claim's own as-of: the record's `as_of`. */
  asOf: string
  /** The cited head's own instant, when the block carries one. */
  headProducedAt: string | null
  /** The contrary-evidence record against THIS claim, when one exists. */
  contention: ContentionRow | null
  /** How the claim was chosen, for the header to state plainly. */
  chosenBy: 'requested' | 'most-contested' | 'first-block'
}

/** The non-context spans of a block, in payload order. */
function quotedSpans(block: AssemblyBlock): AssemblySpan[] {
  return block.spans.filter((s) => s.role !== 'context_body')
}

/** The block carrying an ordinal. */
function blockAt(a: AssemblyPayload, ordinal: number): AssemblyBlock | null {
  return a.blocks.find((b) => b.ordinal === ordinal) ?? null
}

/** The signals a span's markers name, out of its own block's signal list. */
export function spanSignals(block: AssemblyBlock, span: AssemblySpan): AssemblySignal[] {
  const wanted = new Set(span.markers)
  return block.signals.filter((s) => wanted.has(s.marker))
}

/**
 * Normalise a sentence for a text match: markers stripped, whitespace folded,
 * case dropped. Used ONLY to find which span a caller's claim text refers to —
 * never to decide that two units say the same thing.
 */
function normaliseClaimText(text: string): string {
  return text
    .replace(/\[\[ref:[^\]]+\]\]/g, ' ')
    .replace(/\[\d+\]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase()
}

/** The contention rows that settled something, newest first. */
export function bearingContentions(rows: readonly ContentionRow[]): ContentionRow[] {
  return rows
    .filter((r) => STANCE_BEARING.has(r.stance))
    .slice()
    .sort((a, b) => {
      // `contradicts` outranks `qualifies` — the pass's own distinction, and
      // the only ordering this module applies to a row it did not compute.
      const rank = (r: ContentionRow) => (r.stance === 'contradicts' ? 0 : 1)
      return rank(a) - rank(b) || Date.parse(b.as_of) - Date.parse(a.as_of)
    })
}

export interface PickClaimOptions {
  /** A `claim_contentions.claim_id` the caller parked (a claim chip's drill). */
  claimId?: string | null
  /** A claim sentence the caller parked (a situation row, a claims list). */
  claimText?: string | null
  contentions?: readonly ContentionRow[]
}

/**
 * Choose the claim the panel reads, in the order the research note names:
 * the one the caller asked for, else the desk's MOST CONTESTED claim, else the
 * record's first quoted span. Returns `null` for a record with no quoted span —
 * which the panel states as "this record carries no quoted claim", never as an
 * empty table.
 */
export function pickClaim(
  a: AssemblyPayload | null | undefined,
  opts: PickClaimOptions = {},
): { ref: FramingClaimRef; chosenBy: FramingClaim['chosenBy'] } | null {
  if (!a || a.blocks.length === 0) return null

  const locate = (
    ordinal: number | null | undefined,
    match?: (s: AssemblySpan) => boolean,
  ): FramingClaimRef | null => {
    if (typeof ordinal !== 'number') return null
    const block = blockAt(a, ordinal)
    if (!block) return null
    const spans = quotedSpans(block)
    if (spans.length === 0) return null
    const hit = match ? spans.find(match) : undefined
    const span = hit ?? spans[0]
    return { ordinal, spanIndex: block.spans.indexOf(span) }
  }

  const bearing = bearingContentions(opts.contentions ?? [])

  if (opts.claimId) {
    const row = (opts.contentions ?? []).find((r) => r.claim_id === opts.claimId)
    if (row) {
      const ref = locate(row.block_ordinal, (s) => s.role === row.span_role)
      if (ref) return { ref, chosenBy: 'requested' }
    }
  }

  if (opts.claimText) {
    const wanted = normaliseClaimText(opts.claimText)
    if (wanted) {
      for (const block of a.blocks) {
        for (const span of quotedSpans(block)) {
          const got = normaliseClaimText(span.text)
          if (got === wanted || got.includes(wanted) || wanted.includes(got)) {
            return {
              ref: { ordinal: block.ordinal, spanIndex: block.spans.indexOf(span) },
              chosenBy: 'requested',
            }
          }
        }
      }
    }
  }

  for (const row of bearing) {
    const ref = locate(row.block_ordinal, (s) => s.role === row.span_role)
    if (ref) return { ref, chosenBy: 'most-contested' }
  }

  for (const block of a.blocks) {
    const spans = quotedSpans(block)
    if (spans.length > 0) {
      return {
        ref: { ordinal: block.ordinal, spanIndex: block.spans.indexOf(spans[0]) },
        chosenBy: 'first-block',
      }
    }
  }
  return null
}

/** Resolve a chosen ref into the full claim the header states. */
export function resolveClaim(
  a: AssemblyPayload,
  recordId: string,
  chosen: { ref: FramingClaimRef; chosenBy: FramingClaim['chosenBy'] },
  contentions: readonly ContentionRow[],
): FramingClaim | null {
  const block = blockAt(a, chosen.ref.ordinal)
  const span = block?.spans[chosen.ref.spanIndex]
  if (!block || !span) return null
  const signals = spanSignals(block, span)
  const contention =
    bearingContentions(contentions).find(
      (r) => r.block_ordinal === block.ordinal && (!r.span_role || r.span_role === span.role),
    ) ?? null
  return {
    ref: chosen.ref,
    text: span.text,
    role: span.role,
    unit: block.desk,
    unitLabel: unitLabel(block.desk, a.coverage.find((c) => c.unit === block.desk)),
    headId: span.origin.head_id,
    recordId,
    markers: span.markers,
    signalIds: signals.map((s) => s.signal_id).filter((id): id is string => !!id),
    asOf: a.as_of,
    headProducedAt: block.produced_at,
    contention,
    chosenBy: chosen.chosenBy,
  }
}

// ---------------------------------------------------------------------------
// The unit table
// ---------------------------------------------------------------------------

/**
 * The five states a unit row can be in. Every one of them is a statement; none
 * of them is a blank.
 *
 *  * `carries`  — the claim is quoted out of THIS unit's read.
 *  * `frames`   — this unit's read cites at least one of the claim's own cited
 *                 signals, so it rests on the same evidence and says its own
 *                 thing about it.
 *  * `silent`   — this unit has a read in the record and cites NONE of the
 *                 claim's evidence: absent on this matter, not contradicting it.
 *  * `absent`   — no read in the record, and a typed absence explains it in the
 *                 route's own words (`stale` says so in {@link FramingUnitRow}).
 *  * `unrecorded` — no read in the record and NO typed absence names the unit.
 */
export type UnitRowState = 'carries' | 'frames' | 'silent' | 'absent' | 'unrecorded'

/**
 * The record's OWN stamps about what each citation rests on, keyed the way the
 * reading kit already keys them. Both halves are optional: a record that
 * carries neither renders no fold chips at all, which is the honest reading of
 * a record that stated nothing — never a record that stated "clear".
 */
export interface FramingEvidence {
  /** The composition's citations (`lib/citationsModel.extractCitations`). */
  citations?: readonly Citation[]
  /** Per-ordinal corroboration (`lib/claimFold.corroborationByOrdinal`). */
  corroboration?: ReadonlyMap<number, CitationCorroboration>
}

/** `{ordinal: citation}` — block N IS citation `[[ref:N]]` on an assembly row. */
export function citationsByOrdinal(
  citations: readonly Citation[],
): Map<number, Citation> {
  const out = new Map<number, Citation>()
  for (const c of citations) {
    const ordinal = markerOrdinal(c.marker)
    if (ordinal === null || out.has(ordinal)) continue
    out.set(ordinal, c)
  }
  return out
}

export interface FramingUnitRow {
  unit: string
  label: string
  state: UnitRowState
  /** The unit's OWN sentence, verbatim. `null` unless it has a read. */
  framing: string | null
  /** The unit's own markers inside that sentence. */
  markers: string[]
  /** The unit's read (the origin desk head), for a drill. */
  findingId: string | null
  /** The unit's block ordinal in the record, when it has one. */
  ordinal: number | null
  producedAt: string | null
  /** Signals cited by BOTH this unit and the claim — the join, by id. */
  sharedSignalIds: string[]
  /** How many signals this unit's block cites in total. */
  citedCount: number | null
  /** Outlets behind the block, when the record states one. `null` is absence,
   *  never zero. */
  nSources: number | null
  /** How many OTHER desks share this block's evidence, per the record. */
  nDesksSharing: number | null
  /**
   * The fold reasons the record states about this unit's citation, through the
   * reading kit's own derivation (`lib/claimFold.claimFoldChips`) rather than a
   * second vocabulary invented here — so "single-source", "wire-folded" and
   * "contested by retrieval" mean on this surface exactly what they mean beside
   * a claim in the Inspector. A record that stamped no fold key yields none.
   */
  foldChips: ClaimFoldChip[]
  /** The block's own `verify.overall_score` — never recomputed, `null` when
   *  the record carries none. */
  faithfulness: number | null
  /** The coverage roster's own status word for this unit. */
  coverageStatus: string | null
  coverageAgeH: number | null
  /** A contrary-evidence record written against THIS unit's claim. */
  contested: ContentionRow | null
  /** The typed absence explaining a missing read, in the route's own words. */
  absence: AbsenceItem | null
  /** `true` only when the absence's own clock says it expired unchecked. */
  absenceStale: boolean
  /** The one line a row with no read renders — the route's words, never ours. */
  absenceLine: string | null
  /** Declared tensions anchored between this unit's block and the claim's. */
  tensions: AssemblyTension[]
}

function blockShare(block: AssemblyBlock, claimSignalIds: ReadonlySet<string>): string[] {
  const out: string[] = []
  for (const s of block.signals) {
    if (s.signal_id && claimSignalIds.has(s.signal_id) && !out.includes(s.signal_id)) {
      out.push(s.signal_id)
    }
  }
  return out
}

/**
 * Units named by the producer as also citing one of the claim's signals but
 * carrying no block of their own on this record — `also_cited_by` is a stamp
 * about the WHOLE tower, so it can name a desk this record did not carry.
 */
function alsoCitedUnits(claimBlock: AssemblyBlock, span: AssemblySpan): Set<string> {
  const out = new Set<string>()
  for (const s of spanSignals(claimBlock, span)) {
    for (const other of s.also_cited_by) {
      if (other.desk) out.add(other.desk)
    }
  }
  return out
}

/** The typed absence naming a unit, under any kind. */
function absenceForUnit(resp: AbsenceResponse | null | undefined, unit: string): AbsenceItem | null {
  return resp?.absences.find((a) => a.subject === unit) ?? null
}

/** The record's tensions touching a pair of ordinals. */
function tensionsBetween(
  a: AssemblyPayload,
  left: number,
  right: number,
): AssemblyTension[] {
  if (left === right) return []
  return a.tensions.filter((t) => {
    const ords = [t.a?.ordinal, t.b?.ordinal]
    return ords.includes(left) && ords.includes(right)
  })
}

/**
 * One row per unit on the record's own coverage roster — the denominator the
 * composition itself declared, never a constant in this bundle.
 *
 * Rows sort by what the reader came for: the carrying unit, then the units that
 * frame it, then the units with a read that stays silent, then the absences.
 */
export function deriveUnitRows(
  a: AssemblyPayload,
  claim: FramingClaim,
  absence: AbsenceResponse | null | undefined,
  contentions: readonly ContentionRow[],
  evidence: FramingEvidence = {},
): FramingUnitRow[] {
  const claimBlock = blockAt(a, claim.ref.ordinal)
  const claimSpan = claimBlock?.spans[claim.ref.spanIndex] ?? null
  const claimSignalIds = new Set(claim.signalIds)
  const alsoCited = claimBlock && claimSpan ? alsoCitedUnits(claimBlock, claimSpan) : new Set<string>()
  const byUnit = new Map(a.blocks.map((b) => [b.desk, b]))
  const bearing = bearingContentions(contentions)
  const citationAt = citationsByOrdinal(evidence.citations ?? [])
  const corrAt = evidence.corroboration ?? new Map()

  // The roster IS `coverage`: the producer writes one row per declared unit,
  // including the ones with no head. A record that carried a block for a unit
  // the roster somehow omits still gets a row, appended, rather than being
  // dropped from a table whose purpose is to be complete.
  const roster: string[] = a.coverage.map((c) => c.unit)
  for (const b of a.blocks) if (!roster.includes(b.desk)) roster.push(b.desk)

  const rows = roster.map<FramingUnitRow>((unit) => {
    const coverage = a.coverage.find((c) => c.unit === unit) ?? null
    const block = byUnit.get(unit) ?? null
    const label = unitLabel(unit, coverage)
    const item = absenceForUnit(absence, unit)

    if (!block) {
      const stale = item?.stale === true
      return {
        unit,
        label,
        // `also_cited_by` may name this unit, but the record carries no block
        // for it: the claim's evidence reached a read this record did not
        // publish, which is still an absence HERE and says so.
        state: item ? 'absent' : 'unrecorded',
        framing: null,
        markers: [],
        findingId: null,
        ordinal: null,
        producedAt: null,
        sharedSignalIds: [],
        citedCount: null,
        nSources: null,
        nDesksSharing: null,
        foldChips: [],
        faithfulness: null,
        coverageStatus: coverage?.status ?? null,
        coverageAgeH: coverage?.age_h ?? null,
        contested: null,
        absence: item,
        absenceStale: stale,
        absenceLine: item
          ? `${ABSENCE_KIND_LABEL[item.kind] ?? item.kind} — ${item.reason}`
          : null,
        tensions: [],
      }
    }

    const shared = blockShare(block, claimSignalIds)
    const isClaimBlock = block.ordinal === claim.ref.ordinal
    const lead = quotedSpans(block)[0] ?? null
    const corr = block.corroboration
    const contested =
      bearing.find(
        (r) => r.block_ordinal === block.ordinal || (r.desk_key === unit && r.target_id === block.target_id),
      ) ?? null

    return {
      unit,
      label,
      state: isClaimBlock
        ? 'carries'
        : shared.length > 0 || alsoCited.has(unit)
          ? 'frames'
          : 'silent',
      framing: lead ? lead.text : null,
      markers: lead ? lead.markers : [],
      findingId: block.finding_id,
      ordinal: block.ordinal,
      producedAt: block.produced_at,
      sharedSignalIds: shared,
      citedCount: block.signals.length,
      nSources: corr ? corr.n_sources : null,
      nDesksSharing: corr ? corr.n_desks_sharing : null,
      // The fold reasons are the record's own stamps, read through the reading
      // kit's derivation. A citation this record did not write yields none —
      // a missing fold key is UNKNOWN, never "cleared" (claimFold's contract).
      foldChips: (() => {
        const citation = citationAt.get(block.ordinal)
        if (!citation) return []
        return claimFoldChips(citation, {
          contention: contested,
          corroboration: corrAt.get(block.ordinal) ?? null,
        })
      })(),
      faithfulness: block.verify ? block.verify.overall_score : null,
      coverageStatus: coverage?.status ?? null,
      coverageAgeH: coverage?.age_h ?? null,
      contested,
      absence: null,
      absenceStale: false,
      absenceLine: null,
      tensions: tensionsBetween(a, claim.ref.ordinal, block.ordinal),
    }
  })

  const ORDER: Record<UnitRowState, number> = {
    carries: 0,
    frames: 1,
    silent: 2,
    absent: 3,
    unrecorded: 4,
  }
  return rows.sort((x, y) => ORDER[x.state] - ORDER[y.state] || x.label.localeCompare(y.label))
}

/** The one line a row with no read renders, stale-aware — the route's words. */
export function absenceSentence(row: FramingUnitRow): string {
  if (!row.absence) {
    // The coverage status travels VERBATIM — it is the producer's own closed
    // word (`no_head_in_horizon`, `below_floor`, `unverified`), and prettifying
    // it into title case would make a vocabulary term read as a sentence this
    // surface wrote.
    return row.coverageStatus
      ? `no typed absence is recorded — the record's coverage says "${row.coverageStatus}"`
      : 'no typed absence is recorded'
  }
  return `${row.absenceLine} · ${absenceHeadline(row.absence)}`
}

// ---------------------------------------------------------------------------
// The lens row
// ---------------------------------------------------------------------------

export interface FramingLensRow {
  analystId: string
  /** The persona's name, when the id is one of the six leans. */
  name: string | null
  /** The declared prior, named. `null` for an id off the mirrored roster. */
  prior: string | null
  entryId: string | null
  producedAt: string | null
  /** The entry's own gate score. `null` when no critique exists — never 0. */
  judgeScore: number | null
  /** The lens's OWN sentence about the same evidence, verbatim. */
  framing: string | null
  /** Which ref of the claim's the lens cited — the join, stated. */
  citedRefId: string | null
  honestyFlags: string[]
  /** `true` when the lens read exists but names none of the claim's refs. */
  readButSilent: boolean
}

/**
 * The six leans' framings of the SAME evidence.
 *
 * A lens read reaches this table only when its own `claims[].refs[]` names one
 * of the claim's cited signals or the read the claim was quoted out of — the
 * same recorded-id join the unit table uses. A lens that ran and cited none of
 * them is `readButSilent`; a lens with no read at all renders with a null entry
 * id, which the panel states as "no lens read" rather than leaving a gap.
 */
export function deriveLensRows(
  entries: readonly JournalEntry[],
  claim: FramingClaim,
): FramingLensRow[] {
  const wanted = new Set<string>([...claim.signalIds, claim.headId, claim.recordId])
  const newestByAnalyst = new Map<string, JournalEntry>()
  for (const e of entries) {
    if (e.entry_kind !== 'lens') continue
    const id = e.analyst_id
    if (!id) continue
    const prev = newestByAnalyst.get(id)
    if (!prev || Date.parse(e.produced_at) > Date.parse(prev.produced_at)) {
      newestByAnalyst.set(id, e)
    }
  }

  // The six mirrored leans first, then any other lens analyst that actually
  // wrote an entry — so a seventh persona the roster has not learned about is
  // visible under its own id instead of silently missing.
  const ids = [
    ...LEAN_LENSES.map((l) => l.id),
    ...[...newestByAnalyst.keys()].filter((id) => !LEAN_BY_ID.has(id)).sort(),
  ]

  return ids.map<FramingLensRow>((analystId) => {
    const lean = LEAN_BY_ID.get(analystId) ?? null
    const entry = newestByAnalyst.get(analystId) ?? null
    if (!entry) {
      return {
        analystId,
        name: lean?.name ?? null,
        prior: lean?.prior ?? null,
        entryId: null,
        producedAt: null,
        judgeScore: null,
        framing: null,
        citedRefId: null,
        honestyFlags: [],
        readButSilent: false,
      }
    }
    let framing: string | null = null
    let citedRefId: string | null = null
    for (const c of entry.claims ?? []) {
      const hit = (c.refs ?? []).find((r) => r.id && wanted.has(r.id))
      if (hit) {
        framing = c.text_span
        citedRefId = hit.id
        break
      }
    }
    return {
      analystId,
      name: lean?.name ?? null,
      prior: lean?.prior ?? null,
      entryId: entry.id,
      producedAt: entry.produced_at,
      judgeScore: typeof entry.verify_score === 'number' ? entry.verify_score : null,
      framing,
      citedRefId,
      honestyFlags: entry.honesty_flags ?? [],
      readButSilent: framing === null,
    }
  })
}

// ---------------------------------------------------------------------------
// Where the layers diverge
// ---------------------------------------------------------------------------

/** One pair row with the state the divergence map's own classifier gave it. */
export interface FramingPairRow {
  pair: LayerDivergencePair
  state: PairState
  /** The pair's declared meaning, from the route's `pairs_declared`. */
  meaning: string | null
}

export interface FramingDivergence {
  /** The desk's receipt, when the run resolved this desk. */
  desk: LayerDivergenceDesk | null
  /** Every declared pair for the desk, verbatim, worst news first. */
  pairs: FramingPairRow[]
  /** The newest divergence this desk actually FIRED, with its finding. */
  fired: LayerDivergenceFired | null
  /** The run's own day. `null` when no run has happened. */
  asOf: string | null
  receiptRunId: string | null
  methodVersion: string | null
  /** SEAMS #60's un-audited-classification stamp, rendered on every read. */
  classificationAudit: string | null
  /** Why there is no receipt to show, in the route's own terms. */
  absent: 'no-response' | 'not-measured' | 'no-run' | 'desk-not-resolved' | null
}

/** Worst news first — the divergence map's own card ordering. */
const PAIR_STATE_ORDER: Record<PairState, number> = {
  fired: 0,
  thin: 1,
  below_threshold: 2,
  evaluable: 3,
  aperture_excluded: 4,
}

/**
 * The desk's layer-divergence receipt. Every distinguishable "nothing here" is
 * its own answer: the route was unreachable, the read itself failed
 * (`measured: false`), the unit has never written a receipt, or the run wrote
 * one and this desk was not in it. Folding those into one grey blank is the
 * failure the divergence map was built to end.
 */
export function deriveDivergence(
  res: LayerDivergenceResponse | null | undefined,
  targetId: string,
): FramingDivergence {
  const empty: Omit<FramingDivergence, 'absent'> = {
    desk: null,
    pairs: [],
    fired: null,
    asOf: res?.as_of ?? null,
    receiptRunId: res?.receipt_run_id ?? null,
    methodVersion: res?.method_version ?? null,
    classificationAudit: res?.classification_audit ?? null,
  }
  if (!res) return { ...empty, absent: 'no-response' }
  if (!res.measured) return { ...empty, absent: 'not-measured' }
  if (res.receipt_run_id == null) return { ...empty, absent: 'no-run' }
  const desk = res.desks.find((d) => d.target_id === targetId) ?? null
  if (!desk) return { ...empty, absent: 'desk-not-resolved' }
  const meaningOf = new Map(res.pairs_declared.map((p) => [p.pair_id, p.meaning]))
  const pairs = (desk.pairs ?? [])
    .map<FramingPairRow>((pair) => ({
      pair,
      state: pairState(pair, desk.fired, res.thin_min_per_day),
      meaning: meaningOf.get(pair.pair_id) ?? null,
    }))
    .sort((a, b) => PAIR_STATE_ORDER[a.state] - PAIR_STATE_ORDER[b.state])
  return { ...empty, desk, pairs, fired: desk.fired, absent: null }
}

// ---------------------------------------------------------------------------
// What no unit says
// ---------------------------------------------------------------------------

export interface FramingSilenceRow {
  kind: string
  kindLabel: string
  subject: string
  reason: string
  asOf: string | null
  stale: boolean
  proofRef: string | null
  proofRefKind: string | null
}

export interface FramingSilence {
  /** Units on the roster with no read at all — "named by 0 of M". */
  unnamedUnits: string[]
  /** The desk's typed absences whose subject is NOT a roster unit: the
   *  subjects nothing on this record speaks to. */
  offRosterAbsences: FramingSilenceRow[]
  /** Kinds the route could not read for this desk, in its own words. */
  notMeasured: string[]
  /** What the record considered and did not carry, by why-class. */
  drops: { why: string; rows: (DropRow | string)[] }[]
  /** The record's own cross-unit tension check — a checked negative, stated. */
  tensionsExamined: number | null
  tensionsFound: number | null
  tensionScopeNote: string | null
  /** `true` when there is nothing at all to say here. */
  empty: boolean
}

/**
 * WHAT NO UNIT SAYS — the reading a multi-unit surface is uniquely able to take
 * and every single-unit surface discards.
 *
 * Absence, not contradiction: a unit that never ran has not disagreed with the
 * claim, and a subject the desk declared absent is not a subject the desk
 * denied. Every row here quotes a record the platform wrote — a typed absence,
 * an unread absence kind, a drop the composition logged with its why-class, or
 * the tension pass's own checked negative — and nothing is inferred from the
 * shape of what is missing.
 */
export function deriveSilence(
  a: AssemblyPayload,
  rows: readonly FramingUnitRow[],
  absence: AbsenceResponse | null | undefined,
): FramingSilence {
  const roster = new Set(rows.map((r) => r.unit))
  const unnamedUnits = rows
    .filter((r) => r.state === 'absent' || r.state === 'unrecorded')
    .map((r) => r.unit)

  const offRosterAbsences = (absence?.absences ?? [])
    .filter((item) => !roster.has(item.subject))
    .map<FramingSilenceRow>((item) => ({
      kind: item.kind,
      kindLabel: ABSENCE_KIND_LABEL[item.kind] ?? item.kind,
      subject: item.subject,
      reason: item.reason,
      asOf: item.as_of,
      stale: item.stale === true,
      proofRef: item.proof?.ref ?? null,
      proofRefKind: item.proof?.ref_kind ?? null,
    }))

  const d = a.drops
  const drops = d
    ? (
        [
          ['no_head_in_horizon', d.no_head],
          ['below_floor', d.below_floor],
          ['not_selected', d.not_selected],
          ['cap_trimmed', d.trimmed],
          ['shown_not_carried', d.shown_not_carried],
        ] as const
      )
        .filter(([, list]) => Array.isArray(list) && list.length > 0)
        .map(([why, list]) => ({ why, rows: [...list] }))
    : []

  const checked = a.tension_checked
  return {
    unnamedUnits,
    offRosterAbsences,
    notMeasured: absence?.not_measured ?? [],
    drops,
    tensionsExamined: checked ? checked.pairs_examined : null,
    tensionsFound: checked ? checked.pairs_found : null,
    tensionScopeNote: checked ? checked.scope_note : null,
    empty:
      unnamedUnits.length === 0 &&
      offRosterAbsences.length === 0 &&
      (absence?.not_measured?.length ?? 0) === 0 &&
      drops.length === 0 &&
      checked === null,
  }
}

// ---------------------------------------------------------------------------
// The whole view
// ---------------------------------------------------------------------------

export interface FramingCounts {
  /** Units whose read rests on this claim's evidence (the carrier included). */
  carriedBy: number
  /** The record's own declared roster size. */
  roster: number
  /** Units with a read that cites none of the claim's evidence. */
  silent: number
  /** Units with no read at all on this record. */
  noRead: number
}

export interface CrossFramingView {
  targetId: string
  recordId: string
  claim: FramingClaim
  /**
   * The claim's own per-claim verify verdict (`lib/claimVerdicts`), which is
   * the header's "verified state". It is never fabricated: a record with no
   * verification block reads `not-recorded`, and a deterministic-floor-only
   * critique reads `not-checked` — both are honest absences, not failures.
   */
  verdict: ClaimVerdict
  counts: FramingCounts
  units: FramingUnitRow[]
  lenses: FramingLensRow[]
  divergence: FramingDivergence
  silence: FramingSilence
  /** The record's own schema + regime + tier, for the header's stamp line. */
  schema: string
  regime: string
  tier: string
}

export interface CrossFramingInput {
  targetId: string
  recordId: string
  assembly: AssemblyPayload | null | undefined
  contentions?: readonly ContentionRow[]
  absence?: AbsenceResponse | null
  divergence?: LayerDivergenceResponse | null
  lensEntries?: readonly JournalEntry[]
  /** The record's own citation + corroboration stamps. */
  evidence?: FramingEvidence
  /** The record's `verification` block, for the claim's own judge verdict. */
  verification?: Record<string, unknown> | null
  claimId?: string | null
  claimText?: string | null
}

/**
 * The whole panel's state, from the rows the routes already serve.
 *
 * Returns `null` only when there is no record to read or the record carries no
 * quoted span — both of which the panel states in words. Everything else,
 * including every kind of absence, resolves to a view with rows in it.
 */
export function deriveCrossFraming(input: CrossFramingInput): CrossFramingView | null {
  const a = input.assembly
  if (!a) return null
  const contentions = input.contentions ?? []
  const chosen = pickClaim(a, {
    claimId: input.claimId,
    claimText: input.claimText,
    contentions,
  })
  if (!chosen) return null
  const claim = resolveClaim(a, input.recordId, chosen, contentions)
  if (!claim) return null

  const units = deriveUnitRows(a, claim, input.absence, contentions, input.evidence)
  const counts: FramingCounts = {
    carriedBy: units.filter((r) => r.state === 'carries' || r.state === 'frames').length,
    roster: units.length,
    silent: units.filter((r) => r.state === 'silent').length,
    noRead: units.filter((r) => r.state === 'absent' || r.state === 'unrecorded').length,
  }

  return {
    targetId: input.targetId,
    recordId: input.recordId,
    claim,
    verdict: claimVerdictForMarker(input.verification, `[[ref:${claim.ref.ordinal}]]`),
    counts,
    units,
    lenses: deriveLensRows(input.lensEntries ?? [], claim),
    divergence: deriveDivergence(input.divergence, input.targetId),
    silence: deriveSilence(a, units, input.absence),
    schema: a.schema,
    regime: a.regime,
    tier: a.tier,
  }
}

/** The header's count sentence — every number says what it counts. */
export function countsSentence(c: FramingCounts): string {
  const parts = [`carried by ${c.carriedBy} of ${c.roster} units`]
  parts.push(`${c.noRead} with no read`)
  if (c.silent > 0) parts.push(`${c.silent} read this desk but not this claim`)
  return parts.join(' · ')
}

// ---------------------------------------------------------------------------
// Consult — the ambient scope pin
// ---------------------------------------------------------------------------

/** How far the claim text is trimmed for the pin label and the tab title. */
export const PIN_LABEL_CHARS = 120

/**
 * The scope "ASK ABOUT THIS CLAIM ▸" sets.
 *
 * The pin is the AMBIENT one (`lib/consultContext.ts` / `SCOPE_PIN_ORIGIN`),
 * not a side panel and not a second consult: Consult replaces its scope pin
 * from the wall scope on every change, so setting this is the whole mechanism
 * and the same session follows the reader here with its provenance census line
 * intact. The scope is durable — a Consult tile opened LATER picks it up on
 * mount — so the control never depends on the tile already being on screen.
 *
 * `kind: 'report'` because the id must resolve to a real record and the claim
 * is published inside one; the claim itself rides the label, and the members
 * carry the two findings and the cited signals so `scopeSummary` reports the
 * aperture honestly rather than claiming the whole desk.
 */
export function crossFramingScope(view: CrossFramingView): Scope {
  const text = view.claim.text.replace(/\s+/g, ' ').trim()
  const short = text.length > PIN_LABEL_CHARS ? `${text.slice(0, PIN_LABEL_CHARS - 1)}…` : text
  return {
    kind: 'report',
    id: view.recordId,
    label: `Claim · ${view.claim.unitLabel} · “${short}”`,
    origin: 'cross-framing',
    members: {
      findingIds: [view.recordId, view.claim.headId].filter(
        (id, i, all) => !!id && all.indexOf(id) === i,
      ),
      targetIds: [view.targetId],
      signalIds: [...view.claim.signalIds],
      sourceIds: [],
      entityNames: [],
      analystIds: [view.claim.unit],
      asOf: view.claim.asOf,
      windowHours: null,
    },
  }
}
