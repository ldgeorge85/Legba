/**
 * Pure model for the `system.production_gauge` panel — the whole-engine
 * "did each loop produce what its own descriptor and history promised" read.
 *
 * All grouping, ordering, ratio reading, quiet-reason wording and summary-line
 * construction live here so they are unit-tested without a DOM (the
 * `@/lib/evalOps` precedent). The panel is a render shell over these functions.
 *
 * THE HONESTY CONTRACT these helpers exist to hold — the server enforces it and
 * this layer must not undo it:
 *
 *   1. `ungauged` is NEVER folded into `ok`. "We cannot say" and "it is fine"
 *      are different statements, so nothing here ever produces a single health
 *      percentage: `gaugeSummaryLine` and `severityBuckets` report gauged and
 *      ungauged as separate numbers, always.
 *   2. `ratio` is null EXACTLY when `state` is `ungauged`. `readRatio` is a
 *      discriminated union for that reason — an unmeasured row has no `ratio`
 *      field at all, so it CANNOT be rendered as 0.0 or as an empty bar. What
 *      it carries instead is the `quiet_reason` that explains the silence.
 *   3. `totals` is computed server-side over the FULL read BEFORE any filter.
 *      `classCounts` / `groupCounts` read `totals.by_class`; NOTHING in this
 *      module derives a total from a `loops` array, and `totalsCaption` says
 *      out loud that the numbers are whole-engine when a filter is on.
 *   4. `measured: false` means the read FAILED and degraded to an honest empty
 *      payload at HTTP 200. `gaugeReadState` separates that from the genuinely
 *      quiet engine (`measured: true`, zero loops) so a failed read can never
 *      render as an all-clear.
 *   5. `pages` is the alert plane's own predicate. `pagingNote` explains a row
 *      against `alert_min_severity` FROM THE PAYLOAD — this module hardcodes no
 *      threshold, so the panel and the operator's phone cannot drift apart.
 *
 * Mirrors `legba.data.registry.production_gauge` (the vocabulary), its
 * `_api`, `_integrity`, `_metering` and `_staleness` companions (the loop
 * classes and quiet reasons).
 */

import { ApiError } from '@/lib/api'
import type {
  ProductionGaugeResponse,
  ProductionGaugeRow,
  ProductionGaugeTotals,
} from '@/lib/api'

// ---------------------------------------------------------------------------
// Severity — the ladder shared with the alert plane.
// ---------------------------------------------------------------------------

/** `production_gauge.SEVERITY_RANK`, verbatim. */
export const SEVERITY_RANK: Record<string, number> = {
  info: 0,
  low: 1,
  medium: 2,
  high: 3,
  critical: 4,
}

/** Worst-first, for chip rows and sorts. */
export const SEVERITY_DESC: readonly string[] = ['critical', 'high', 'medium', 'low', 'info']

/** Rank of a severity; `-1` for a severity this UI does not know (so an
 *  unrecognised one sorts LAST rather than silently ranking as `info`). */
export function severityRank(severity: string): number {
  const r = SEVERITY_RANK[severity]
  return r == null ? -1 : r
}

/**
 * Does `severity` clear the alert floor? The floor comes from the payload's
 * `alert_min_severity` — never a constant in this file.
 *
 * This is an EXPLANATION of `row.pages`, not a replacement for it: the server's
 * flag is the authority and the panel renders that flag.
 */
export function meetsAlertFloor(severity: string, floor: string): boolean {
  const s = severityRank(severity)
  const f = severityRank(floor)
  if (s < 0 || f < 0) return false
  return s >= f
}

// ---------------------------------------------------------------------------
// Loop classes — four production loops plus the six bricks.
// ---------------------------------------------------------------------------

export type GaugeGroupId = 'production' | 'integrity' | 'metering' | 'staleness' | 'other'

/**
 * Which brick a loop class belongs to. The bricks arrive in the SAME flat
 * `loops` array as the production loops, distinguished only by `loop_class`,
 * so this map is what makes them visible as separate instruments.
 */
export const LOOP_CLASS_GROUP: Record<string, GaugeGroupId> = {
  analyst_cadence: 'production',
  analyst_production: 'production',
  source_production: 'production',
  backlog_drain: 'production',
  judge_availability: 'integrity',
  descriptor_prompt_drift: 'integrity',
  descriptor_state_drift: 'integrity',
  llm_latency: 'metering',
  llm_daily_burn: 'metering',
  desk_head_staleness: 'staleness',
}

