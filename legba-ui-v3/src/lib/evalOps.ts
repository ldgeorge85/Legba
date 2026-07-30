/**
 * UI-5 (Tiers E+F) — pure data-layer for the eval + ops panels.
 *
 * All grouping / trend / diff / lag-classification logic lives here so it is
 * unit-tested without a DOM (mirrors `@/lib/findingsViews`). The panels
 * (EvalScorecard / StreamLag / GovernorEvents / OptimizerDiff / AuditChain /
 * ActorHealth) stay thin render shells over these helpers.
 */

// ===========================================================================
// Eval / critic scorecard (system.eval)
// ===========================================================================

/**
 * One critic-scored output for an analyst. Mirrors the substrate-reads shape
 * `GET /api/v1/v3/eval/scorecard?analyst_id=&since=&limit=` returns: a row per
 * critic judgement with the per-axis rubric breakdown + an overall.
 */
export interface ScorecardRow {
  id: string
  analyst_id: string
  analyst_version: string | null
  /** per-rubric-axis scores in [0,1] (e.g. {calibration: 0.8, evidence: 0.6}) */
  scores: Record<string, number>
  overall_score: number
  /** optional ground-truth backtest accuracy in [0,1] when present */
  ground_truth_accuracy?: number | null
  produced_at: string
}

export interface AnalystScorecard {
  analyst_id: string
  /** rows newest-first */
  rows: ScorecardRow[]
  latest_overall: number
  /** mean overall across the window */
  mean_overall: number
  /** latest - earliest overall in the window (trend direction) */
  trend_delta: number
  /** mean per-axis scores across the window */
  axis_means: Record<string, number>
  /** latest ground-truth backtest accuracy if any row carries it */
  latest_accuracy: number | null
}

/** Group raw scorecard rows into per-analyst summaries with trend stats. */
export function buildScorecards(rows: ScorecardRow[]): AnalystScorecard[] {
  const byAnalyst = new Map<string, ScorecardRow[]>()
  for (const r of rows) {
    const list = byAnalyst.get(r.analyst_id) ?? []
    list.push(r)
    byAnalyst.set(r.analyst_id, list)
  }
  const out: AnalystScorecard[] = []
  for (const [analyst_id, list] of byAnalyst) {
    // newest-first for display; oldest needed for trend.
    const sorted = [...list].sort(
      (a, b) => Date.parse(b.produced_at) - Date.parse(a.produced_at),
    )
    const newest = sorted[0]
    const oldest = sorted[sorted.length - 1]
    const mean_overall =
      sorted.reduce((a, r) => a + r.overall_score, 0) / (sorted.length || 1)

    // per-axis means over every row that carries the axis.
    const axisSum: Record<string, number> = {}
    const axisN: Record<string, number> = {}
    for (const r of sorted) {
      for (const [axis, v] of Object.entries(r.scores)) {
        axisSum[axis] = (axisSum[axis] ?? 0) + v
        axisN[axis] = (axisN[axis] ?? 0) + 1
      }
    }
    const axis_means: Record<string, number> = {}
    for (const axis of Object.keys(axisSum)) {
      axis_means[axis] = axisSum[axis] / axisN[axis]
    }

    const latest_accuracy =
      sorted.find((r) => typeof r.ground_truth_accuracy === 'number')
        ?.ground_truth_accuracy ?? null

    out.push({
      analyst_id,
      rows: sorted,
      latest_overall: newest.overall_score,
      mean_overall,
      trend_delta: newest.overall_score - oldest.overall_score,
      axis_means,
      latest_accuracy,
    })
  }
  // worst-performing analysts first — they need attention.
  return out.sort((a, b) => a.latest_overall - b.latest_overall)
}

/** Chronological (oldest→newest) overall-score series for a trend chart. */
export function critScoreTrend(
  rows: ScorecardRow[],
): Array<{ t: number; label: string; overall: number }> {
  return [...rows]
    .sort((a, b) => Date.parse(a.produced_at) - Date.parse(b.produced_at))
    .map((r) => ({
      t: Date.parse(r.produced_at),
      label: new Date(r.produced_at).toLocaleDateString(),
      overall: r.overall_score,
    }))
}

