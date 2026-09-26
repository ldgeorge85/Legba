/**
 * assemblyModel — the reader's half of the composition-demotion contract (D-4).
 *
 * The producer (D-2) writes `data.data.assembly`, a typed `assembly.v1` document,
 * onto the SAME `kind='finding'` row the Morning Read already reads
 * (`DEMOTION_D1_SPEC_2026-09-04.md` §1.1 — no new kind, no new analyst_id, so
 * `/api/v1/findings?analyst_id=world_assessor` keeps serving it and this module
 * needs no new endpoint). The Assessment (D-6) is its own row,
 * `analyst_id='world_assessment'`, carrying `data.data.assessment` and a
 * `derived_from` of length exactly one — the spine it was written from.
 *
 * ── WHY THIS FILE IS DEFENSIVE RATHER THAN A CAST ─────────────────────────────
 * `/findings` returns `data` as unstructured JSON. There is no schema validation
 * anywhere in the tree (spec §1.1) and D-2 lands on its own train, so this reader
 * WILL meet: legacy rows with no `assembly` key at all (regime `legacy`), rows
 * written against a future `assembly.v2`, and — during the A/B — both regimes
 * inside one history list. Every projector below returns `null` rather than
 * throwing, and every optional field is optional here too, so a missing
 * `salience` block costs one absent chip and not a blank page.
 *
 * The one thing this module will NOT do is invent. A field the payload does not
 * carry renders as absent, never as zero: the whole point of the demotion is that
 * the reader can trust that what it shows was recorded.
 */

/** §1.1b — the payload version. The reader branches on it. */
export const ASSEMBLY_SCHEMA = 'assembly.v1'
export const ASSESSMENT_SCHEMA = 'assessment.v1'

export type AssemblyTier = 'country' | 'world' | 'thematic'
export type AssemblyRegime = 'assembly' | 'legacy'
export type LeadKind = 'earned_single' | 'co_leads' | 'none'
export type SpanRole = 'bluf' | 'what_changed' | 'body' | 'indicator' | 'context_body'

/**
 * P3 Lane A — CONTEXT, not quotation. A COUNTRY block carries its origin desk
 * head in FULL (offsets [0:len], same digest, same construction gate) so the
 * per-country Assessment can argue ACROSS dimensions. It is NOT part of the
 * record: the server's own render omits it and every reader here must too, or
 * a record card that quotes one sentence per desk starts printing eight whole
 * desk reads. `projectBlock` splits it off into `contextSpans` so no component
 * has to know — `block.spans` stays exactly the quotations it has always been.
 */
export const CONTEXT_SPAN_ROLES: readonly SpanRole[] = ['context_body']

export function isContextSpan(span: { role: SpanRole }): boolean {
  return CONTEXT_SPAN_ROLES.includes(span.role)
}
export type EvidenceTier = 'basis' | 'periphery'

/** §1.2 — a span carries its ORIGIN, never a path. Depth-1, always. */
export interface SpanOrigin {
  head_id: string
  start: number
  end: number
  body_sha256: string
  body_len: number
}

export interface AssemblySpan {
  role: SpanRole
  text: string
  origin: SpanOrigin
  /** The DESK's own `[N]` markers inside the span (already full-width folded). */
  markers: string[]
  /** §3.2 — the collection-denominator scope tokens the source sentence carried. */
  scope_tokens: string[]
}

export interface AssemblySignal {
  marker: string
  signal_id: string | null
  source_id: string | null
  title: string
  url: string | null
  age_h: number | null
  salience_magnitude: number | null
  also_cited_by: { desk: string; target_id: string | null }[]
}

export interface BlockVerify {
  overall_score: number | null
  effective_confidence: number | null
  checkable_claims: number | null
  supported_claims: number | null
  judge_status: 'llm' | 'deterministic' | 'unavailable' | string
  score_state: string
}

export interface BlockSalience {
  cited_max: number | null
  cited_mass: number | null
  n_above: number | null
  n_cited_scored: number | null
  legacy_magnitude: number | null
}

export interface AssemblyBlock {
  ordinal: number
  finding_id: string
  desk: string
  target_id: string | null
  target_name: string | null
  /** §B.4 Correction 3 — the block header IS the desk's bounded question. */
  question: string
  question_source: 'descriptor' | 'fallback_desk_name' | string
  produced_at: string | null
  evidence_age_h: number | null
  tier: EvidenceTier
  severity: string | null
  verify: BlockVerify | null
  salience: BlockSalience | null
  /** THE QUOTATIONS — what the record carries. Never the context body. */
  spans: AssemblySpan[]
  /**
   * P3 Lane A — the origin desk head IN FULL, country tier only, empty
   * everywhere else. Available to a reader that wants to open the read behind a
   * quoted sentence; never rendered as part of the record.
   */
  contextSpans: AssemblySpan[]
  signals: AssemblySignal[]
  corroboration: { n_sources: number; n_desks_sharing: number } | null
}

export interface LeadTest {
  key: string
  top_share: number
  ratio_12: number
  bar_share: number
  bar_ratio: number
  earned: boolean
  n_candidates: number
}

export interface AssemblyLead {
  kind: LeadKind
  block_ordinals: number[]
  test: LeadTest | null
}