/** Reading order: the ordinary loops first, then each brick family. */
export const GROUP_ORDER: readonly GaugeGroupId[] = [
  'production',
  'integrity',
  'metering',
  'staleness',
  'other',
]

export const GROUP_META: Record<GaugeGroupId, { label: string; blurb: string }> = {
  production: {
    label: 'Production loops',
    blurb: 'did it produce what its own cron and trailing rate promised',
  },
  integrity: {
    label: 'Integrity bricks',
    blurb: 'is what it produces still what we think it is',
  },
  metering: {
    label: 'Metering bricks',
    blurb: 'can the plane it runs on afford it — latency against the timeout, dollars against the ceiling',
  },
  staleness: {
    label: 'Staleness bricks',
    blurb: 'was what it read still current when it read it',
  },
  other: {
    label: 'Unrecognised loop classes',
    blurb: 'the server gauges these and this panel has no grouping for them yet — shown verbatim rather than folded into a family that would misdescribe them',
  },
}

/** Every loop class this UI knows, in reading order. Drives the class filter. */
export const LOOP_CLASSES: readonly string[] = Object.keys(LOOP_CLASS_GROUP).sort(
  (a, b) =>
    GROUP_ORDER.indexOf(LOOP_CLASS_GROUP[a]) - GROUP_ORDER.indexOf(LOOP_CLASS_GROUP[b]),
)

export function loopClassGroup(loopClass: string): GaugeGroupId {
  return LOOP_CLASS_GROUP[loopClass] ?? 'other'
}

/** A brick is anything that is not one of the four ordinary production loops. */
export function isBrick(loopClass: string): boolean {
  return loopClassGroup(loopClass) !== 'production'
}

const LOOP_CLASS_LABEL: Record<string, string> = {
  analyst_cadence: 'analyst cadence',
  analyst_production: 'analyst output',
  source_production: 'source signals',
  backlog_drain: 'backlog drain',
  judge_availability: 'judge availability',
  descriptor_prompt_drift: 'prompt drift',
  descriptor_state_drift: 'state drift',
  llm_latency: 'LLM latency',
  llm_daily_burn: 'LLM daily burn',
  desk_head_staleness: 'desk-head staleness',
}

/** Human label for a class; an unknown class renders its raw id, never a guess. */
export function loopClassLabel(loopClass: string): string {
  return LOOP_CLASS_LABEL[loopClass] ?? loopClass
}

// ---------------------------------------------------------------------------
// Ordering.
// ---------------------------------------------------------------------------

/**
 * `deficit` first, then `ungauged`, then `ok`.
 *
 * `ungauged` sits ABOVE `ok` on purpose: an engine that cannot say is a thing an
 * operator must look at, and sorting it below the healthy rows would bury it.
 */
export const STATE_ORDER: Record<string, number> = { deficit: 0, ungauged: 1, ok: 2 }

function stateRank(state: string): number {
  const r = STATE_ORDER[state]
  return r == null ? 3 : r
}

/** Stable key for a row. `loop_id` alone is NOT unique — the same analyst id
 *  appears under both `analyst_cadence` and `analyst_production`. */
export function rowKey(row: ProductionGaugeRow): string {
  return `${row.loop_class}:${row.loop_id}`
}

/** Worst-first: paging, then state, then severity, then ratio, then id. */
export function sortGaugeRows(rows: readonly ProductionGaugeRow[]): ProductionGaugeRow[] {
  return [...rows].sort((a, b) => {
    if (a.pages !== b.pages) return a.pages ? -1 : 1
    const st = stateRank(a.state) - stateRank(b.state)
    if (st !== 0) return st
    const sev = severityRank(b.severity) - severityRank(a.severity)
    if (sev !== 0) return sev
    // An ungauged row has no ratio; it must not sort as though it had 0.0, so
    // it falls through to the id tiebreak instead.
    const ar = a.ratio
    const br = b.ratio
    if (ar != null && br != null && ar !== br) return br - ar
    if (ar != null && br == null) return -1
    if (ar == null && br != null) return 1
    return rowKey(a).localeCompare(rowKey(b))
  })
}