export type ScoreBand = 'good' | 'warn' | 'bad'
export function scoreBand(score: number): ScoreBand {
  if (score >= 0.8) return 'good'
  if (score >= 0.5) return 'warn'
  return 'bad'
}

// ===========================================================================
// Calibration / forecast-pilot skill scoreboard (system.eval — the honest top)
// ===========================================================================

/**
 * The platform's honest skill scoreboard. Mirrors the registry route
 * `GET /api/v1/v3/eval/calibration` (`CalibrationScoreboard`), itself the exact
 * reduction of `SubstrateQueryPort.get_calibration`. `brier`/`brier_exogenous`
 * is the EXOGENOUS-only headline; the acute-forecast pilot lives in its own
 * keys and is never pooled in. `available` is false before any calibration
 * finding exists (a distinct "no pilot yet" state, not a failed pilot).
 */
export interface CalibrationScoreboard {
  available: boolean
  produced_at: string | null
  brier: number | null
  brier_exogenous: number | null
  exogenous_sample_size: number | null
  sample_size: number | null
  insufficient_exogenous: boolean | null
  self_consistency_only: boolean | null
  brier_forecast_acute: number | null
  brier_skill_score: number | null
  forecast_acute_sample_size: number | null
  forecast_acute_ready: boolean
  forecast_acute_degenerate: boolean
  forecast_acute_status: string | null
  forecast_unproven: boolean
  calibration_thin: boolean
  refs: string[]
  /** P2-3 — the band-calibration harness section (additive; `null` only when
   *  the section read itself failed — see {@link BandCalibrationSection}). */
  band_calibration: BandCalibrationSection | null
}

// ===========================================================================
// Band-calibration harness (system.eval — P2-3, NOT a Brier score)
// ===========================================================================

/**
 * One horizon's (14d / 28d) graded-outcome block. Mirrors
 * `band_calibration_tracker.summarize_claims`'s `_rate_block` +
 * `_block` shape and the registry's `BandCalibrationSection.horizons[*]`.
 *
 * `outcomes` is the raw outcome-count map (`held` / `worsened` / `improved` /
 * `reverted` / `insufficient` / `unresolvable` — whichever occurred; absent
 * keys never occurred, not zero-by-omission). `persistence_rate` /
 * `reversal_rate` are `null` on a zero `scored` denominator — an honest empty
 * read, never a fabricated 0.0.
 */
export interface BandCalibrationHorizon {
  resolved: number
  open: number
  outcomes: Record<string, number>
  confirmed: number
  reverted: number
  scored: number
  excluded_insufficient: number
  excluded_unresolvable: number
  persistence_rate: number | null
  reversal_rate: number | null
}

/** One `by_direction[direction]` / `by_dimension[dimension]` slice — the same
 *  horizon blocks, plus the claim count they were computed over. */
export interface BandCalibrationSlice {
  claims: number
  [horizon: string]: BandCalibrationHorizon | number
}

/**
 * P2-3 — the band-persistence harness aggregate. Mirrors the registry route's
 * `BandCalibrationSection` (`GET /api/v1/v3/eval/calibration`'s
 * `band_calibration` key), itself the freshest `band_calibration_tracker`
 * finding's `data.data.band_calibration` block.
 *
 * HONESTY (the whole point of this section, per `no_brier` + `honesty_note`):
 * bands are ORDINAL risk categories, not probabilities. There is no Brier
 * score, Brier-skill score, or forecast-skill claim here or anywhere in this
 * harness — only persistence/reversal RATES with their sample sizes. UI copy
 * consuming this type must never call a rate a "Brier score".
 * `available` is false before the tracker's first finding exists (a distinct
 * "nothing graded yet" state, not a zero rate).
 */
export interface BandCalibrationSection {
  available: boolean
  produced_at: string | null
  claims_total: number | null
  resolution_spec: string | null
  horizons: Record<string, BandCalibrationHorizon>
  by_direction: Record<string, BandCalibrationSlice>
  by_dimension: Record<string, BandCalibrationSlice>
  no_brier: boolean
  honesty_note: string | null
  refs: string[]
}

/** Horizon display order — 14-day read before 28-day, then anything else. */
export const BAND_CALIBRATION_HORIZON_ORDER = ['14d', '28d'] as const

