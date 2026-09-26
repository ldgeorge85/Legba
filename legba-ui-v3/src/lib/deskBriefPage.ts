/**
 * deskBriefPage — the desk brief READ AS A PAGE, derived (P-A).
 *
 * `POST /api/v1/v3/export` has composed a full desk brief since 7b-iii: the
 * composition that quotes the desk's units, each unit's latest admitted read
 * in the composition's own declared order, the open-situations appendix, and
 * (since k5b) the server-read typed-absence block. `lib/printDocument.ts`
 * lays it out for paper. Nobody could READ it without downloading it first,
 * which is the whole gap `target.desk_brief_page` closes.
 *
 * This module is the DOM-free half: the `format:'json'` document's wire
 * shape, the projection of its citation entries onto the workstation's one
 * citation model, the per-unit evidence table, and the two generated
 * honesty sections. The panel does fetch + paint and nothing else.
 *
 * ## Three rules this file exists to keep
 *
 *  1. **The document is the document.** Every figure on the page comes off
 *     the SAME `POST /v3/export` response the markdown and the print path
 *     render. The page never recomposes a number, never re-reads a row the
 *     document already carries, and never renders a field the document did
 *     not publish. A page that agreed with the printed brief only by
 *     coincidence would be a second brief under one name.
 *  2. **Absence renders as absence.** A unit with no read has no judge
 *     score, and its cell says `unmeasured` — never `0`, never `100%`,
 *     never blank. The evidence state vocabulary is OURS (the composition's
 *     own coverage register, the gap strip's cadence verdict, the eight-kind
 *     typed absence), not a borrowed four-word enum.
 *  3. **The honesty sections are GENERATED.** "What this page does not
 *     publish" is assembled from the route's own `not_measured` classes, the
 *     coverage register's unfilled rows, the export's own missing-id
 *     placeholders and the grader's reference state. Nothing in it is
 *     hand-written prose, because hand-written prose about what is missing
 *     rots the first week nothing is missing.
 */
import {
  ABSENCE_KIND_LABEL,
  type AbsenceKind,
  type AbsenceItem,
} from '@/lib/absenceModel'
import { extractCitations, type Citation } from '@/lib/citationsModel'
import { isInsufficient, insufficientLabel, type DimensionBand } from '@/lib/evalOps'
import {
  GAP_STRIP_UNITS,
  deriveGapCell,
  type GapCellState,
  type GapLatestFinding,
  type GapUnit,
} from '@/lib/gapStripModel'
import type { CoverageRow } from '@/lib/assemblyModel'
import type { ReferenceState, UnitCorrectness } from '@/lib/unitCorrectnessModel'

// ---------------------------------------------------------------------------
// The `POST /v3/export` JSON document, as the route publishes it.
// Mirrors `registry/export_api.build_document` + `export_absences.absences_block`.
// ---------------------------------------------------------------------------

/**
 * One citation entry of an export item. The route publishes a UNIFORM key set
 * across every citation kind, so a reader never has to sniff a shape — but the
 * two date keys arrived in wave O, so they are optional HERE: a registry that
 * has not rolled past that lane answers without them, and a page that assumed
 * the key would print `undefined` as a date.
 */
export interface ExportCitation {
  marker: string
  citation_kind: string
  resolves_against: string | null
  marker_class: string | null
  signal_id: string | null
  ref_id: string | null
  title: string | null
  canonical_url: string | null
  resolved: boolean
  resolution_source: string
  archived?: boolean
  archive_sha256?: string | null
  /** o2 — the cited source's own date, when the route could date it. */
  citation_date?: string | null
  /** o2 — `published` / `fetched`: WHICH date that is. Never normalised. */
  citation_date_label?: string | null
  ordinal?: number | null
  spine_block?: Record<string, unknown> | null
  observation?: Record<string, unknown> | null
}

/** One item of the export document — a finding/report row, or a placeholder. */
export interface ExportItem {
  kind: string
  id: string
  row_kind?: string
  title?: string | null
  analyst_id?: string | null
  analyst_version?: string | null
  target_id?: string | null
  severity?: string | null
  produced_at?: string | null
  superseded?: boolean
  body?: string
  derivation?: Record<string, unknown> | null
  citations?: ExportCitation[]
  verify_state?: string | null
  verify_flags?: Record<string, number>
  confidence?: number | null
  effective_confidence?: number | null
  receipt_path?: string | null
  receipt_url?: string | null
  /** Present ONLY on the honest placeholder for a basket id with no row. */
  error?: string
}