export interface GaugeGroup {
  id: GaugeGroupId
  label: string
  blurb: string
  /** The rows of this group that survived the current filter, worst-first. */
  rows: ProductionGaugeRow[]
  /** Loop classes actually present in `rows`, in reading order. */
  classes: string[]
}

/** Split a flat `loops` array into its visible families. Empty groups drop out. */
export function groupGaugeRows(rows: readonly ProductionGaugeRow[]): GaugeGroup[] {
  const byGroup = new Map<GaugeGroupId, ProductionGaugeRow[]>()
  for (const r of rows) {
    const g = loopClassGroup(r.loop_class)
    const list = byGroup.get(g) ?? []
    list.push(r)
    byGroup.set(g, list)
  }
  const out: GaugeGroup[] = []
  for (const id of GROUP_ORDER) {
    const list = byGroup.get(id)
    if (!list || list.length === 0) continue
    const sorted = sortGaugeRows(list)
    const classes: string[] = []
    for (const r of sorted) if (!classes.includes(r.loop_class)) classes.push(r.loop_class)
    out.push({ id, label: GROUP_META[id].label, blurb: GROUP_META[id].blurb, rows: sorted, classes })
  }
  return out
}

// ---------------------------------------------------------------------------
// Whole-engine counts — read from `totals`, NEVER derived from `loops`.
// ---------------------------------------------------------------------------

export interface ClassCounts {
  loops: number
  gauged: number
  ok: number
  deficit: number
  ungauged: number
}

const ZERO_COUNTS: ClassCounts = { loops: 0, gauged: 0, ok: 0, deficit: 0, ungauged: 0 }

function fromByClass(entry: Record<string, number> | undefined): ClassCounts {
  if (!entry) return { ...ZERO_COUNTS }
  const gauged = entry.gauged ?? 0
  const ungauged = entry.ungauged ?? 0
  return {
    loops: gauged + ungauged,
    gauged,
    ok: entry.ok ?? 0,
    deficit: entry.deficit ?? 0,
    ungauged,
  }
}

/** Whole-engine counts for ONE loop class, straight off `totals.by_class`. */
export function classCounts(totals: ProductionGaugeTotals, loopClass: string): ClassCounts {
  return fromByClass(totals.by_class?.[loopClass])
}

/**
 * Whole-engine counts for a brick family — the sum of its classes' server-side
 * per-class counts. Still pre-filter, because `by_class` is.
 */
export function groupCounts(totals: ProductionGaugeTotals, group: GaugeGroupId): ClassCounts {
  const acc: ClassCounts = { ...ZERO_COUNTS }
  for (const [cls, entry] of Object.entries(totals.by_class ?? {})) {
    if (loopClassGroup(cls) !== group) continue
    const c = fromByClass(entry)
    acc.loops += c.loops
    acc.gauged += c.gauged
    acc.ok += c.ok
    acc.deficit += c.deficit
    acc.ungauged += c.ungauged
  }
  return acc
}

export interface SeverityBucket {
  severity: string
  count: number
  /** Would a deficit at this severity page? Answered against the PAYLOAD floor. */
  pages: boolean
}

/**
 * `totals.by_severity` worst-first, zeros dropped, each marked against the
 * payload's own alert floor. Unknown severities keep their count and land last
 * rather than being discarded.
 */
export function severityBuckets(res: ProductionGaugeResponse): SeverityBucket[] {
  const raw = res.totals?.by_severity ?? {}
  const known = SEVERITY_DESC.filter((s) => (raw[s] ?? 0) > 0)
  const unknown = Object.keys(raw)
    .filter((s) => !SEVERITY_DESC.includes(s) && (raw[s] ?? 0) > 0)
    .sort()
  return [...known, ...unknown].map((severity) => ({
    severity,
    count: raw[severity] ?? 0,
    pages: meetsAlertFloor(severity, res.alert_min_severity),
  }))
}

// ---------------------------------------------------------------------------
// Ratio — the measured / unmeasured discriminated union.
// ---------------------------------------------------------------------------

/**
 * The ratio meter maps onto the SERVER's own severity ramp
 * (`production_gauge.severity_for_ratio`: 1x medium, 2x high, 4x critical), so
 * the bar's full width is "critical" and the marked threshold is the bar the
 * loop had to clear.
 */