/** Order a `horizons` (or a `by_direction`/`by_dimension` slice's horizon
 *  keys) map for display: the two known horizons first, then any extras. */
export function orderedBandHorizons(
  horizons: Record<string, BandCalibrationHorizon>,
): Array<[string, BandCalibrationHorizon]> {
  const known = BAND_CALIBRATION_HORIZON_ORDER.filter((h) => h in horizons).map(
    (h) => [h, horizons[h]] as [string, BandCalibrationHorizon],
  )
  const extras = Object.keys(horizons)
    .filter((h) => !(BAND_CALIBRATION_HORIZON_ORDER as readonly string[]).includes(h))
    .sort()
    .map((h) => [h, horizons[h]] as [string, BandCalibrationHorizon])
  return [...known, ...extras]
}

/** A horizon rate as a percentage string, or the honest empty marker when the
 *  scored denominator is zero (never a fabricated 0%/100%). */
export function bandRateLabel(rate: number | null): string {
  return rate == null ? '— (no scored claims yet)' : `${Math.round(rate * 100)}%`
}

/** True when the section carries nothing graded yet — no tracker finding
 *  (`!available`), or a finding with zero claims logged so far. Drives the
 *  panel's empty/awaiting state. */
export function bandCalibrationEmpty(section: BandCalibrationSection | null | undefined): boolean {
  if (!section || !section.available) return true
  return !section.claims_total || section.claims_total <= 0
}

export type AcuteTag = 'ready' | 'accumulating' | 'degenerate'

export interface CalibrationBanner {
  /** true when no calibration finding has ever been computed (≠ insufficient-sample) */
  absent: boolean
  /** exogenous headline — a Brier `value` when sufficient, else the honest message */
  exogenous: { value: number | null; label: string; insufficient: boolean }
  /** acute-forecast leg — a `bss` number ONLY when ready && !degenerate && bss>0 */
  acute: { tag: AcuteTag; label: string; bss: number | null }
}

/** The n the acute pilot accumulates toward before a skill claim is admissible. */
export const ACUTE_TARGET_N = 30

/**
 * Compute every displayed string by gating on the SAME flags the route returns,
 * so a number can never leak past its honesty gate:
 *
 *  * EXOGENOUS Brier — absent → "no calibration finding computed yet"; else
 *    `insufficient_exogenous` (or a null brier) → the verbatim
 *    "INSUFFICIENT exogenous sample (n_exo=k/N)"; else the exogenous Brier.
 *  * ACUTE BSS — `forecast_acute_degenerate` → "degenerate — skill claim withheld"
 *    and NO number; else not ready → "accumulating (n=k/30)"; else ready &&
 *    !degenerate && bss>0 → the BSS. A ready-but-non-positive pilot still shows
 *    NO number (skill not yet earned).
 *
 * The reducer NEVER returns a bare positive BSS unless ready AND non-degenerate.
 */
export function calibrationBanner(
  cal: CalibrationScoreboard | null | undefined,
): CalibrationBanner {
  if (!cal || !cal.available) {
    // No calibration finding computed yet — distinct from insufficient-sample.
    return {
      absent: true,
      exogenous: {
        value: null,
        label: 'no calibration finding computed yet',
        insufficient: false,
      },
      acute: {
        tag: 'accumulating',
        label: 'no calibration finding computed yet',
        bss: null,
      },
    }
  }

  // EXOGENOUS headline. A thin sample OR a null brier → the verbatim honest
  // message; a number is shown ONLY when the exogenous sample is sufficient.
  const brierExo = cal.brier_exogenous ?? cal.brier
  // A null brier is always insufficient; the explicit `!= null` here also lets TS
  // narrow `brierExo` to a number in the sufficient branch.
  const exogenous =
    brierExo != null && cal.insufficient_exogenous !== true
      ? { value: brierExo, label: brierExo.toFixed(3), insufficient: false }
      : {
          value: null,
          label: `INSUFFICIENT exogenous sample (n_exo=${cal.exogenous_sample_size ?? 0}/${cal.sample_size ?? 0})`,
          insufficient: true,
        }

  // ACUTE-forecast BSS. Gate order matters: degenerate → withheld (no number)
  // BEFORE ready, so a degenerate pilot can never show a skill number.
  const bss = cal.brier_skill_score
  let acute: CalibrationBanner['acute']
  if (cal.forecast_acute_degenerate) {
    acute = { tag: 'degenerate', label: 'degenerate — skill claim withheld', bss: null }
  } else if (!cal.forecast_acute_ready) {
    acute = {
      tag: 'accumulating',
      label: `accumulating (n=${cal.forecast_acute_sample_size ?? 0}/${ACUTE_TARGET_N})`,
      bss: null,
    }
  } else if (typeof bss === 'number' && bss > 0) {
    acute = { tag: 'ready', label: `BSS ${bss.toFixed(3)}`, bss }
  } else {
    // Ready + non-degenerate but no POSITIVE skill earned — still withheld,
    // NO number (a non-positive BSS is not a skill claim).
    acute = { tag: 'accumulating', label: 'ready — no positive skill yet', bss: null }
  }

  return { absent: false, exogenous, acute }
}