export interface TensionRef {
  ordinal: number
  span_index: number
}

export interface TensionDroppedRef {
  finding_id: string
  desk: string
  target_id: string | null
  target_name: string | null
  span: string
}

export interface AssemblyTension {
  kind: 'carried_pair' | 'carried_vs_dropped' | 'dropped_pair' | string
  a: TensionRef | null
  b: TensionRef | null
  b_ref: TensionDroppedRef | null
  /**
   * W-1 — was the B side CARRIED by this record?
   *
   * `false` means the pair is evidence for the drop ledger, NOT a conflict the
   * record published: nothing was carried on the B side to conflict with. The
   * reader must never render a `b_carried: false` tension in the same voice as
   * a carried pair — on 2026-09-06 the world read's only declared tension was
   * that shape and the Assessment narrated it as a live conflict between two
   * published reads.
   *
   * Derived from `kind` on a record written before the producer stamped it, so
   * an older payload projects with the same meaning rather than a default of
   * convenience.
   */
  b_carried: boolean
  /** The drop why-class of the uncarried side; `null` when B was carried. */
  b_why: string | null
  statement: string
  statement_source: 'template' | 'judge' | string
  detector: string
  same_target: boolean
  same_window_h: number | null
}

export interface TensionChecked {
  pairs_examined: number
  pairs_found: number
  /**
   * W-1 — `pairs_found` split into its two populations. `null` (never 0) on a
   * record written before the producer published the split: an unmeasured
   * split is stated as unknown, because a zero here reads as "checked, and
   * none of them were cross-tier", which is the opposite of what happened.
   */
  pairs_found_carried: number | null
  pairs_found_uncarried: number | null
  scope: string
  /** §1.6 — a checked negative that does not say WHAT it checked is M-11 in a costume. */
  scope_note: string | null
}

export interface DropRow {
  finding_id: string
  desk: string
  target_id: string | null
  target_name: string | null
  title: string | null
  rank: number | null
  tier: EvidenceTier | null
  severity: string | null
  cited_mass: number | null
  why: string
}

export interface DropCounts {
  shown: number
  carried: number
  shown_not_carried: number
  candidates: number
  not_selected: number
  trimmed: number
  below_floor: number
  no_head: number
  /** §1.7 — a COUNT, never a list. No why-class is derivable for these. */
  invisible_heads: number
}

export interface DropLedger {
  shown_not_carried: DropRow[]
  not_selected: DropRow[]
  trimmed: DropRow[]
  below_floor: DropRow[]
  no_head: (DropRow | string)[]
  counts: DropCounts
  why_classes: string[]
}

export interface CoverageRow {
  unit: string
  unit_name?: string | null
  status: 'in_basis' | 'below_floor' | 'unverified' | 'no_head_in_horizon' | string
  read_date: string | null
  age_h: number | null
  effective_confidence?: number | null
}

export interface AssemblyPayload {
  schema: string
  regime: AssemblyRegime
  tier: AssemblyTier
  as_of: string
  lead: AssemblyLead | null
  blocks: AssemblyBlock[]
  tensions: AssemblyTension[]
  tension_checked: TensionChecked | null
  drops: DropLedger | null
  coverage: CoverageRow[]
  connectives: { vocabulary_version: string } | null
}

/** §3.6 READ time — a row whose arms failed refuses the affected blocks. */
export interface AssemblyArms {
  gate: 'passed' | 'failed' | string
  quote_fidelity: number | null
  scope_preservation: number | null
  attribution_equality: number | null
  coverage_completeness: number | null
  spans: number | null
  failed_ordinals: number[]
}

// ── the Assessment channel (§2) ──────────────────────────────────────────────

export type UnsupportedClass =
  | 'rank'
  | 'superlative'
  | 'causal_link'
  | 'scope_widening'
  | 'instrument_prose'
  | 'aperture_unrostered'
  | 'aperture_guess'
  | 'uncited'
  | 'fact'

export interface UnsupportedMark {
  sentence_index: number
  char_start: number
  char_end: number
  text: string
  class: UnsupportedClass | string
  detector: 'deterministic' | 'judge' | string
  note: string
}

export interface ExternalAccuracy {
  value: number
  n: number
  round: string
  as_of: string
  population: string
  lineage: string
}

/**
 * W-7 — the STANDING number, from the auditor's own ledger (design §3.4).
 *
 * `value` is `null` whenever `state` is not `measured`, so a reader that prints
 * it unconditionally prints an absence rather than a rate the instrument did not
 * earn. `state` is a WORD — `unmeasured` is not `0.0`, which on a badge reads as
 * a measured failure — and `state_note` is the server-composed sentence the
 * reader renders in place of the number.
 */
export interface StandingAccuracy {
  value: number | null
  state: 'measured' | 'unmeasured' | 'instrument_limited' | 'sampled' | string
  n_decided: number
  n_searched: number
  decided_rate: number | null
  window_days: number
  population: string
  grader_family: string | null
  instrument_limited: boolean
  sample_fraction: number | null
  state_note: string | null
}