export const RATIO_METER_CAP = 4

export interface RatioMeter {
  /** Fill, 0-100, clamped to the cap. */
  pct: number
  /** Where 1.0x — the loop's own bar — sits on that scale. */
  thresholdPct: number
  /** True when the real ratio ran off the end of the meter. */
  clamped: boolean
  cap: number
}

export function ratioMeter(ratio: number, cap: number = RATIO_METER_CAP): RatioMeter {
  const bounded = Math.min(Math.max(ratio, 0), cap)
  return {
    pct: (bounded / cap) * 100,
    thresholdPct: (1 / cap) * 100,
    clamped: ratio > cap,
    cap,
  }
}

/** `2.40x` — the observed absence over the bar that absence had to clear. */
export function formatRatio(ratio: number): string {
  return `${ratio.toFixed(2)}×`
}

export interface MeasuredRatio {
  measured: true
  ratio: number
  text: string
  meter: RatioMeter
  /** Did it clear its own bar? (`ratio >= 1`.) */
  overBar: boolean
}

export interface UnmeasuredRatio {
  measured: false
  /** The raw `quiet_reason`, or `'unstated'` when the server sent none. */
  quietReason: string
  /** Plain-language rendering of that reason. */
  text: string
  /** True when the silence is a FAILED query, not quiet-by-design. */
  readFailure: boolean
}

/** Deliberately a union with no `ratio` on the unmeasured arm — an ungauged row
 *  has no number to render, so no caller can accidentally render it as 0.0. */
export type RatioReading = MeasuredRatio | UnmeasuredRatio

export function readRatio(row: ProductionGaugeRow): RatioReading {
  // Both conditions, not one: the contract says they coincide, and if a payload
  // ever breaks it the safe reading is "we cannot say".
  if (row.state === 'ungauged' || row.ratio == null) {
    const quietReason = row.quiet_reason ?? 'unstated'
    return {
      measured: false,
      quietReason,
      text: quietReasonLabel(row.quiet_reason),
      readFailure: isReadFailureQuietReason(row.quiet_reason),
    }
  }
  return {
    measured: true,
    ratio: row.ratio,
    text: formatRatio(row.ratio),
    meter: ratioMeter(row.ratio),
    overBar: row.ratio >= 1,
  }
}

// ---------------------------------------------------------------------------
// Quiet-by-design vocabulary.
// ---------------------------------------------------------------------------

/** The `ungauged` reasons, mirroring the four gauge modules' constants. */
export const QUIET_REASON_TEXT: Record<string, string> = {
  // production_gauge
  not_active: 'the descriptor is not active — draft, configured, paused or retired',
  no_declared_cadence: 'no declared cadence — it runs on demand, so nothing is owed',
  unparsable_cadence: 'its declared cadence could not be parsed into an interval',
  gather_only: 'gather-only — it opts out of cadence-driven substrate consumption',
  trace_only_by_observation: 'observed as trace-only — it has never written a substrate row',
  never_ran_within_window: 'it never ran inside the window, so there is no rate to compare',
  activation_grace: 'recently activated — still inside its grace period',
  insufficient_history: 'too little history to form an honest baseline',
  no_overdue_work: 'no overdue work exists to drain',
  owner_not_running: 'the owning analyst is not running, so the backlog is not its fault',
  polling_errors: 'polling errored — the loop could not be observed at all',
  // production_gauge_metering
  no_calls_in_window: 'no LLM calls in the window',
  no_burn_threshold: 'no daily-spend ceiling is declared for this component',
  no_spend_data: 'no spend rows in the window',
  latency_query_failed: 'the latency query FAILED — this is a read failure, not a quiet loop',
  burn_query_failed: 'the spend query FAILED — this is a read failure, not a quiet loop',
  // production_gauge_integrity
  no_critiques_in_window: 'no critiques were judged in the window',
  judge_never_configured: 'no judge was ever configured for this loop',
  prompt_manifest_unavailable: 'the prompt manifest is unavailable, so live and tree cannot be compared',
  no_live_descriptor_prompts: 'no live descriptor prompts exist to compare',
  no_live_descriptors: 'no live descriptors exist to compare',
  no_copresent_descriptors: 'nothing is present in BOTH live and tree, so there is nothing to diff',
  judge_query_failed: 'the judge query FAILED — this is a read failure, not a quiet loop',
  drift_query_failed: 'the prompt-drift query FAILED — this is a read failure, not a quiet loop',
  state_drift_query_failed: 'the state-drift query FAILED — this is a read failure, not a quiet loop',
  // production_gauge_staleness
  no_head_ages_stamp: 'no desk-head age stamp has been written yet',
  staleness_query_failed: 'the staleness query FAILED — this is a read failure, not a quiet loop',
}