// ===========================================================================
// NATS consumer-lag monitor (system.stream_lag)
// ===========================================================================

/**
 * One consumer's lag snapshot. Mirrors
 * `NatsStore.consumer_lag()` / `SubscriptionEngine.consumer_lag()` projected
 * over `GET /api/v1/v3/streams/consumer_lag` — one row per per-source /
 * per-target durable consumer.
 */
export interface ConsumerLagRow {
  stream: string
  durable: string
  /** the dimension this consumer fans out — source_id or target_id */
  scope_kind: 'source' | 'target' | string
  scope_id: string
  /** headline lag — messages on the stream not yet delivered */
  num_pending: number
  /** delivered but unacked */
  num_ack_pending: number
  num_redelivered: number
  num_waiting: number
  delivered_stream_seq: number | null
  ack_floor_stream_seq: number | null
}

export type LagSeverity = 'ok' | 'warn' | 'critical'

/**
 * Classify a consumer's health. Lag is the headline; redeliveries (poison
 * messages) and a large unacked backlog escalate independently.
 */
export function lagSeverity(
  row: Pick<ConsumerLagRow, 'num_pending' | 'num_redelivered' | 'num_ack_pending'>,
  thresholds: { warn: number; critical: number } = { warn: 100, critical: 1000 },
): LagSeverity {
  if (row.num_pending >= thresholds.critical || row.num_redelivered >= 25) {
    return 'critical'
  }
  if (
    row.num_pending >= thresholds.warn ||
    row.num_redelivered > 0 ||
    row.num_ack_pending >= thresholds.warn
  ) {
    return 'warn'
  }
  return 'ok'
}

/** Worst-lag-first, then by redelivered, for the monitor table. */
export function sortLag(rows: ConsumerLagRow[]): ConsumerLagRow[] {
  return [...rows].sort(
    (a, b) => b.num_pending - a.num_pending || b.num_redelivered - a.num_redelivered,
  )
}

// ===========================================================================
// Optimizer prompt-module diff (system.optimizer / optimizer.diff)
// ===========================================================================

/**
 * The candidate-vs-current prompt-module diff. Mirrors
 * `GET /api/v1/v3/optimizer/candidates/{id}/diff` — the candidate module text
 * and the currently-active module text, plus identity for the header.
 */
export interface PromptModuleDiff {
  candidate_id: string
  analyst_id: string
  /** path of the live/active module the candidate would replace */
  current_module_path: string
  candidate_module_path: string
  current_text: string
  candidate_text: string
  eval_score: number
  eval_score_delta: number
}

export type DiffOp = 'same' | 'add' | 'del'
export interface DiffLine {
  op: DiffOp
  /** line number in the current (old) text, null for added lines */
  oldNo: number | null
  /** line number in the candidate (new) text, null for deleted lines */
  newNo: number | null
  text: string
}

/**
 * Minimal line-level diff via LCS. Adequate for prompt-module review (these
 * are short, line-oriented texts); avoids a runtime diff dependency.
 */