export interface AssessmentBadge {
  fidelity_to_spine: number | null
  fidelity_n: number | null
  external_accuracy: ExternalAccuracy | null
  external_accuracy_note: string | null
  /** The VOICE's standing number — this channel's own sentences. */
  standing_accuracy: StandingAccuracy | null
  standing_state: string
}

/**
 * The RECORD's side of the pair (§3.4, F-12) — the truth of the DESK SENTENCES
 * the spine quotes, carried on its own key so a reader can never sum it with the
 * voice's. `standing_note` is the demotion in one line and is always printed
 * beside the number.
 */
export interface SpineBadge {
  standing_accuracy: StandingAccuracy | null
  standing_note: string | null
}

/**
 * W-3 — one `data.data.citations` entry off an ASSESSMENT row.
 *
 * THE READING PROBLEM, and it is a reading problem only. D-6 fences the voice
 * to its spine (`derived_from == [spine_id]`), so every `ref_id` the channel
 * writes is that one row — the 2026-09-06 12:15Z Assessment carried four
 * citations and all four resolved to the same uuid. The fence is the contract
 * and nothing here widens it by a byte. What it does is stop DISCARDING the
 * two keys the producer already stamps: `ordinal` (which spine block the
 * marker names) and `spine_block` (that block's own desk head). Resolved
 * against the spine those two are a chain — Assessment → world read block N →
 * the country/escalation read → its signals — which is the lineage a reader
 * thought was missing.
 */
export interface AssessmentCitationBlock {
  finding_id: string | null
  desk: string | null
  target_id: string | null
}

export interface AssessmentCitation {
  marker: string
  ordinal: number
  ref_id: string
  ref_kind: string
  source: string
  title: string
  /** The block's own head. NOT a ref_id — this channel did not read it. */
  spine_block: AssessmentCitationBlock | null
  tier: EvidenceTier | null
  effective_confidence: number | null
}

/**
 * One citation resolved into its hops. `in_spine` is the honesty bit: an
 * ordinal the spine payload does not carry (a spine that failed to load, or an
 * A/B pairing across cycles) resolves to the hops the CITATION knows and states
 * that the block itself was not found, rather than rendering an empty block as
 * an absent one.
 */
export interface AssessmentCitationChain {
  marker: string
  ordinal: number
  /** Hop 1 — the row the channel actually read. */
  spine_id: string
  spine_title: string
  /** Hop 2 — the spine block that ordinal names, when the spine is loaded. */
  block: AssemblyBlock | null
  block_finding_id: string | null
  block_desk: string | null
  block_target_id: string | null
  /** Hop 3 — the signals that block's own head quotes. */
  signals: AssemblySignal[]
  in_spine: boolean
}

export interface AssessmentPayload {
  schema: string
  spine_id: string
  spine_schema: string
  lead_test: LeadTest | null
  markers: { ordinal: number; sentence_index: number }[]
  unsupported: UnsupportedMark[]
  fidelity: { checkable: number; supported: number; score: number } | null
  badge: AssessmentBadge | null
  spine_badge: SpineBadge | null
}

// ── the wire row ─────────────────────────────────────────────────────────────

/** The `/api/v1/findings` row, projected to only what this reader touches. */
export interface ReadFindingRow {
  id: string
  title?: string | null
  body?: string | null
  severity?: string | null
  confidence?: number | null
  analyst_id?: string | null
  target_id?: string | null
  produced_at: string
  derived_from?: string[] | null
  data?: unknown
  payload?: unknown
  verification?: Record<string, unknown> | null
}

// ── coercion helpers ─────────────────────────────────────────────────────────

function obj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}
function arr(v: unknown): unknown[] {
  return Array.isArray(v) ? v : []
}
function str(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback
}
function strOrNull(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}
function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}
function int(v: unknown, fallback = 0): number {
  return typeof v === 'number' && Number.isFinite(v) ? Math.trunc(v) : fallback
}

/** Coerce the row's payload (object or JSON string) into a plain object. */
export function readPayload(row: ReadFindingRow): Record<string, unknown> {
  const raw = row.data ?? row.payload
  const direct = obj(raw)
  if (direct) return direct
  if (typeof raw === 'string') {
    try {
      return obj(JSON.parse(raw)) ?? {}
    } catch {
      return {}
    }
  }
  return {}
}

/** `data.data` — where every composition payload lives. */
function innerData(row: ReadFindingRow): Record<string, unknown> {
  return obj(readPayload(row).data) ?? {}
}

// ── projectors ───────────────────────────────────────────────────────────────

function projectSpan(v: unknown): AssemblySpan | null {
  const o = obj(v)
  if (!o) return null
  const origin = obj(o.origin)
  const text = str(o.text)
  if (text === '' || !origin) return null
  return {
    role: (str(o.role, 'body') as SpanRole) || 'body',
    text,
    origin: {
      head_id: str(origin.head_id),
      start: int(origin.start),
      end: int(origin.end),
      body_sha256: str(origin.body_sha256),
      body_len: int(origin.body_len),
    },
    markers: arr(o.markers).map((m) => str(m)).filter((m) => m !== ''),
    scope_tokens: arr(o.scope_tokens).map((m) => str(m)).filter((m) => m !== ''),
  }
}