/**
 * A silence whose reason is a failed query is NOT quiet-by-design — it is the
 * gauge admitting it could not look, and it reads differently from a paused
 * descriptor.
 */
export function isReadFailureQuietReason(reason: string | null): boolean {
  return reason != null && reason.endsWith('_query_failed')
}

/** Plain-language reason. An unknown reason renders verbatim, never as "fine". */
export function quietReasonLabel(reason: string | null): string {
  if (reason == null || reason === '') {
    return 'ungauged, and the server stated no reason — treat this as unknown, not as healthy'
  }
  return QUIET_REASON_TEXT[reason] ?? reason
}

// ---------------------------------------------------------------------------
// Read state — a failed read is not an all-clear.
// ---------------------------------------------------------------------------

export type GaugeReadState = 'read_failed' | 'engine_quiet' | 'reporting'

export function gaugeReadState(res: ProductionGaugeResponse): GaugeReadState {
  if (!res.measured) return 'read_failed'
  if ((res.totals?.loops ?? 0) === 0) return 'engine_quiet'
  return 'reporting'
}

export interface GaugeNotice {
  state: Exclude<GaugeReadState, 'reporting'>
  headline: string
  detail: string
}

/**
 * The loud banner, or `null` when the gauge is genuinely reporting.
 *
 * The two non-reporting states must not look alike: `measured: false` is a
 * FAILURE degraded to HTTP 200, while `measured: true` with zero loops is a
 * successful read of an engine that has no producing loop.
 */
export function gaugeNotice(res: ProductionGaugeResponse): GaugeNotice | null {
  const state = gaugeReadState(res)
  if (state === 'reporting') return null
  if (state === 'read_failed') {
    return {
      state,
      headline: 'READ FAILED — nothing below was measured',
      detail:
        'The gauge could not reach the substrate and degraded to an empty payload at HTTP 200 (measured: false). No loop has been cleared, no deficit has been ruled out. This is not an engine with nothing to report.',
    }
  }
  return {
    state,
    headline: 'Measured, and there is nothing to gauge',
    detail:
      'The read succeeded (measured: true) and found no producing loop at all — no analyst, source, backlog or brick. That is a real answer, unlike a failed read.',
  }
}

// ---------------------------------------------------------------------------
// Summary lines.
// ---------------------------------------------------------------------------

function plural(n: number, one: string, many: string): string {
  return n === 1 ? one : many
}

/**
 * The subtitle. Reports gauged and ungauged as SEPARATE numbers and never
 * emits a percentage — a single "health %" is exactly the blur this panel
 * exists to refuse.
 */
export function gaugeSummaryLine(res: ProductionGaugeResponse): string {
  if (!res.measured) {
    return 'read FAILED (measured: false) — nothing was gauged, nothing was cleared'
  }
  const t = res.totals
  if (t.loops === 0) {
    return `no producing loop found · ${res.window_days}d baseline · read succeeded`
  }
  return [
    `${t.loops} loops`,
    `${t.gauged} gauged (${t.ok} ok · ${t.deficit} deficit)`,
    `${t.ungauged} ungauged`,
    `${t.paging} paging at ${res.alert_min_severity}+`,
    `${res.window_days}d baseline`,
  ].join(' · ')
}

/** Always says "whole-engine", and says so LOUDER when a filter is on. */
export function totalsCaption(filterLabel: string | null): string {
  return filterLabel == null
    ? 'Whole-engine totals — computed server-side over every loop.'
    : `Whole-engine totals — computed server-side over every loop BEFORE the ${filterLabel} filter, not from the ${filterLabel} rows below.`
}

/** How many rows the current filter is showing, against the true denominator. */
export function shownLine(shown: number, totals: ProductionGaugeTotals): string {
  return `${shown} ${plural(shown, 'row', 'rows')} shown of ${totals.loops} gauged loops`
}