/** One typed absence as the export's own block carries it. */
export interface ExportAbsenceItem extends Omit<AbsenceItem, 'kind'> {
  kind?: AbsenceKind
}

/** One kind's block inside the document's `absences`. */
export interface ExportAbsenceKind {
  kind: AbsenceKind
  meaning: string
  /** `absent` (items) · `clear` (read, nothing found) · `not_measured`. */
  state: 'absent' | 'clear' | 'not_measured' | string
  not_measured: string | null
  count: number
  held_back: number
  items: ExportAbsenceItem[]
}

/** The server-composed typed-absence block (k5b), when the caller asked. */
export interface ExportAbsences {
  scope: string
  read_at: string
  item_count: number
  note: string
  by_kind: ExportAbsenceKind[]
}

/** The whole document `format:'json'` returns. */
export interface ExportDocument {
  title: string
  generated_at: string
  item_count: number
  missing_count: number
  provenance_note: string
  items: ExportItem[]
  appendix: string | null
  /** Absent — not null — when the export did not ask for absences. */
  absences?: ExportAbsences
}

/**
 * Parse the artifact `exportCollection({format:'json'})` returns.
 *
 * Returns `null` rather than throwing on anything that is not an object with
 * an `items` array: a proxy error body must leave the page saying it could
 * not read the document, never take the tile down mid-read.
 */
export function parseExportDocument(raw: string | null | undefined): ExportDocument | null {
  if (!raw) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
  const d = parsed as Record<string, unknown>
  if (!Array.isArray(d.items)) return null
  return parsed as ExportDocument
}

/** The item for one id, or `null`. */
export function findItem(doc: ExportDocument | null, id: string | null): ExportItem | null {
  if (!doc || !id) return null
  return doc.items.find((i) => i.id === id) ?? null
}

// ---------------------------------------------------------------------------
// Export citations → the workstation's ONE citation model.
// ---------------------------------------------------------------------------

/**
 * The export's citation entries, in the shape `extractCitations` reads.
 *
 * The export route renames two keys on its way out — `ref_kind` becomes
 * `citation_kind` and `source` becomes `canonical_url` — and adds the two o2
 * date keys. Everything else is the stored entry. So this is a RENAME, not a
 * parser: the grounding classification, the observation camel-casing, the
 * drill rules and the marker normalisation all stay in `citationsModel`,
 * which is the one place in the bundle that knows them.
 *
 * `produced_at` is filled from `citation_date` and ONLY when the route stated
 * one, so the source tag prints a masthead with a real date or a masthead
 * alone — never a date lifted off the row that did the citing.
 */
function asStoredCitation(c: ExportCitation): Record<string, unknown> {
  return {
    marker: c.marker,
    ordinal: c.ordinal ?? undefined,
    ref_kind: c.citation_kind,
    ref_id: c.ref_id ?? undefined,
    signal_id: c.signal_id ?? undefined,
    marker_class: c.marker_class ?? undefined,
    resolves_against: c.resolves_against ?? undefined,
    title: c.title ?? undefined,
    source: c.canonical_url ?? undefined,
    produced_at: c.citation_date ?? undefined,
    observation: c.observation ?? undefined,
  }
}

/**
 * Project one export item's citations onto `Citation`, so the page renders
 * its reads through `components/CitedProse` — the ONE prose renderer — rather
 * than growing a second marker parser beside it.
 */
export function exportCitations(item: ExportItem | null): Citation[] {
  const entries = (item?.citations ?? []).map(asStoredCitation)
  return entries.length > 0 ? extractCitations({ citations: entries }) : []
}

/**
 * The endnote's date line, in the route's own words: `published 2026-09-04`,
 * `fetched 2026-09-04`, or the honest absence.
 *
 * The LABEL is never dropped and never normalised into one word — "published"
 * and "fetched" are different claims about a source, and a page with no
 * tooltip beside the line has to say which one the reader is holding.
 */
export const CITATION_UNDATED = 'no date on record'

export function citationDateLine(c: ExportCitation): string {
  const date = (c.citation_date ?? '').trim()
  if (!date) return CITATION_UNDATED
  const label = (c.citation_date_label ?? '').trim()
  return label ? `${label} ${date}` : date
}

// ---------------------------------------------------------------------------
// The evidence table — one row per bounded unit of the desk's roster.
// ---------------------------------------------------------------------------

/**
 * The evidence state, in OUR vocabulary. Six of the seven come from a record
 * that already said them: `in_basis` / `below_floor` / `unverified` /
 * `no_head_in_horizon` are the composition's own coverage register verbatim,
 * `insufficient` is the scorecard's banding verdict, and `stale` / `no_read`
 * are the gap strip's cadence check.
 */