function projectSignal(v: unknown): AssemblySignal | null {
  const o = obj(v)
  if (!o) return null
  return {
    marker: str(o.marker),
    signal_id: strOrNull(o.signal_id),
    source_id: strOrNull(o.source_id),
    title: str(o.title) || '(untitled wire item)',
    url: strOrNull(o.url),
    age_h: num(o.age_h),
    salience_magnitude: num(o.salience_magnitude),
    also_cited_by: arr(o.also_cited_by)
      .map((x) => {
        const c = obj(x)
        return c ? { desk: str(c.desk), target_id: strOrNull(c.target_id) } : null
      })
      .filter((x): x is { desk: string; target_id: string | null } => x !== null && x.desk !== ''),
  }
}

function projectBlock(v: unknown): AssemblyBlock | null {
  const o = obj(v)
  if (!o) return null
  const allSpans = arr(o.spans).map(projectSpan).filter((s): s is AssemblySpan => s !== null)
  // P3 Lane A — the split happens HERE, once, so every consumer of `spans`
  // keeps meaning "the quotations" with no edit. See CONTEXT_SPAN_ROLES.
  const spans = allSpans.filter((s) => !isContextSpan(s))
  const contextSpans = allSpans.filter((s) => isContextSpan(s))
  const findingId = str(o.finding_id)
  if (findingId === '' || spans.length === 0) return null
  const verify = obj(o.verify)
  const salience = obj(o.salience)
  const corr = obj(o.corroboration)
  const desk = str(o.desk)
  return {
    ordinal: int(o.ordinal, 0),
    finding_id: findingId,
    desk,
    target_id: strOrNull(o.target_id),
    target_name: strOrNull(o.target_name),
    question: str(o.question) || desk.replace(/_/g, ' '),
    question_source: str(o.question_source, 'fallback_desk_name'),
    produced_at: strOrNull(o.produced_at),
    evidence_age_h: num(o.evidence_age_h),
    tier: str(o.tier, 'basis') === 'periphery' ? 'periphery' : 'basis',
    severity: strOrNull(o.severity),
    verify: verify
      ? {
          overall_score: num(verify.overall_score),
          effective_confidence: num(verify.effective_confidence),
          checkable_claims: num(verify.checkable_claims),
          supported_claims: num(verify.supported_claims),
          judge_status: str(verify.judge_status, 'unavailable'),
          score_state: str(verify.score_state, 'unscored'),
        }
      : null,
    salience: salience
      ? {
          cited_max: num(salience.cited_max),
          cited_mass: num(salience.cited_mass),
          n_above: num(salience.n_above),
          n_cited_scored: num(salience.n_cited_scored),
          legacy_magnitude: num(salience.legacy_magnitude),
        }
      : null,
    spans,
    contextSpans,
    signals: arr(o.signals).map(projectSignal).filter((s): s is AssemblySignal => s !== null),
    corroboration: corr
      ? { n_sources: int(corr.n_sources), n_desks_sharing: int(corr.n_desks_sharing) }
      : null,
  }
}

function projectTensionRef(v: unknown): TensionRef | null {
  const o = obj(v)
  if (!o) return null
  const ordinal = num(o.ordinal)
  if (ordinal === null) return null
  return { ordinal, span_index: int(o.span_index) }
}

function projectTension(v: unknown): AssemblyTension | null {
  const o = obj(v)
  if (!o) return null
  const statement = str(o.statement)
  if (statement === '') return null
  const bRef = obj(o.b_ref)
  return {
    kind: str(o.kind, 'carried_pair'),
    a: projectTensionRef(o.a),
    b: projectTensionRef(o.b),
    b_ref: bRef
      ? {
          finding_id: str(bRef.finding_id),
          desk: str(bRef.desk),
          target_id: strOrNull(bRef.target_id),
          target_name: strOrNull(bRef.target_name),
          span: str(bRef.span),
        }
      : null,
    // W-1. The stamp when the producer wrote one; otherwise read off the shape
    // the pre-W-1 producer did publish — `carried_vs_dropped` / a `b_ref` with
    // no `b` ordinal IS an uncarried B side, and that fact was always true of
    // those rows even when nothing said so in a field.
    b_carried:
      typeof o.b_carried === 'boolean'
        ? o.b_carried
        : !(str(o.kind) === 'carried_vs_dropped' || bRef !== null),
    b_why: strOrNull(o.b_why),
    statement,
    statement_source: str(o.statement_source, 'template'),
    detector: str(o.detector, 'direction_conflict'),
    same_target: o.same_target === true,
    same_window_h: num(o.same_window_h),
  }
}

function projectDropRow(v: unknown): DropRow | null {
  const o = obj(v)
  if (!o) return null
  const id = str(o.finding_id)
  if (id === '') return null
  return {
    finding_id: id,
    desk: str(o.desk),
    target_id: strOrNull(o.target_id),
    target_name: strOrNull(o.target_name),
    title: strOrNull(o.title),
    rank: num(o.rank),
    tier: (strOrNull(o.tier) as EvidenceTier | null) ?? null,
    severity: strOrNull(o.severity),
    cited_mass: num(o.cited_mass),
    why: str(o.why, 'not_selected'),
  }
}