/**
 * What would actually reach the operator's phone, stated against the payload's
 * own floor rather than a threshold this file invented.
 */
export function pagingExplainer(res: ProductionGaugeResponse): string {
  const t = res.totals
  const floor = res.alert_min_severity
  if (t.deficit === 0) {
    return `No loop is in deficit — nothing would page (the alert plane's floor is severity ${floor} and above).`
  }
  if (t.paging === 0) {
    return `${t.deficit} ${plural(t.deficit, 'deficit', 'deficits')}, none at or above the ${floor} floor — they surface here and stay off the operator's phone.`
  }
  return `${t.paging} of ${t.deficit} ${plural(t.deficit, 'deficit', 'deficits')} ${plural(t.paging, 'clears', 'clear')} the alert floor (severity ${floor} and above) and would page.`
}

/** Per-row: why this row does or does not page. `null` for a non-deficit row. */
export function pagingNote(row: ProductionGaugeRow, alertMinSeverity: string): string | null {
  if (row.pages) {
    return `pages — deficit at ${row.severity}, at or above the ${alertMinSeverity} alert floor`
  }
  if (row.state === 'deficit') {
    return `does not page — ${row.severity} is below the ${alertMinSeverity} alert floor`
  }
  return null
}

// ---------------------------------------------------------------------------
// Filter.
// ---------------------------------------------------------------------------

export type GaugeScope = 'all' | 'deficits' | 'paging'

export interface GaugeFilter {
  scope: GaugeScope
  loopClass: string | null
  /** `null` = let the server use its own default baseline depth. */
  windowDays: number | null
}

export const EMPTY_FILTER: GaugeFilter = { scope: 'all', loopClass: null, windowDays: null }

/** A short name for the active narrowing, or `null` when nothing is narrowed. */
export function describeFilter(f: GaugeFilter): string | null {
  const parts: string[] = []
  if (f.scope === 'deficits') parts.push('deficits-only')
  if (f.scope === 'paging') parts.push('paging-only')
  if (f.loopClass) parts.push(loopClassLabel(f.loopClass))
  return parts.length === 0 ? null : parts.join(' + ')
}

/** Filter → `fetchProductionGauge` options. `window_days` is a server-side
 *  baseline override, not a client filter, so it rides here too. */
export function gaugeQueryOptions(f: GaugeFilter, limit: number): {
  loopClass?: string
  deficitsOnly?: boolean
  pagingOnly?: boolean
  windowDays?: number
  limit: number
} {
  const opts: {
    loopClass?: string
    deficitsOnly?: boolean
    pagingOnly?: boolean
    windowDays?: number
    limit: number
  } = { limit }
  if (f.scope === 'deficits') opts.deficitsOnly = true
  if (f.scope === 'paging') opts.pagingOnly = true
  if (f.loopClass) opts.loopClass = f.loopClass
  if (f.windowDays != null) opts.windowDays = f.windowDays
  return opts
}

// ---------------------------------------------------------------------------
// Evidence + errors.
// ---------------------------------------------------------------------------

export interface EvidenceField {
  key: string
  value: string
}

function formatEvidenceValue(v: unknown): string {
  if (v == null) return 'null'
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : String(Number(v.toFixed(3)))
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  if (typeof v === 'string') return v
  try {
    return JSON.stringify(v)
  } catch {
    return String(v)
  }
}

/**
 * The numbers a verdict was computed from, key-sorted. The shape varies by
 * `loop_class` on purpose (a cadence deficit and a source drought are not in
 * the same units), so this renders whatever arrived rather than assuming keys.
 */
export function evidenceFields(evidence: Record<string, unknown>): EvidenceField[] {
  return Object.keys(evidence ?? {})
    .sort()
    .map((key) => ({ key, value: formatEvidenceValue(evidence[key]) }))
}

/** A read failure the panel must show instead of an empty gauge. */
export function gaugeErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body
    let detail = ''
    if (typeof body === 'string') detail = body
    else if (body && typeof body === 'object' && 'detail' in body) {
      const d = (body as { detail: unknown }).detail
      detail = typeof d === 'string' ? d : JSON.stringify(d)
    }
    return `HTTP ${err.status}${detail ? ` — ${detail}` : ''}`
  }
  return String((err as Error)?.message ?? err)
}