export type EvidenceState =
  | 'in_basis'
  | 'below_floor'
  | 'unverified'
  | 'no_head_in_horizon'
  | 'insufficient'
  | 'stale'
  | 'no_read'

/** The word each state prints. Never a colour alone, never an abbreviation. */
export const EVIDENCE_STATE_LABEL: Record<EvidenceState, string> = {
  in_basis: 'in basis',
  below_floor: 'below floor',
  unverified: 'unverified',
  no_head_in_horizon: 'no head in horizon',
  insufficient: 'insufficient evidence',
  stale: 'stale',
  no_read: 'no read yet',
}

/** Which record decided the state — printed, so the reader can go check it. */
export type EvidenceStateSource = 'coverage register' | 'scorecard' | 'cadence'

export interface EvidenceUnitRow {
  unitId: string
  label: string
  /** The unit's own question, off the composition's block. Null when the
   *  composition carried no block for it — never a fabricated question. */
  question: string | null
  state: EvidenceState
  stateSource: EvidenceStateSource
  /** The composition's coverage-register status, verbatim, when it named
   *  this unit at all. */
  coverageStatus: string | null
  /** The latest read's instant, or null when there is no read. */
  readAt: string | null
  /** The finding to drill, or null. */
  findingId: string | null
  /** The scorecard's folded faithfulness for this dimension — NEVER pooled
   *  with correctness, and null when unmeasured. */
  judgeScore: number | null
  /** The grader's coverage share for this unit, or null when ungraded. */
  correctnessCoverage: number | null
  /** The server-composed correctness badge, verbatim, or null. */
  correctnessBadge: string | null
  /** The typed absences whose subject IS this unit, in the route's order. */
  absences: { kind: AbsenceKind; label: string; reason: string; stale: boolean }[]
}

/** The state the coverage register's own word maps onto, when it is not
 *  `in_basis`. An unknown status is reported as `unverified` — the weakest
 *  claim of the three, so an unrecognised word never upgrades a unit. */
function coverageState(status: string): EvidenceState | null {
  switch (status) {
    case 'in_basis':
      return null
    case 'below_floor':
      return 'below_floor'
    case 'no_head_in_horizon':
      return 'no_head_in_horizon'
    case 'unverified':
      return 'unverified'
    default:
      return 'unverified'
  }
}

export interface EvidenceTableInput {
  units?: readonly GapUnit[]
  /** The composition's declared coverage register (`assembly.coverage`). */
  coverage: readonly CoverageRow[]
  /** Each unit's block question, keyed by unit id (`assembly.blocks`). */
  questions: ReadonlyMap<string, string>
  /** The unit's actual latest read, keyed by `analyst_id`. */
  latestByUnit: ReadonlyMap<string, GapLatestFinding>
  /** The banded scorecard's per-dimension verdicts. */
  dimensions: Record<string, DimensionBand> | null | undefined
  /** The desk's correctness rows, keyed by `analyst_id`. */
  correctness: ReadonlyMap<string, UnitCorrectness>
  /** The export document's typed-absence block. */
  absences: ExportAbsences | null | undefined
  now?: Date
}

/** Every typed absence whose SUBJECT is this unit, across all kinds. */
function absencesForUnit(
  absences: ExportAbsences | null | undefined,
  unitId: string,
): EvidenceUnitRow['absences'] {
  const out: EvidenceUnitRow['absences'] = []
  for (const block of absences?.by_kind ?? []) {
    for (const item of block.items ?? []) {
      if (item.subject !== unitId) continue
      out.push({
        kind: block.kind,
        label: ABSENCE_KIND_LABEL[block.kind] ?? String(block.kind),
        reason: item.reason,
        stale: !!item.stale,
      })
    }
  }
  return out
}

/**
 * One row per bounded unit of the desk's roster — the table World Monitor's
 * `Evidence state` column suggested, over OUR units and OUR vocabularies.
 *
 * The roster, not the composition, is the denominator: a unit the composition
 * never carried is a row saying so, which is the fact a reader most needs and
 * the one a table built from the carried blocks alone cannot state.
 *
 * PRIORITY. The composition's own coverage register wins when it refused the
 * unit, because that is the record's own verdict about its own basis. Below
 * it, the scorecard's `insufficient-evidence` banding; below that, the cadence
 * check. Typed absence is never a state here — it is a separate column,
 * because "this unit banded below floor" and "nothing has been collected for
 * it since Tuesday" are two different facts about one unit.
 */