function projectDrops(v: unknown): DropLedger | null {
  const o = obj(v)
  if (!o) return null
  const counts = obj(o.counts) ?? {}
  const rows = (k: string) =>
    arr(o[k]).map(projectDropRow).filter((r): r is DropRow => r !== null)
  return {
    shown_not_carried: rows('shown_not_carried'),
    not_selected: rows('not_selected'),
    trimmed: rows('trimmed'),
    below_floor: rows('below_floor'),
    // `no_head` may be a bare unit-id list (the coverage ledger's own grain).
    no_head: arr(o.no_head).map((x) => projectDropRow(x) ?? str(x)).filter((x) => x !== ''),
    counts: {
      shown: int(counts.shown),
      carried: int(counts.carried),
      shown_not_carried: int(counts.shown_not_carried),
      candidates: int(counts.candidates),
      not_selected: int(counts.not_selected),
      trimmed: int(counts.trimmed),
      below_floor: int(counts.below_floor),
      no_head: int(counts.no_head),
      invisible_heads: int(counts.invisible_heads),
    },
    why_classes: arr(o.why_classes).map((x) => str(x)).filter((x) => x !== ''),
  }
}

function projectCoverage(v: unknown): CoverageRow | null {
  const o = obj(v)
  if (!o) return null
  const unit = str(o.unit)
  if (unit === '') return null
  return {
    unit,
    unit_name: strOrNull(o.unit_name),
    status: str(o.status, 'unverified'),
    read_date: strOrNull(o.read_date),
    age_h: num(o.age_h),
    effective_confidence: num(o.effective_confidence),
  }
}

/**
 * Project `data.data.assembly` off a findings row.
 *
 * Returns `null` for a legacy prose row, an unrecognised schema, or a payload
 * with no resolvable block — all three of which the reader renders as "this run
 * predates the assembly", never as an error.
 */
export function projectAssembly(row: ReadFindingRow): AssemblyPayload | null {
  const a = obj(innerData(row).assembly)
  if (!a) return null
  const schema = str(a.schema)
  if (schema !== ASSEMBLY_SCHEMA) return null
  const blocks = arr(a.blocks).map(projectBlock).filter((b): b is AssemblyBlock => b !== null)
  if (blocks.length === 0) return null
  const lead = obj(a.lead)
  const leadTest = lead ? obj(lead.test) : null
  const checked = obj(a.tension_checked)
  return {
    schema,
    regime: str(a.regime, 'assembly') === 'legacy' ? 'legacy' : 'assembly',
    tier: (str(a.tier, 'world') as AssemblyTier) ?? 'world',
    as_of: str(a.as_of, row.produced_at),
    lead: lead
      ? {
          kind: (str(lead.kind, 'none') as LeadKind) ?? 'none',
          block_ordinals: arr(lead.block_ordinals)
            .map((x) => num(x))
            .filter((x): x is number => x !== null),
          test: leadTest
            ? {
                key: str(leadTest.key, 'cited_mass.v1'),
                top_share: num(leadTest.top_share) ?? 0,
                ratio_12: num(leadTest.ratio_12) ?? 0,
                bar_share: num(leadTest.bar_share) ?? 0.15,
                bar_ratio: num(leadTest.bar_ratio) ?? 1.5,
                earned: leadTest.earned === true,
                n_candidates: int(leadTest.n_candidates),
              }
            : null,
        }
      : null,
    blocks,
    tensions: arr(a.tensions).map(projectTension).filter((t): t is AssemblyTension => t !== null),
    tension_checked: checked
      ? {
          pairs_examined: int(checked.pairs_examined),
          pairs_found: int(checked.pairs_found),
          pairs_found_carried: num(checked.pairs_found_carried),
          pairs_found_uncarried: num(checked.pairs_found_uncarried),
          scope: str(checked.scope, 'shown_only'),
          scope_note: strOrNull(checked.scope_note),
        }
      : null,
    drops: projectDrops(a.drops),
    coverage: arr(a.coverage).map(projectCoverage).filter((c): c is CoverageRow => c !== null),
    connectives: obj(a.connectives)
      ? { vocabulary_version: str(obj(a.connectives)!.vocabulary_version, 'connective.v1') }
      : null,
  }
}

/** §3.6 — the verify arms' gate, off the row's `verification` block. */
export function projectArms(row: ReadFindingRow): AssemblyArms | null {
  const v = obj(row.verification)
  const a = v ? obj(v.assembly_arms) : null
  if (!a) return null
  return {
    gate: str(a.gate, 'passed'),
    quote_fidelity: num(a.quote_fidelity),
    scope_preservation: num(a.scope_preservation),
    attribution_equality: num(a.attribution_equality),
    coverage_completeness: num(a.coverage_completeness),
    spans: num(a.spans),
    failed_ordinals: arr(a.failed_ordinals)
      .map((x) => num(x))
      .filter((x): x is number => x !== null),
  }
}