export function diffLines(oldText: string, newText: string): DiffLine[] {
  const a = oldText.split('\n')
  const b = newText.split('\n')
  const n = a.length
  const m = b.length

  // LCS length table.
  const lcs: number[][] = Array.from({ length: n + 1 }, () =>
    new Array<number>(m + 1).fill(0),
  )
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] =
        a[i] === b[j]
          ? lcs[i + 1][j + 1] + 1
          : Math.max(lcs[i + 1][j], lcs[i][j + 1])
    }
  }

  const out: DiffLine[] = []
  let i = 0
  let j = 0
  let oldNo = 1
  let newNo = 1
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ op: 'same', oldNo: oldNo++, newNo: newNo++, text: a[i] })
      i++
      j++
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      out.push({ op: 'del', oldNo: oldNo++, newNo: null, text: a[i] })
      i++
    } else {
      out.push({ op: 'add', oldNo: null, newNo: newNo++, text: b[j] })
      j++
    }
  }
  while (i < n) out.push({ op: 'del', oldNo: oldNo++, newNo: null, text: a[i++] })
  while (j < m) out.push({ op: 'add', oldNo: null, newNo: newNo++, text: b[j++] })
  return out
}

export interface DiffStat {
  added: number
  deleted: number
  unchanged: number
}
export function diffStat(lines: DiffLine[]): DiffStat {
  let added = 0
  let deleted = 0
  let unchanged = 0
  for (const l of lines) {
    if (l.op === 'add') added++
    else if (l.op === 'del') deleted++
    else unchanged++
  }
  return { added, deleted, unchanged }
}

// ===========================================================================
// Governor events (system.governor)
// ===========================================================================

/**
 * One governor decision. Mirrors `GovernorEvent.to_payload()` (the DB row +
 * the NATS telemetry payload). Served by
 * `GET /api/v1/registry/governor_events?pack_id=&decision=&limit=` and tailed
 * live on the `governor.events.>` subject via the registry-events WS.
 */
export interface GovernorEventRow {
  pack_id: string
  decision: 'allow' | 'block'
  cause: string
  tool_name: string | null
  budget_account: string
  requested_by: string
  tenant_id: string
  cap_dimension: string | null
  cap_limit: number | null
  observed_value: number | null
  detail: string
  occurred_at: string
  /** synthesised client-side for keying / live-badging; not from the wire */
  _key?: string
  _live?: boolean
}

const GOVERNOR_TAIL_FILTER = 'governor.events.>'
export { GOVERNOR_TAIL_FILTER }

/**
 * Map a `governor.events.>` WS envelope payload into a row. Tolerates partial
 * payloads (telemetry can lag the schema). Returns null when unusable.
 */
export function mapGovernorEnvelope(
  payload: Record<string, unknown> | undefined,
): GovernorEventRow | null {
  if (!payload) return null
  const decision = payload.decision
  if (decision !== 'allow' && decision !== 'block') return null
  const occurred_at =
    typeof payload.occurred_at === 'string'
      ? payload.occurred_at
      : new Date().toISOString()
  const pack_id = typeof payload.pack_id === 'string' ? payload.pack_id : '(unknown)'
  const num = (k: string): number | null =>
    typeof payload[k] === 'number' ? (payload[k] as number) : null
  const str = (k: string, dflt = ''): string =>
    typeof payload[k] === 'string' ? (payload[k] as string) : dflt
  return {
    pack_id,
    decision,
    cause: str('cause', 'ok'),
    tool_name: typeof payload.tool_name === 'string' ? payload.tool_name : null,
    budget_account: str('budget_account', 'system'),
    requested_by: str('requested_by', 'system'),
    tenant_id: str('tenant_id', 'default'),
    cap_dimension:
      typeof payload.cap_dimension === 'string' ? payload.cap_dimension : null,
    cap_limit: num('cap_limit'),
    observed_value: num('observed_value'),
    detail: str('detail'),
    occurred_at,
    _key: `${pack_id}:${decision}:${occurred_at}:${str('tool_name')}`,
    _live: true,
  }
}

export interface GovernorSummary {
  total: number
  blocked: number
  allowed: number
  /** distinct packs that have at least one BLOCK */
  blocked_packs: string[]
  /** count of block events by cause */
  by_cause: Record<string, number>
}
export function summariseGovernor(rows: GovernorEventRow[]): GovernorSummary {
  let blocked = 0
  let allowed = 0
  const blockedPacks = new Set<string>()
  const byCause: Record<string, number> = {}
  for (const r of rows) {
    if (r.decision === 'block') {
      blocked++
      blockedPacks.add(r.pack_id)
      byCause[r.cause] = (byCause[r.cause] ?? 0) + 1
    } else {
      allowed++
    }
  }
  return {
    total: rows.length,
    blocked,
    allowed,
    blocked_packs: [...blockedPacks],
    by_cause: byCause,
  }
}