export function deriveEvidenceTable(input: EvidenceTableInput): EvidenceUnitRow[] {
  const {
    units = GAP_STRIP_UNITS,
    coverage,
    questions,
    latestByUnit,
    dimensions,
    correctness,
    absences,
    now,
  } = input
  const coverageByUnit = new Map(coverage.map((c) => [c.unit, c]))

  return units.map((unit) => {
    const cell = deriveGapCell({
      unit,
      latestFinding: latestByUnit.get(unit.id),
      dimension: dimensions?.[unit.id],
      now,
    })
    const cov = coverageByUnit.get(unit.id) ?? null
    const covState = cov ? coverageState(cov.status) : null
    const dimension = dimensions?.[unit.id]

    let state: EvidenceState
    let stateSource: EvidenceStateSource
    if (covState) {
      state = covState
      stateSource = 'coverage register'
    } else if (dimension && isInsufficient(dimension) && dimension.reason !== 'no-finding') {
      state = 'insufficient'
      stateSource = 'scorecard'
    } else if (cell.state === 'none') {
      state = 'no_read'
      stateSource = 'cadence'
    } else if (cell.state === 'stale') {
      state = 'stale'
      stateSource = 'cadence'
    } else if (cov) {
      state = 'in_basis'
      stateSource = 'coverage register'
    } else {
      // A current read the composition's register never named: the cadence
      // check is all that can honestly be said about it.
      state = 'in_basis'
      stateSource = 'cadence'
    }

    const graded = correctness.get(unit.id) ?? null
    return {
      unitId: unit.id,
      label: unit.label,
      question: questions.get(unit.id) ?? null,
      state,
      stateSource,
      coverageStatus: cov?.status ?? null,
      readAt: cell.producedAt ?? cov?.read_date ?? null,
      findingId: cell.findingId,
      judgeScore: cell.judgeScore,
      correctnessCoverage: graded?.coverage_share ?? null,
      correctnessBadge: graded?.badge ?? null,
      absences: absencesForUnit(absences, unit.id),
    }
  })
}

/** The scorecard's machine reason for an `insufficient` row, in words. */
export function evidenceStateDetail(
  row: EvidenceUnitRow,
  dimensions: Record<string, DimensionBand> | null | undefined,
): string | null {
  if (row.state !== 'insufficient') return null
  return insufficientLabel(dimensions?.[row.unitId]?.reason ?? null)
}

// ---------------------------------------------------------------------------
// "What this page does not publish" — GENERATED, never written.
// ---------------------------------------------------------------------------

/** One named absence of publication, with the record that declared it. */
export interface NotPublishedLine {
  /** What is not published. */
  subject: string
  /** Why, in the words of whatever declared it. */
  why: string
  /** Which record this line was read off. */
  source: 'typed absence' | 'coverage register' | 'export document' | 'grader reference'
}

export interface NotPublishedInput {
  doc: ExportDocument | null
  /** Units the composition's register named and carried no read for. */
  missingUnits: readonly { unit: string; unitName: string | null; status: string }[]
  /** The desk's independent-reference state, for the correctness half. */
  referenceState?: ReferenceState | null
  /** The desk's correctness rows, so an ungraded unit is named as ungraded. */
  ungradedUnits?: readonly string[]
}

/** The line a page carries when the export was composed without absences. */
export const ABSENCES_NOT_ASKED =
  'the typed-absence block was not composed for this document, so this page ' +
  'cannot say what the desk is missing'

/**
 * Assemble the section from records that already said these things.
 *
 * Every line is lifted off a field: the route's own `not_measured` sentence,
 * the coverage register's status word, the export's own missing-id
 * placeholder, the grader's reference state. Nothing here is a sentence
 * somebody wrote about a condition that might have changed since.
 */