/** Project `data.data.assessment` off a `world_assessment` row. */
export function projectAssessment(row: ReadFindingRow): AssessmentPayload | null {
  const a = obj(innerData(row).assessment)
  if (!a) return null
  if (str(a.schema) !== ASSESSMENT_SCHEMA) return null
  const spineId = str(a.spine_id)
  if (spineId === '') return null
  const badge = obj(a.badge)
  const ext = badge ? obj(badge.external_accuracy) : null
  const spineBadge = obj(a.spine_badge)
  const fid = obj(a.fidelity)
  const lt = obj(a.lead_test)
  return {
    schema: ASSESSMENT_SCHEMA,
    spine_id: spineId,
    spine_schema: str(a.spine_schema, ASSEMBLY_SCHEMA),
    lead_test: lt
      ? {
          key: str(lt.key, 'cited_mass.v1'),
          top_share: num(lt.top_share) ?? 0,
          ratio_12: num(lt.ratio_12) ?? 0,
          bar_share: num(lt.bar_share) ?? 0.15,
          bar_ratio: num(lt.bar_ratio) ?? 1.5,
          earned: lt.earned === true,
          n_candidates: int(lt.n_candidates),
        }
      : null,
    markers: arr(a.markers)
      .map((m) => {
        const o = obj(m)
        const ordinal = o ? num(o.ordinal) : null
        return ordinal === null ? null : { ordinal, sentence_index: int(o!.sentence_index, -1) }
      })
      .filter((m): m is { ordinal: number; sentence_index: number } => m !== null),
    unsupported: arr(a.unsupported)
      .map((u) => {
        const o = obj(u)
        if (!o) return null
        const text = str(o.text)
        const start = num(o.char_start)
        const end = num(o.char_end)
        if (text === '' || start === null || end === null || end <= start) return null
        return {
          sentence_index: int(o.sentence_index, -1),
          char_start: start,
          char_end: end,
          text,
          class: str(o.class, 'fact'),
          detector: str(o.detector, 'deterministic'),
          note: str(o.note),
        }
      })
      .filter((u): u is UnsupportedMark => u !== null),
    fidelity: fid
      ? {
          checkable: int(fid.checkable),
          supported: int(fid.supported),
          score: num(fid.score) ?? 0,
        }
      : null,
    badge: badge
      ? {
          fidelity_to_spine: num(badge.fidelity_to_spine),
          fidelity_n: num(badge.fidelity_n),
          external_accuracy: ext
            ? {
                value: num(ext.value) ?? 0,
                n: int(ext.n),
                round: str(ext.round),
                as_of: str(ext.as_of),
                population: str(ext.population),
                lineage: str(ext.lineage),
              }
            : null,
          external_accuracy_note: strOrNull(badge.external_accuracy_note),
          standing_accuracy: projectStanding(badge.standing_accuracy),
          standing_state: str(badge.standing_state, 'unmeasured'),
        }
      : null,
    spine_badge: spineBadge
      ? {
          standing_accuracy: projectStanding(spineBadge.standing_accuracy),
          standing_note: strOrNull(spineBadge.standing_note),
        }
      : null,
  }
}

/**
 * W-3 — the Assessment's citation list, projected.
 *
 * Reads the double-nested live shape first (`data.data.citations`, where
 * `FindingPayload.data` lands) and the flat shape as the fallback, the same
 * defensive order `citationsModel.extractCitations` uses. An entry with no
 * ordinal is dropped: the ordinal is the ONLY thing that distinguishes one
 * fenced citation from another, so an entry without one resolves to nothing a
 * reader can use.
 */
export function projectAssessmentCitations(row: ReadFindingRow): AssessmentCitation[] {
  const inner = innerData(row)
  const raw = Array.isArray(inner.citations) ? inner.citations : readPayload(row).citations
  const out: AssessmentCitation[] = []
  for (const entry of arr(raw)) {
    const o = obj(entry)
    if (!o) continue
    const ordinal = num(o.ordinal)
    const refId = str(o.ref_id)
    if (ordinal === null || refId === '') continue
    const sb = obj(o.spine_block)
    out.push({
      marker: str(o.marker) || `[[ref:${ordinal}]]`,
      ordinal,
      ref_id: refId,
      ref_kind: str(o.ref_kind, 'finding'),
      source: str(o.source),
      title: str(o.title),
      spine_block: sb
        ? {
            finding_id: strOrNull(sb.finding_id),
            desk: strOrNull(sb.desk),
            target_id: strOrNull(sb.target_id),
          }
        : null,
      tier: str(o.tier) === 'periphery' ? 'periphery' : null,
      effective_confidence: num(o.effective_confidence),
    })
  }
  return out
}

/**
 * W-3 — resolve the fenced citations into their chain against the spine.
 *
 * `spine` is the `assembly.v1` payload of the row the Assessment was written
 * from (`projectAssembly` over the spine row). Pass `null` and every chain
 * still carries hop 1 and whatever hop 2 the citation itself stamped — a
 * reader that could not load the spine shows a shorter chain, never a wrong
 * one.
 */