// ===========================================================================
// Audit-chain browser (system.audit)
// ===========================================================================

/**
 * One audit-chain entry. Mirrors `AuditEntryOut` from
 * `src/legba/data/registry/api.py`: each register/update/promote is signed
 * (Ed25519) and re-verified inline by the backend, exposing
 * `signature_verified` (true/false, or null in reader-only mode).
 */
export interface AuditEntryRow {
  id: string
  occurred_at: string
  actor_id: string
  actor_role: string
  namespace: string
  descriptor_id: string
  action: string
  from_version: string | null
  to_version: string | null
  change_summary: Record<string, unknown>
  signer_did: string
  signature_verified: boolean | null
}

export type VerifyStatus = 'verified' | 'failed' | 'unverifiable'
export function verifyStatus(entry: Pick<AuditEntryRow, 'signature_verified'>): VerifyStatus {
  if (entry.signature_verified === true) return 'verified'
  if (entry.signature_verified === false) return 'failed'
  return 'unverifiable'
}

export interface ChainHealth {
  total: number
  verified: number
  failed: number
  unverifiable: number
  /** true when every entry that *could* be checked passed */
  intact: boolean
}
export function chainHealth(entries: AuditEntryRow[]): ChainHealth {
  let verified = 0
  let failed = 0
  let unverifiable = 0
  for (const e of entries) {
    const s = verifyStatus(e)
    if (s === 'verified') verified++
    else if (s === 'failed') failed++
    else unverifiable++
  }
  return {
    total: entries.length,
    verified,
    failed,
    unverifiable,
    intact: failed === 0,
  }
}

// ===========================================================================
// Runtime actor health (system.runtime) — kind classification
// ===========================================================================

export const ACTOR_KINDS = [
  'all',
  'target',
  'analyst',
  'discovery',
  'source',
  'consult',
] as const
export type ActorKindFilter = (typeof ACTOR_KINDS)[number]

/** Roll up actor rows by kind + lifecycle for the health summary header. */
export function actorRollup(
  rows: Array<{ actor_kind: string; lifecycle: string; error_count: number }>,
): {
  byKind: Record<string, number>
  byLifecycle: Record<string, number>
  errors: number
  stale: number
} {
  const byKind: Record<string, number> = {}
  const byLifecycle: Record<string, number> = {}
  let errors = 0
  for (const r of rows) {
    byKind[r.actor_kind] = (byKind[r.actor_kind] ?? 0) + 1
    byLifecycle[r.lifecycle] = (byLifecycle[r.lifecycle] ?? 0) + 1
    errors += r.error_count
  }
  return { byKind, byLifecycle, errors, stale: byLifecycle['error'] ?? 0 }
}

// ===========================================================================
// Country banded scorecard (system.eval — the honest-top drillable card, P4-T3/T5)
// ===========================================================================

/**
 * The per-dimension eval fold (P4-T5). Each unit dimension carries the latest
 * per-unit faithfulness + correctness from `unit_correctness_scorer`, honest-null
 * when the scorer never measured it. `faithfulness_flagged` is true when the
 * aggregate faithfulness sits below the banding `faith_floor` — a visible mark on
 * the basis card even when the per-claim critic score passed.
 */
export interface DimensionEval {
  faithfulness: number | null
  correctness_vs_reference: number | null
  n_labeled: number
  faithfulness_flagged: boolean
}

/**
 * One dimension's banded verdict — the exact `data.bands.dimensions[unit]` shape
 * the T1 banding emits (T5-extended). `basis` NAMES the real verified
 * `analyst_outputs.id`s the band rests on; it is `[]` (and never a synthesised
 * id) iff the dimension is insufficient-evidence.
 */