export function notPublished(input: NotPublishedInput): NotPublishedLine[] {
  const { doc, missingUnits, referenceState, ungradedUnits } = input
  const out: NotPublishedLine[] = []

  if (!doc) return out

  if (!doc.absences) {
    out.push({ subject: 'typed absence', why: ABSENCES_NOT_ASKED, source: 'export document' })
  } else {
    for (const block of doc.absences.by_kind) {
      if (block.state !== 'not_measured') continue
      out.push({
        subject: `typed absence · ${ABSENCE_KIND_LABEL[block.kind] ?? block.kind}`,
        why: block.not_measured ?? 'the route could not read this kind for this desk',
        source: 'typed absence',
      })
    }
    for (const block of doc.absences.by_kind) {
      if (block.held_back > 0) {
        out.push({
          subject: `typed absence · ${ABSENCE_KIND_LABEL[block.kind] ?? block.kind}`,
          why:
            `${block.held_back} of ${block.count} items are held back by the ` +
            `document's own per-kind cap and are not printed here`,
          source: 'typed absence',
        })
      }
    }
  }

  for (const u of missingUnits) {
    out.push({
      subject: `unit · ${u.unitName ?? u.unit}`,
      why:
        `the composition's coverage register names this unit and carried no ` +
        `admitted read for it (${u.status})`,
      source: 'coverage register',
    })
  }

  const missing = doc.items.filter((i) => i.error)
  for (const i of missing) {
    out.push({
      subject: `${i.kind} · ${i.id}`,
      why: i.error ?? 'not found in substrate',
      source: 'export document',
    })
  }

  if (referenceState && referenceState !== 'current') {
    out.push({
      subject: 'correctness against an independent reference',
      why:
        referenceState === 'none'
          ? 'no reference has been built for this desk, so no unit carries a correctness share'
          : 'the reference for this desk is stale, so its correctness shares are last known, not re-checked',
      source: 'grader reference',
    })
  }

  for (const unit of ungradedUnits ?? []) {
    out.push({
      subject: `correctness · ${unit}`,
      why: 'the grader has not reached this unit under the current reference',
      source: 'grader reference',
    })
  }

  return out
}

// ---------------------------------------------------------------------------
// Reading limits — every figure with its unit and its as-of.
// ---------------------------------------------------------------------------

/** One limit on how far this page's numbers reach. */
export interface ReadingLimit {
  label: string
  value: string
}

export interface ReadingLimitsInput {
  doc: ExportDocument | null
  /** The composition's own `assembly.as_of`. */
  compositionAsOf?: string | null
  /** The scale/method stamp line, already read (`lib/scaleStamp`). */
  scaleLine?: string | null
  /** The banded scorecard's own instant. */
  scorecardAt?: string | null
  /** The grader reference window + state. */
  reference?: {
    state: ReferenceState
    window_start: string | null
    window_end: string | null
    age_days: number | null
  } | null
}

/** Absence, printed as absence — the one string every empty limit uses. */
export const LIMIT_UNKNOWN = 'not recorded'

/**
 * The limits, each carrying the unit of what it measures and the instant it
 * was measured at. Nothing is computed from the clock the page was opened
 * on: a limit the records do not state is stated as not recorded.
 */
export function readingLimits(input: ReadingLimitsInput): ReadingLimit[] {
  const { doc, compositionAsOf, scaleLine, scorecardAt, reference } = input
  const out: ReadingLimit[] = [
    { label: 'scale era', value: scaleLine?.trim() || LIMIT_UNKNOWN },
    { label: 'composition as of', value: compositionAsOf?.trim() || LIMIT_UNKNOWN },
    { label: 'document composed at', value: doc?.generated_at?.trim() || LIMIT_UNKNOWN },
    {
      label: 'typed absence read at',
      value: doc?.absences?.read_at?.trim() || LIMIT_UNKNOWN,
    },
    { label: 'judge scorecard produced at', value: scorecardAt?.trim() || LIMIT_UNKNOWN },
  ]
  if (reference) {
    const window =
      reference.window_start && reference.window_end
        ? `${reference.window_start.slice(0, 10)} → ${reference.window_end.slice(0, 10)}`
        : LIMIT_UNKNOWN
    const age =
      reference.age_days != null ? ` · ${reference.age_days.toFixed(1)} days old` : ''
    out.push({
      label: 'grader reference',
      value: `${reference.state} · window ${window}${age}`,
    })
  } else {
    out.push({ label: 'grader reference', value: LIMIT_UNKNOWN })
  }
  return out
}

/** A coverage / correctness share as a percentage, or the absence word. */
export function shareText(share: number | null | undefined): string {
  return typeof share === 'number' && Number.isFinite(share)
    ? `${(share * 100).toFixed(1)}%`
    : 'unmeasured'
}

/** The judge (faithfulness) cell — a share with its unit, or `unmeasured`. */
export function judgeText(score: number | null | undefined): string {
  return typeof score === 'number' && Number.isFinite(score)
    ? `${(score * 100).toFixed(0)}% faithfulness`
    : 'unmeasured'
}

/** The gap strip's own state for a unit, kept so a caller can cross-check the
 *  page's evidence state against the strip a reader may have beside it. */
export function cadenceState(row: EvidenceUnitRow): GapCellState {
  if (row.state === 'no_read') return 'none'
  if (row.state === 'stale') return 'stale'
  if (row.state === 'insufficient') return 'insufficient'
  return 'current'
}