export function resolveAssessmentCitations(
  citations: AssessmentCitation[],
  spine: AssemblyPayload | null,
  spineTitle = '',
): AssessmentCitationChain[] {
  const blocks = new Map<number, AssemblyBlock>()
  for (const b of spine?.blocks ?? []) blocks.set(b.ordinal, b)
  return citations.map((c) => {
    const block = blocks.get(c.ordinal) ?? null
    return {
      marker: c.marker,
      ordinal: c.ordinal,
      spine_id: c.ref_id,
      spine_title: spineTitle || c.source,
      block,
      // The citation's own stamp wins over the spine's copy of the same fact:
      // it is what the producer recorded at write time, and it survives a
      // spine that has since been superseded.
      block_finding_id: c.spine_block?.finding_id ?? block?.finding_id ?? null,
      block_desk: c.spine_block?.desk ?? block?.desk ?? null,
      block_target_id: c.spine_block?.target_id ?? block?.target_id ?? null,
      signals: block?.signals ?? [],
      in_spine: block !== null,
    }
  })
}

/** Project one `standing_accuracy` block; `null` in, `null` out. */
export function projectStanding(raw: unknown): StandingAccuracy | null {
  const o = obj(raw)
  if (!o) return null
  return {
    value: num(o.value),
    state: str(o.state, 'unmeasured'),
    n_decided: int(o.n_decided),
    n_searched: int(o.n_searched),
    decided_rate: num(o.decided_rate),
    window_days: int(o.window_days, 7),
    population: str(o.population),
    grader_family: strOrNull(o.grader_family),
    instrument_limited: o.instrument_limited === true,
    sample_fraction: num(o.sample_fraction),
    state_note: strOrNull(o.state_note),
  }
}

/**
 * The reader's sentence for a standing number — W-7's whole contract in one
 * function: it NEVER returns a bare number.
 *
 * `null` / `unmeasured` -> "not graded against the world yet".
 * `instrument_limited`  -> the graders-disagreed sentence, and NO figure.
 * `sampled`             -> the rate WITH the fraction it covers.
 * `measured`            -> the rate with its n and its decided rate.
 *
 * The server composes `state_note` for the first three; this falls back to its
 * own wording only if a note is missing, so the reader and the API cannot drift
 * into two different sentences for the same state.
 */
export function standingSentence(
  standing: StandingAccuracy | null | undefined,
  what: string,
): string {
  // instrument_limited is checked FIRST, before the null-value branch: a number
  // withheld because two grader families disagreed also carries value===null,
  // but it is a DIFFERENT fact from "not graded yet" and must not read as one.
  // The server nulls `value` in both states, so order is the only thing that
  // keeps them apart on the reader's side.
  if (standing && (standing.state === 'instrument_limited' || standing.instrument_limited)) {
    return (
      standing.state_note ??
      'the graders did not agree with each other often enough this week for this number to be reported'
    )
  }
  if (!standing || standing.state === 'unmeasured' || standing.value === null) {
    return (
      standing?.state_note ??
      `${what} is not graded against the world yet — the standing loop has not decided enough claims to report a rate`
    )
  }
  const rate =
    standing.decided_rate === null ? '' : `, decided rate ${standing.decided_rate.toFixed(2)}`
  const window = `over ${standing.window_days} days`
  if (standing.state === 'sampled') {
    const pct =
      standing.sample_fraction === null ? '' : ` (${(standing.sample_fraction * 100).toFixed(0)}% sample)`
    return `${what} ${standing.value.toFixed(2)} on ${standing.n_decided} decided claims${rate} ${window}${pct}`
  }
  return `${what} ${standing.value.toFixed(2)} on ${standing.n_decided} decided claims${rate} ${window}`
}

// ── derived reader facts ─────────────────────────────────────────────────────

/**
 * The RECORD's badge (§2.3) — computed here, not read from a producer field.
 *
 * Every number below is already on the page or already on the row; computing
 * them means the badge cannot disagree with the blocks beneath it, which is the
 * failure the spec names in one line: "a badge that can read 1.00 on a page
 * containing a fact its own desk denies is not a badge."
 *
 * The record's badge deliberately carries NO faithfulness score.
 */
export interface RecordBadge {
  quoteFidelity: number | null
  coverageCompleteness: number | null
  dropCount: number
  blocks: number
  gate: 'passed' | 'failed' | 'unmeasured'
}

export function recordBadge(a: AssemblyPayload, arms: AssemblyArms | null): RecordBadge {
  const drops = a.drops?.counts
  const dropCount =
    drops === undefined
      ? 0
      : drops.shown_not_carried + drops.not_selected + drops.trimmed + drops.below_floor
  let coverage = arms?.coverage_completeness ?? null
  if (coverage === null && a.coverage.length > 0) {
    const accounted = a.coverage.filter((c) => c.status !== '').length
    coverage = accounted / a.coverage.length
  }
  return {
    quoteFidelity: arms?.quote_fidelity ?? null,
    coverageCompleteness: coverage,
    dropCount,
    blocks: a.blocks.length,
    gate: arms === null ? 'unmeasured' : arms.gate === 'failed' ? 'failed' : 'passed',
  }
}

/** True when this block's quotation could not be confirmed (§3.6 READ time). */
export function blockRefused(arms: AssemblyArms | null, ordinal: number): boolean {
  return arms !== null && arms.gate === 'failed' && arms.failed_ordinals.includes(ordinal)
}

/**
 * Where each tension is rendered: after the LATER of its two blocks, so the
 * reader has already met both halves. A `carried_vs_dropped` anchors on its one
 * carried half. Tensions naming no on-page block fall to the record's foot.
 */