export interface DimensionBand {
  band: string
  /** real analyst_outputs.id sub-claims — [] iff insufficient-evidence. */
  basis: string[]
  severity_tag: string | null
  effective_confidence: number | null
  confidence: number | null
  /** the per-claim folded faithfulness (banding's own gather). */
  critic_score: number | null
  damped: boolean
  reason: string
  produced_at: string | null
  eval: DimensionEval
}

/** The P3 composition aggregate node — the country-level verified composition. */
export interface CompositionNode {
  present: boolean
  basis: string[]
  effective_confidence?: number | null
  produced_at?: string | null
}

/**
 * The persisted banded scorecard for one G20 country. Mirrors the registry
 * route `GET /api/v1/v3/eval/country_scorecard?target_id=` (`CountryScorecard`),
 * itself a straight projection of the persisted `data.bands` (kind=scorecard
 * row). One honest card per active country — a country with no verified claims
 * still returns a row whose dimensions are ALL insufficient-evidence.
 */
export interface CountryScorecard {
  target_id: string
  id: string
  produced_at: string
  generated_at: string | null
  floors: Record<string, number>
  dimensions: Record<string, DimensionBand>
  composition: CompositionNode
}

/** Coarse severity tone for a band pill. Insufficient is its own honest tone. */
export type BandTone = 'good' | 'watch' | 'elevated' | 'high' | 'critical' | 'insufficient'

/** True iff the dimension has no qualifying verified claim (an honest state). */
export function isInsufficient(b: Pick<DimensionBand, 'band'>): boolean {
  return b.band === 'insufficient-evidence'
}

/**
 * Map a band label to a coarse tone. Insufficient-evidence is a first-class
 * honest tone (never colored as a severity). Unknown labels fall back to
 * 'watch' rather than inventing a severity.
 */
export function bandTone(band: string): BandTone {
  switch (band) {
    case 'insufficient-evidence':
      return 'insufficient'
    case 'critical':
      return 'critical'
    case 'high':
    case 'severe':
      return 'high'
    case 'elevated':
      return 'elevated'
    case 'good':
    case 'clear':
    case 'nominal':
    case 'low':
    case 'stable':
      return 'good'
    case 'watch':
    case 'moderate':
      return 'watch'
    default:
      return 'watch'
  }
}

/**
 * Human string for WHY a band is insufficient — the machine `reason` rendered
 * for an operator. Distinguishes the honest states (no finding yet / verify
 * never ran / below floor / excluded for low faithfulness / no severity).
 */
export function insufficientLabel(reason: string | null | undefined): string {
  switch (reason) {
    case 'no-finding':
      return 'no unit finding yet'
    case 'verify-failed':
      return 'faithfulness verify never ran'
    case 'below-floor':
      return 'below confidence floor'
    case 'low-faithfulness':
      return 'excluded: low faithfulness'
    case 'no-severity-tag':
      return 'no severity emitted'
    default:
      return reason ? `insufficient (${reason})` : 'insufficient'
  }
}

/**
 * The per-dimension eval badge — the honest idiom (mirrors labels_api
 * `_compose_badge`): a measured "faithfulness X | correctness Y (n=k)" ONLY when
 * a number is present, else the verbatim "unmeasured". Never invents a number.
 */
export function evalBadge(ev: DimensionEval | null | undefined): string {
  if (!ev) return 'unmeasured'
  const parts: string[] = []
  if (typeof ev.faithfulness === 'number') {
    parts.push(`faithfulness ${ev.faithfulness.toFixed(2)}`)
  }
  if (typeof ev.correctness_vs_reference === 'number') {
    parts.push(`correctness ${ev.correctness_vs_reference.toFixed(2)}`)
  }
  if (parts.length === 0) return 'unmeasured'
  return `${parts.join(' | ')} (n=${ev.n_labeled ?? 0})`
}

/** Relative-time formatter shared by the ops panels. */
export function relTime(ts: string | null): string {
  if (!ts) return 'never'
  const d = new Date(ts).getTime()
  if (!Number.isFinite(d)) return 'never'
  const sec = Math.max(0, (Date.now() - d) / 1000)
  if (sec < 60) return `${sec.toFixed(0)}s ago`
  if (sec < 3600) return `${(sec / 60).toFixed(0)}m ago`
  if (sec < 86400) return `${(sec / 3600).toFixed(1)}h ago`
  return `${(sec / 86400).toFixed(1)}d ago`
}