export function tensionsByAnchor(a: AssemblyPayload): Map<number, AssemblyTension[]> {
  const out = new Map<number, AssemblyTension[]>()
  const shown = new Set(a.blocks.map((b) => b.ordinal))
  for (const t of a.tensions) {
    const ords = [t.a?.ordinal, t.b?.ordinal].filter(
      (o): o is number => typeof o === 'number' && shown.has(o),
    )
    if (ords.length === 0) continue
    const anchor = Math.max(...ords)
    const list = out.get(anchor) ?? []
    list.push(t)
    out.set(anchor, list)
  }
  return out
}

/** Tensions whose halves are both off the page — rendered at the record's foot. */
export function orphanTensions(a: AssemblyPayload): AssemblyTension[] {
  const shown = new Set(a.blocks.map((b) => b.ordinal))
  return a.tensions.filter(
    (t) =>
      ![t.a?.ordinal, t.b?.ordinal].some((o) => typeof o === 'number' && shown.has(o)),
  )
}

/** The signals cited by MORE THAN ONE desk head — the correlation grain (§D-4). */
export interface SharedSignal {
  signal: AssemblySignal
  /** Every on-page block that cites it, plus the off-page desks it names. */
  citedByOrdinals: number[]
  sharedWith: { desk: string; target_id: string | null }[]
}

export function sharedSignals(a: AssemblyPayload): SharedSignal[] {
  const bySignal = new Map<string, SharedSignal>()
  for (const b of a.blocks) {
    for (const s of b.signals) {
      if (!s.signal_id) continue
      const found = bySignal.get(s.signal_id)
      if (found) {
        if (!found.citedByOrdinals.includes(b.ordinal)) found.citedByOrdinals.push(b.ordinal)
      } else {
        bySignal.set(s.signal_id, {
          signal: s,
          citedByOrdinals: [b.ordinal],
          sharedWith: s.also_cited_by,
        })
      }
    }
  }
  return [...bySignal.values()]
    .filter((s) => s.citedByOrdinals.length > 1 || s.sharedWith.length > 0)
    .sort(
      (x, y) =>
        y.citedByOrdinals.length + y.sharedWith.length -
          (x.citedByOrdinals.length + x.sharedWith.length) ||
        (y.signal.salience_magnitude ?? 0) - (x.signal.salience_magnitude ?? 0),
    )
}

/** Aggregate counts for the evidence map's opening sentence. */
export interface EvidenceTotals {
  signals: number
  distinctSignals: number
  sources: number
  shared: number
  scored: number
}

export function evidenceTotals(a: AssemblyPayload): EvidenceTotals {
  const ids = new Set<string>()
  const sources = new Set<string>()
  let signals = 0
  let scored = 0
  for (const b of a.blocks) {
    for (const s of b.signals) {
      signals += 1
      if (s.signal_id) ids.add(s.signal_id)
      if (s.source_id) sources.add(s.source_id)
      if (s.salience_magnitude !== null) scored += 1
    }
  }
  return {
    signals,
    distinctSignals: ids.size,
    sources: sources.size,
    shared: sharedSignals(a).length,
    scored,
  }
}

/**
 * The history row's label (VOICE §4.6.1). The list used to differentiate 60 runs
 * by a truncated title alone — a column of near-identical sentences — because
 * `severity` was NULL on 115/115 world rows and nothing else was rendered. The
 * assembly sets `severity` (§1.1) and carries the counts, so the row can finally
 * say which read it is.
 */
export interface HistoryLabel {
  lead: string
  blocks: number
  dropped: number
}

export function historyLabel(a: AssemblyPayload | null): HistoryLabel | null {
  if (!a) return null
  const drops = a.drops?.counts
  const dropped =
    drops === undefined ? 0 : drops.shown_not_carried + drops.not_selected + drops.trimmed
  const ordinals = a.lead?.block_ordinals ?? []
  const named = ordinals
    .map((o) => a.blocks.find((b) => b.ordinal === o))
    .filter((b): b is AssemblyBlock => b !== undefined)
    .map((b) => `${b.desk} / ${b.target_name ?? b.target_id ?? '—'}`)
  const lead =
    named.length > 0
      ? named.join(' · ')
      : a.lead?.kind === 'none'
        ? 'no single lead'
        : '—'
  return { lead, blocks: a.blocks.length, dropped }
}

/** The one-line reason the lead position was, or was not, earned (§1.5.3). */
export function leadReason(lead: AssemblyLead | null): string | null {
  const t = lead?.test
  if (!lead || !t) return null
  const bars = `bars ${t.bar_share.toFixed(2)} / ${t.bar_ratio.toFixed(2)}`
  const measured = `top-share ${t.top_share.toFixed(3)}, ratio ${t.ratio_12.toFixed(2)}`
  if (lead.kind === 'earned_single') {
    return `concentration earned this cycle: ${measured} against ${bars}, over ${t.n_candidates} candidates`
  }
  if (lead.kind === 'co_leads') {
    return `concentration not earned: ${measured} against ${bars} — ${lead.block_ordinals.length} co-leads inside the band, none crowned`
  }
  return `no single lead this cycle: ${measured} against ${bars}, and no candidate band formed`
}
