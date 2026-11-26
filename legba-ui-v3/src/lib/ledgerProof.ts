/**
 * ledgerProof — the read model of the Ledger page (U-11): forecasts, the
 * track record, the evaluation record, and the admin resolution queue.
 *
 * The routes (reads, then the F-1 writes):
 *
 *   GET /v3/ledger/forecasts          every sealed forecast (the store's own rows)
 *   GET /v3/ledger/scorecard          the scoring of those rows, with the
 *                                     resolver's newest proposal and act
 *   GET /v3/system/receipt-anchors    the daily anchors the seals fold into
 *   GET /v3/ledger/resolution-records one question's proposals and acts, with
 *                                     the evidence the resolver cites (admin side)
 *   GET /v3/eval/runs                 the evaluation runs the owner published
 *                                     (E-3, migration 0260): every version, a
 *                                     withdrawn one marked; `runs: []` is the
 *                                     designed "no evaluation published yet"
 *   GET /v3/ledger/seal-proof         F-1: a seal's inclusion path in a daily
 *                                     anchor's tree, verified HERE (in the browser)
 *   GET /v3/ledger/resolution-queue   F-1: what waits on this person, with what
 *                                     they may do (a contributor sees only the
 *                                     questions they may decide)
 *   POST /v3/ledger/resolutions/{record}/confirm | reject | judgement
 *                                     F-1: a person decides; the same store calls
 *                                     as the host script, one record per act
 *
 * THE OWNER'S RULES, enforced here and nowhere else:
 *
 *   1. No forecast score is shown until {@link SCORE_FLOOR} questions have
 *      resolved. {@link trackRecordGate} is the ONE switch: it opens only when
 *      the route has answered, the count of resolved questions is at least the
 *      floor (the larger of ours and the route's), AND the route itself says
 *      scores are not withheld. Either side saying "not yet" keeps it shut, so
 *      a route that drops its own floor cannot put a figure on screen, and a
 *      console bug cannot either. Below the floor the page prints the count
 *      toward it and what will appear — no score, no partial score, no colour.
 *   2. Results are shown as they are, losses included: every resolved forecast
 *      is listed with its outcome, a miss as plainly as a hit, never folded.
 *
 * Pure: the panels render these; `ledgerProof.test.ts` is the contract.
 */
import type { LedgerForecast } from '@/lib/ledgerModel'
import type { ScorecardQuestion, ScorecardResponse } from '@/lib/scorecardModel'

export const SCORE_FLOOR = 5
export const ANCHORS_ROUTE = '/v3/system/receipt-anchors?limit=60'
export const RECORDS_ROUTE = '/v3/ledger/resolution-records'
export const EVAL_RUNS_ROUTE = '/v3/eval/runs'
export const QUEUE_ROUTE = '/v3/ledger/resolution-queue'
export const SEAL_PROOF_ROUTE = '/v3/ledger/seal-proof'

const DASH = '—'
const WORDS = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten']

/** Small counts in words ("five"), larger ones as numerals. */
export function countWord(n: number): string {
  return Number.isInteger(n) && n >= 0 && n < WORDS.length ? WORDS[n] : String(n)
}

// ---------------------------------------------------------------------------
// Who stated a number — reader words, never the machine's issuer ids.
// ---------------------------------------------------------------------------

export const ISSUER_WORDS: Readonly<Record<string, string>> = {
  operator: 'Owner',
  tower: 'This system',
  consult: 'Consult',
  synthesis: 'Synthesis',
  seat: 'Panel seat',
  actor_model: 'Actor model',
  frontier_arm: 'Frontier model',
  clef_arm: 'Decision model',
}

const ISSUER_ORDER = ['operator', 'tower', 'consult', 'synthesis', 'seat', 'actor_model', 'frontier_arm', 'clef_arm']

export function issuerWord(kind: string): string {
  return ISSUER_WORDS[kind] ?? kind.replace(/[_-]+/g, ' ').replace(/^\w/, (c) => c.toUpperCase())
}

export function fmtPct(p: number | null | undefined): string {
  return p == null || !Number.isFinite(p) ? DASH : `${Math.round(p * 100)} %`
}

export function day(iso: string | null | undefined): string {
  if (!iso) return DASH
  const t = parseTs(iso)
  return t == null ? iso.slice(0, 10) : new Date(t).toISOString().slice(0, 10)
}

/**
 * A timestamp as the routes write it — ISO, or Postgres text
 * (`2026-11-14 00:10:01.36+00`) — to epoch ms; null when unreadable.
 */
export function parseTs(s: string | null | undefined): number | null {
  if (!s) return null
  let t = s.trim().replace(' ', 'T')
  if (/[+-]\d\d$/.test(t)) t = `${t}:00`
  const ms = Date.parse(t)
  return Number.isFinite(ms) ? ms : null
}

export interface StatedNumber {
  kind: string
  who: string
  /** "75 %" or "no number". */
  text: string
  p: number | null
  /** Why there is no number (the store's own reason). */
  reason: string | null
}

/** Every issuer's number on one forecast, the owner first; an absence keeps its reason. */
export function statedNumbers(f: Pick<LedgerForecast, 'numbers'>): StatedNumber[] {
  const rank = (k: string) => {
    const i = ISSUER_ORDER.indexOf(k)
    return i < 0 ? ISSUER_ORDER.length : i
  }
  return [...(f.numbers ?? [])]
    .sort((a, b) => rank(a.issuer_kind) - rank(b.issuer_kind))
    .map((n) => ({
      kind: n.issuer_kind,
      who: issuerWord(n.issuer_kind),
      p: typeof n.p === 'number' ? n.p : null,
      text: typeof n.p === 'number' ? fmtPct(n.p) : 'no number',
      reason: typeof n.p === 'number' ? null : (n.absent_reason ?? 'no reason recorded'),
    }))
}

// ---------------------------------------------------------------------------
// One forecast's state.
// ---------------------------------------------------------------------------

export type ForecastStateKind =
  | 'open'
  | 'overdue'
  | 'awaiting_confirmation'
  | 'needs_judgement'
  | 'resolved_yes'
  | 'resolved_no'
  | 'void'

export type Tone = 'neutral' | 'warning'

export interface ForecastState {
  kind: ForecastStateKind
  label: string
  /** One plain sentence under the label, or ''. */
  detail: string
  tone: Tone
}

const CLOSING = new Set(['yes', 'no', 'void'])

/**
 * Open; past due; a proposal awaiting a person's confirmation; waiting on a
 * judgement the rule cannot make; resolved yes / no; voided with its reason.
 * A resolved outcome carries no colour: "yes" is not good news and "no" is
 * not a loss — the forecast's probability decides that, in the track record.
 */
export function forecastState(
  f: Pick<LedgerForecast, 'status' | 'overdue' | 'resolution' | 'due_at'>,
  q?: Pick<ScorecardQuestion, 'proposal' | 'latest_act' | 'outcome'> | null,
  voidReason?: string | null,
  now: Date = new Date(),
): ForecastState {
  const res = f.resolution
  const outcome = res?.outcome ?? q?.outcome ?? null
  if (f.status === 'void' || outcome === 'void') {
    const why = res?.note || voidReason || 'no reason was recorded with it'
    return { kind: 'void', label: 'Voided', detail: `Voided: ${why}`, tone: 'neutral' }
  }
  if (outcome === 'yes' || outcome === 'no' || f.status === 'resolved') {
    const word = outcome === 'no' ? 'no' : 'yes'
    const when = res?.resolved_at ? ` on ${day(res.resolved_at)}` : ''
    const by = actWords(q?.latest_act ?? null)
    return {
      kind: word === 'yes' ? 'resolved_yes' : 'resolved_no',
      label: `Resolved: ${word}`,
      detail: `Resolved ${word}${when}${by ? `; ${by}` : ''}.`,
      tone: 'neutral',
    }
  }
  const prop = q?.proposal ?? null
  const act = q?.latest_act ?? null
  const actAfter = !!(act && prop && (parseTs(act.recorded_at) ?? 0) >= (parseTs(prop.recorded_at) ?? 0))
  const notAccepted =
    actAfter && act?.record_kind === 'rejection'
      ? ` A proposed reading was not accepted by ${roleWords(act.actor_role, act.actor)} on ${day(act.recorded_at)}; it stays open.`
      : ''
  if (prop && !actAfter) {
    if (CLOSING.has(prop.outcome)) {
      return {
        kind: 'awaiting_confirmation',
        label: `Proposed: ${prop.outcome} — awaiting confirmation`,
        detail: `The resolver read it as "${prop.outcome}" on ${day(prop.recorded_at)}. Nothing closes until a person confirms it.`,
        tone: 'warning',
      }
    }
    if (prop.outcome === 'needs_judgement') {
      return {
        kind: 'needs_judgement',
        label: 'Waiting on a judgement',
        detail: 'The resolver found something the rule cannot decide alone; a person decides it.',
        tone: 'warning',
      }
    }
  }
  const due = parseTs(f.due_at)
  if (f.overdue || (due != null && due < now.getTime())) {
    return { kind: 'overdue', label: 'Past due', detail: `Its due date has passed and it is not resolved yet.${notAccepted}`, tone: 'warning' }
  }
  return { kind: 'open', label: 'Open', detail: notAccepted.trim(), tone: 'neutral' }
}

/**
 * Who acted, as a role — never a person's name or id: the owner, an admin or
 * a contributor (recorded by the console's routes); the rule itself for a
 * settlement by the published value; "a person" for an act made on the host.
 */
export function roleWords(role: string | null | undefined, actor?: string | null): string {
  if (role === 'owner') return 'the owner'
  if (role === 'admin') return 'an admin'
  if (role === 'contributor') return 'a contributor'
  if (actor && actor.startsWith('resolver:')) return 'the rule itself'
  return 'a person'
}

const ACT_VERB: Record<string, string> = { confirmation: 'confirmed', override: 'decided', correction: 'corrected' }

/** "confirmed by an admin on 2026-11-26" for the act that closed a forecast, or ''. */
export function actWords(
  act: { record_kind?: string | null; actor_role?: string | null; actor?: string | null; recorded_at?: string | null } | null,
): string {
  const verb = act?.record_kind ? ACT_VERB[act.record_kind] : undefined
  if (!act || !verb) return ''
  if (act.actor && act.actor.startsWith('resolver:')) return 'settled by the published value itself'
  return `${verb} by ${roleWords(act.actor_role, act.actor)} on ${day(act.recorded_at)}`
}

// ---------------------------------------------------------------------------
// The resolution rule, in plain words.
// ---------------------------------------------------------------------------

export interface ResolutionRule {
  /** What makes it "yes". */
  rule: string
  /** What counts as confirmation, if the rule names it. */
  confirmation: string | null
  /** The pre-registered void rules, verbatim. */
  voidRule: string | null
  /** The condition it is conditional on, verbatim. */
  condition: string | null
  /** "Due 2026-11-25, with 72 hours of grace." */
  timing: string
}

type Spec = Record<string, unknown>

const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v.trim() : typeof v === 'number' ? String(v) : null)
const list = (v: unknown): string | null => {
  if (Array.isArray(v)) {
    const xs = v.map(str).filter((x): x is string => !!x)
    if (xs.length === 0) return null
    return xs.length === 1 ? xs[0] : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`
  }
  return str(v)
}
const windowWords = (v: unknown): string | null => {
  if (Array.isArray(v) && v.length === 2 && str(v[0]) && str(v[1])) return `between ${str(v[0])} and ${str(v[1])}`
  return null
}
const OPS: Record<string, string> = { '>=': 'at or above', '>': 'above', '<=': 'at or below', '<': 'below', '==': 'exactly' }

function ruleFor(kind: string, spec: Spec): string | null {
  switch (kind) {
    case 'event_reported': {
      const what = str(spec.class)
      if (!what) return null
      const who = list(spec.actors) ?? list(spec.actor)
      const where = str(spec.target_area)
      const when = windowWords(spec.window)
      return `Yes if a ${what} is reported${who ? ` (by ${who})` : ''}${where ? ` on ${where}` : ''}${when ? ` ${when}` : ''}.`
    }
    case 'official_document': {
      const doc = str(spec.doc_type)
      if (!doc) return null
      const who = list(spec.issuers)
      const by = str(spec.by)
      const inEffect = str(spec.in_effect_on)
      let s = `Yes if ${who ?? 'the named authorities'} issue a ${doc}${by ? ` by ${by}` : ''}${inEffect ? `, in effect on ${inEffect}` : ''}`
      const ack = str(spec.acknowledged_by)
      if (ack) s += `, acknowledged by ${ack}`
      const also = Array.isArray(spec.also) ? spec.also : []
      for (const a of also) {
        const sub = (a && typeof a === 'object' ? (a as Spec).spec : null) as Spec | null
        const cls = sub ? str(sub.class) : null
        if (!cls) continue
        const w = windowWords(sub!.window)
        s += (a as Spec).negate ? `, and no ${cls} is reported${w ? ` ${w}` : ''}` : `, and a ${cls} is reported${w ? ` ${w}` : ''}`
      }
      return `${s}.`
    }
    case 'official_result': {
      const outcome = str(spec.outcome)
      if (!outcome) return null
      const race = str(spec.race)
      const auth = str(spec.authority)
      const date = str(spec.election_date) ?? str(spec.vote_date)
      return `Yes if the result is: ${outcome}${race ? ` (${race})` : ''}${auth ? `, as published by ${auth}` : ''}${date ? `; the vote is on ${date}` : ''}.`
    }
    case 'series_threshold': {
      const series = str(spec.series)
      const value = str(spec.value)
      if (!series || value == null) return null
      const op = OPS[String(spec.op)] ?? String(spec.op ?? '')
      const src = str(spec.source)
      const on = str(spec.on)
      const hol = str(spec.holiday_rule)
      return `Yes if ${src ? `${src} ` : ''}${series} is ${op} ${value}${on ? ` on ${on}` : ''}${hol ? ` (${hol})` : ''}. Settled by the published value itself.`
    }
    case 'count_threshold': {
      const what = str(spec.count) ?? str(spec.class)
      const value = str(spec.value)
      if (!what || value == null) return null
      return `Yes if the official count of ${what} is ${OPS[String(spec.op)] ?? 'at least'} ${value}.`
    }
    case 'scheduled_event_status': {
      const ev = str(spec.event)
      const st = str(spec.status)
      if (!ev || !st) return null
      return `Yes if ${ev} is ${st}.`
    }
    default:
      return null
  }
}

export function resolutionRule(f: Pick<LedgerForecast, 'resolver_kind' | 'resolver_spec' | 'void_rules' | 'condition' | 'due_at' | 'grace_hours'>): ResolutionRule {
  const spec = (f.resolver_spec && typeof f.resolver_spec === 'object' ? f.resolver_spec : {}) as Spec
  const rule = ruleFor(f.resolver_kind, spec) ?? 'Resolved by the rule sealed with the question, as the question states it.'
  const grace = typeof f.grace_hours === 'number' && f.grace_hours > 0 ? `, with ${f.grace_hours} hours of grace for the confirming report` : ''
  return {
    rule,
    confirmation: str(spec.confirmation),
    voidRule: f.void_rules?.trim() || null,
    condition: f.condition?.trim() || null,
    timing: `Due ${day(f.due_at)}${grace}.`,
  }
}

// ---------------------------------------------------------------------------
// The seal, and where it can be checked.
// ---------------------------------------------------------------------------

export interface AnchorRow {
  id: string
  day: string
  root_hash: string
  leaf_count: number
  calendar: string
  status: string
  submitted_at?: string | null
  proof?: string | null
}
export interface AnchorsResponse {
  version?: string
  anchors: AnchorRow[]
}

/** What a question's seal covers (`forecast_ledger.ForecastQuestion.content`). */
export const SEAL_COVERS: readonly string[] = [
  'the question as worded',
  'its resolution rule',
  'the window and the due date, with the grace',
  'the void rules and any condition',
  'the subjects it is about',
]

export type SealState = 'unsealed' | 'reading' | 'unreadable' | 'not_covered' | 'pending' | 'anchored'

export interface SealCheck {
  state: SealState
  /** One plain line: where the reader can check it, and what it does not prove. */
  text: string
  anchor: { day: string; root: string; calendars: number; leafCount: number } | null
}

/**
 * Where a seal can be checked: the first daily anchor made AFTER the seal
 * folds every ledger hash present at that moment (`receipt_anchor`), so a
 * sealed forecast is covered from the first anchor after its seal. The
 * console states coverage; it does not verify the timestamp proof (that is
 * done offline with the OpenTimestamps client against the stored proof) and
 * the anchors route lists roots, not leaves, so this alone never says
 * "included": the inclusion path is {@link sealProofCheck}'s (F-1).
 */
export function sealCheck(sealedAt: string | null | undefined, anchors: AnchorRow[] | null | undefined): SealCheck {
  if (!sealedAt) return { state: 'unsealed', text: 'Not sealed: this forecast can still change.', anchor: null }
  if (anchors === undefined) return { state: 'reading', text: 'Reading the daily anchors…', anchor: null }
  if (anchors === null) {
    return { state: 'unreadable', text: 'The daily anchors could not be read, so coverage is not known.', anchor: null }
  }
  const sealed = parseTs(sealedAt)
  const after = anchors
    .filter((a) => {
      const t = parseTs(a.submitted_at ?? null) ?? parseTs(`${a.day}T23:59:59Z`)
      return sealed != null && t != null && t > sealed
    })
    .sort((a, b) => a.day.localeCompare(b.day))
  if (after.length === 0) {
    return {
      state: 'not_covered',
      text: 'Not covered yet: sealed after the newest daily anchor. The next anchor will include it.',
      anchor: null,
    }
  }
  const first = after[0].day
  const rows = after.filter((a) => a.day === first)
  const submitted = rows.filter((a) => a.status === 'submitted')
  const anchor = { day: first, root: rows[0].root_hash, calendars: submitted.length, leafCount: rows[0].leaf_count }
  if (submitted.length === 0) {
    return {
      state: 'pending',
      text: `Folded into the anchor of ${first}, which has not reached a public calendar yet.`,
      anchor,
    }
  }
  return {
    state: 'anchored',
    text: `Covered by the daily anchor of ${first}, submitted to ${countWord(submitted.length)} public timestamp calendar${submitted.length === 1 ? '' : 's'}. The timestamp itself is not checked in the console: check the stored proof with the OpenTimestamps client.`,
    anchor,
  }
}

/** The anchor leaf of a sealed question, as text to hash: `forecast_question:<id><sha256>`. */
export function questionLeafInput(id: string, contentSha256: string): string {
  return `forecast_question:${id}${contentSha256}`
}

// ---------------------------------------------------------------------------
// The track record — THE gate.
// ---------------------------------------------------------------------------

export type TrackRecordState = 'reading' | 'not_deployed' | 'unreadable' | 'error' | 'withheld' | 'scored'

export interface TrackRecordGate {
  state: TrackRecordState
  /** Resolved questions (yes or no); null until read. */
  resolved: number | null
  /** The floor in force: the larger of ours and the route's. */
  floor: number
  /** Resolutions still needed; null until read. */
  remaining: number | null
  /** The one line the region leads with. */
  line: string
}

/** What will appear once the floor is reached — said in words, never as a figure. */
export const WILL_APPEAR: readonly string[] = [
  'the Brier score of each forecaster (lower is better), beside the score of always saying 50 %',
  'calibration: the forecasts grouped by probability band, with the count in each band',
  'every resolved forecast with its outcome, the misses listed as plainly as the hits',
]

/**
 * `data`: undefined while reading, null on a 404 (route not deployed), else
 * the scorecard. `failed`: a non-404 error.
 */
export function trackRecordGate(data: ScorecardResponse | null | undefined, opts: { failed?: boolean } = {}): TrackRecordGate {
  const floor = Math.max(SCORE_FLOOR, data?.scores_min_resolved ?? data?.header?.scores_min_resolved ?? SCORE_FLOOR)
  if (opts.failed) return { state: 'error', resolved: null, floor, remaining: null, line: 'The track record could not be read; no figure is shown.' }
  if (data === undefined) return { state: 'reading', resolved: null, floor, remaining: null, line: 'Reading the track record…' }
  if (data === null) return { state: 'not_deployed', resolved: null, floor, remaining: null, line: 'The track record is not served by this installation yet.' }
  if (data.available === false || !data.header) {
    return {
      state: 'unreadable',
      resolved: null,
      floor,
      remaining: null,
      line: 'The forecast ledger could not be read. That is not the same as an empty record; no figure is shown.',
    }
  }
  const resolved = Math.max(0, data.header.n_resolved | 0)
  const open = resolved >= floor && data.scores_withheld === false && data.header.scores_withheld !== true
  if (!open) {
    const remaining = Math.max(0, floor - resolved)
    const line =
      resolved >= floor
        ? `${resolved} forecasts have resolved; the scores are not published yet. No score is shown until they are.`
        : `${resolved} of the ${floor} resolutions needed before any score is shown.`
    return { state: 'withheld', resolved, floor, remaining, line }
  }
  const voids = data.header.n_void ? ` ${data.header.n_void} voided, not counted.` : ''
  return { state: 'scored', resolved, floor, remaining: 0, line: `Scored over ${resolved} resolved forecasts.${voids}` }
}

export interface ArmScore {
  kind: string
  who: string
  n: number
  /** "0.142" or the reason there is none. */
  brier: string
  baseline: string
  /** "better than always 50 %" / "worse than …" / "level with …" / '' */
  versus: string
  versusKind: 'better' | 'worse' | 'level' | 'none'
}

/** Each forecaster's Brier against the stated baseline. ONLY for a scored gate. */
export function armScores(data: ScorecardResponse, gate: TrackRecordGate): ArmScore[] {
  if (gate.state !== 'scored') return []
  return (data.arms ?? []).map((a) => {
    const who = issuerWord(a.issuer_kind)
    if (a.below_min_sample || a.mean_brier == null) {
      return {
        kind: a.issuer_kind,
        who,
        n: a.n,
        brier: `no score: ${a.n} resolved from this forecaster (fewer than ${gate.floor})`,
        baseline: DASH,
        versus: '',
        versusKind: 'none',
      }
    }
    const base = a.baseline_mean_brier
    const d = base == null ? null : a.mean_brier - base
    const versusKind: ArmScore['versusKind'] = d == null ? 'none' : Math.abs(d) < 0.0005 ? 'level' : d < 0 ? 'better' : 'worse'
    const versus =
      versusKind === 'none' ? '' : versusKind === 'level' ? 'level with always saying 50 %' : `${versusKind} than always saying 50 %`
    return { kind: a.issuer_kind, who, n: a.n, brier: a.mean_brier.toFixed(3), baseline: base == null ? DASH : base.toFixed(3), versus, versusKind }
  })
}

export interface BandRow {
  /** "60–70 %" */
  band: string
  /** Forecasts in the band (a measured count; 0 is a real zero). */
  n: number
  /** How often they came true — only when the route publishes the band table. */
  observed: string
}

export interface Calibration {
  /** Whose numbers the bands hold. */
  who: string
  rows: BandRow[]
  /** Why the observed column is empty, or ''. */
  note: string
}

function leadKind(data: ScorecardResponse): string | null {
  const resolved = (data.questions ?? []).filter((q) => q.outcome === 'yes' || q.outcome === 'no')
  const kinds = new Set<string>()
  for (const q of resolved) for (const n of q.numbers ?? []) if (n.p != null) kinds.add(n.issuer_kind)
  if (kinds.has('operator')) return 'operator'
  return ISSUER_ORDER.find((k) => kinds.has(k)) ?? [...kinds][0] ?? null
}

/**
 * Calibration by probability band, ten bands of ten points, with the count in
 * each. The COUNTS are taken from the resolved forecasts themselves; how
 * often each band came true is the route's to publish (it does from its own
 * calibration minimum, 30 today), never computed here.
 */
export function calibration(data: ScorecardResponse, gate: TrackRecordGate): Calibration | null {
  if (gate.state !== 'scored') return null
  const kind = (data.calibration?.bins_arm as string | undefined) ?? leadKind(data)
  if (!kind) return null
  const counts = new Array(10).fill(0) as number[]
  for (const q of data.questions ?? []) {
    if (q.outcome !== 'yes' && q.outcome !== 'no') continue
    const n = (q.numbers ?? []).find((x) => x.issuer_kind === kind && x.p != null)
    if (!n || n.p == null) continue
    counts[Math.min(9, Math.max(0, Math.floor(n.p * 10)))]++
  }
  const bins = data.calibration?.available && data.calibration.bins_arm === kind ? data.calibration.bins ?? [] : []
  const rows = counts.map((n, i) => {
    const bin = bins.find((b) => Math.abs(b.lo - i / 10) < 1e-9)
    return {
      band: `${i * 10}–${i * 10 + 10} %`,
      n,
      observed: bin && bin.observed != null && bin.n > 0 ? fmtPct(bin.observed) : DASH,
    }
  })
  const note = bins.length
    ? ''
    : `How often each band came true is published from ${data.calibration?.min_sample ?? data.header?.calibration_min_sample ?? 30} resolved forecasts; ${data.calibration?.n ?? 0} so far.`
  return { who: issuerWord(kind), rows, note }
}

export interface ResolvedRow {
  id: string
  slug: string
  question: string
  outcome: 'yes' | 'no'
  resolvedOn: string
  /** The lead forecaster's number. */
  who: string
  p: string
  /** "called it" / "missed" / "no lean (50 %)" — the same weight on screen. */
  verdict: string
  verdictKind: 'called' | 'missed' | 'no_lean' | 'none'
  /** Each forecaster's Brier on this question. */
  briers: { who: string; brier: string }[]
}

/** Every resolved forecast with its outcome, newest first. ONLY for a scored gate. */
export function resolvedRows(data: ScorecardResponse, gate: TrackRecordGate): ResolvedRow[] {
  if (gate.state !== 'scored') return []
  const kind = leadKind(data)
  return (data.questions ?? [])
    .filter((q) => q.outcome === 'yes' || q.outcome === 'no')
    .map((q): ResolvedRow => {
      const lead = (q.numbers ?? []).find((n) => n.issuer_kind === kind && n.p != null) ?? null
      const p = lead?.p ?? null
      const yes = q.outcome === 'yes'
      const verdictKind: ResolvedRow['verdictKind'] =
        p == null ? 'none' : p === 0.5 ? 'no_lean' : (p > 0.5) === yes ? 'called' : 'missed'
      const verdict = { called: 'called it', missed: 'missed', no_lean: 'no lean (50 %)', none: 'no number' }[verdictKind]
      return {
        id: q.id,
        slug: q.slug,
        question: q.question,
        outcome: q.outcome as 'yes' | 'no',
        resolvedOn: day(q.resolution?.resolved_at ?? null),
        who: kind ? issuerWord(kind) : DASH,
        p: fmtPct(p),
        verdict,
        verdictKind,
        briers: (q.numbers ?? [])
          .filter((n) => n.p != null && n.brier != null)
          .map((n) => ({ who: issuerWord(n.issuer_kind), brier: (n.brier as number).toFixed(3) })),
      }
    })
    .sort((a, b) => b.resolvedOn.localeCompare(a.resolvedOn) || a.slug.localeCompare(b.slug))
}

// ---------------------------------------------------------------------------
// The evaluation record — GET /v3/eval/runs (registry/eval_runs_api.py,
// migration 0260; the owner publishes a run with scripts/publish_eval_run.py).
// ---------------------------------------------------------------------------

export interface EvalArm {
  id: string
  /** Plain words: "Consult answering from the verified record". */
  label: string
  description?: string | null
  /** An arm run twice: the arm it repeats and which sample this is. */
  sample_of?: string | null
  sample_index?: number | null
}
export interface EvalClassScore {
  class: string
  score: number | null
  n: number
}
export interface EvalResult {
  arm: string
  metric: string
  metric_label?: string | null
  score: number | null
  ci_low?: number | null
  ci_high?: number | null
  /** 0.95 */
  ci_level?: number | null
  n: number
  higher_is_better?: boolean | null
  by_class?: EvalClassScore[] | null
}
/** current: the newest version, not withdrawn. superseded: a later version corrects it. withdrawn: the owner withdrew it. */
export type EvalRunStatus = 'current' | 'superseded' | 'withdrawn'
export interface EvalRun {
  /** This version's id. */
  id: string
  /** The run's stable id across versions. */
  run_key?: string
  version?: number
  status?: EvalRunStatus
  /** The id of the version this one corrects. */
  supersedes?: string | null
  superseded_by?: string | null
  correction_reason?: string | null
  withdrawn_at?: string | null
  withdrawn_reason?: string | null
  title: string
  /** What the run answers, in one sentence. */
  question?: string | null
  /** The instant every arm answered as of. */
  as_of?: string | null
  published_at?: string | null
  design?: { preregistered_at?: string | null; prereg_sha256?: string | null; version?: string | null } | null
  question_count?: number | null
  question_classes?: { class: string; n: number }[] | null
  grader?: string | null
  temperature?: number | null
  gold_versions?: string[] | null
  arms: EvalArm[]
  results: EvalResult[]
  /** Caveats attached to the run — every one is shown, none folded. */
  caveats?: string[] | null
  /** Departures from the pre-registered design. */
  deviations?: string[] | null
  content_sha256?: string | null
}
export interface EvalRunsResponse {
  version?: string
  available: boolean
  runs: EvalRun[]
}

/**
 * none_published: the route answered with no run (HTTP 200, `runs: []`) — the
 * designed empty state. not_served: a 404, a registry older than the route
 * (it is not the same as "none published", so it is said differently).
 */
export type EvalRecordState = 'reading' | 'none_published' | 'not_served' | 'unreadable' | 'error' | 'published'

export function evalRecordState(data: EvalRunsResponse | null | undefined, opts: { failed?: boolean } = {}): EvalRecordState {
  if (opts.failed) return 'error'
  if (data === undefined) return 'reading'
  if (data === null) return 'not_served'
  if (data.available === false) return 'unreadable'
  return (data.runs ?? []).length ? 'published' : 'none_published'
}

/**
 * The order a reader meets the runs in: the opened run's versions first, then
 * runs by their newest as-of; within a run, the newest version first, so a
 * correction sits directly above the version it corrects.
 */
export function orderEvalRuns(runs: EvalRun[], openRun?: string | null): EvalRun[] {
  const keyOf = (r: EvalRun) => r.run_key || r.id
  const newest = new Map<string, string>()
  for (const r of runs) {
    const k = keyOf(r)
    const a = r.as_of ?? ''
    if (!newest.has(k) || a > (newest.get(k) as string)) newest.set(k, a)
  }
  const opened = openRun ? runs.find((r) => r.id === openRun) : undefined
  const openKey = opened ? keyOf(opened) : null
  return [...runs].sort((a, b) => {
    const ka = keyOf(a)
    const kb = keyOf(b)
    if (ka !== kb) {
      if (openKey && ka === openKey) return -1
      if (openKey && kb === openKey) return 1
      return (newest.get(kb) as string).localeCompare(newest.get(ka) as string) || ka.localeCompare(kb)
    }
    return (b.version ?? 0) - (a.version ?? 0)
  })
}

/** Where a version stands, in words — superseded and withdrawn are said, never styled. */
export function evalStanding(run: EvalRun, runs: EvalRun[]): string {
  const v = run.version ?? 1
  const later = run.superseded_by ? runs.find((r) => r.id === run.superseded_by) : undefined
  const parts: string[] = []
  if (run.status === 'withdrawn') {
    parts.push(`Withdrawn on ${day(run.withdrawn_at)}: ${run.withdrawn_reason || 'no reason was recorded'}. Its figures are kept below as published; they are not a current result.`)
  }
  if (run.status === 'superseded' || (run.status === 'withdrawn' && run.superseded_by)) {
    parts.push(
      `Version ${v}, superseded by version ${later?.version ?? v + 1}${later?.published_at ? ` on ${day(later.published_at)}` : ''}. Kept as it was published.`,
    )
  } else if (v > 1) {
    parts.push(`Version ${v}, correcting version ${v - 1}: ${run.correction_reason || 'no reason was recorded'}.`)
  } else if (run.status !== 'withdrawn') {
    parts.push('Version 1, as first published.')
  }
  return parts.join(' ')
}

/** The run's conditions, each a plain line: as-of, questions and classes, grader, temperature, gold, design. */
export function evalRunFacts(run: EvalRun): string[] {
  const out: string[] = []
  out.push(`Answered as of ${day(run.as_of)}${run.published_at ? ` · published ${day(run.published_at)}` : ''}`)
  if (run.question_count != null) {
    const classes = (run.question_classes ?? []).map((c) => `${c.class.toLowerCase()} ${c.n}`)
    out.push(`${run.question_count} questions${classes.length ? ` in ${countWord(classes.length)} classes (${classes.join(', ')})` : ''}`)
  }
  if (run.grader) out.push(`Marked by ${run.grader}${run.temperature != null ? ` · answering temperature ${run.temperature}` : ''}`)
  if (run.gold_versions?.length) out.push(`Gold answers: ${run.gold_versions.join('; ')}`)
  const d = run.design
  if (d?.version || d?.prereg_sha256) {
    out.push(
      `Design${d.version ? ` version ${d.version}` : ''}${d.preregistered_at ? `, registered ${day(d.preregistered_at)}` : ', no registration date published'}${d.prereg_sha256 ? ` · hash ${d.prereg_sha256.slice(0, 12)}…` : ''}`,
    )
  }
  return out
}

/** What a published run will show — the empty state says it in these words. */
export const EVAL_WILL_SHOW: readonly string[] = [
  'what was compared, in plain words',
  'each score with its interval and the number of questions behind it',
  'the caveats attached to the run',
  'every departure from the design registered before the run',
]

export interface EvalResultRow {
  arm: string
  metric: string
  score: string
  interval: string
  n: string
  /** Per class: "headline 0.583 (n = 7)"; empty when the run published none. */
  byClass: string[]
}

function fmtScore(v: number | null | undefined): string {
  return v == null || !Number.isFinite(v) ? DASH : Math.abs(v) <= 1 ? v.toFixed(3) : v.toFixed(1)
}

/** An arm's label, with its sample said in words when it was run twice. */
export function armWords(a: EvalArm | undefined, arms: EvalArm[], fallback: string): string {
  if (!a) return fallback
  if (a.sample_of && a.sample_index) {
    const of = arms.find((x) => x.id === a.sample_of)
    return `${a.label} (sample ${a.sample_index}${of && of.id !== a.id ? ` of “${of.label}”` : ''})`
  }
  return a.label
}

export function evalResultRows(run: EvalRun): EvalResultRow[] {
  const byId = new Map(run.arms.map((a) => [a.id, a]))
  return run.results.map((r) => ({
    arm: armWords(byId.get(r.arm), run.arms, r.arm),
    metric: r.metric_label || r.metric,
    score: fmtScore(r.score),
    interval:
      r.ci_low != null && r.ci_high != null
        ? `${fmtScore(r.ci_low)} to ${fmtScore(r.ci_high)}${r.ci_level ? ` (${Math.round(r.ci_level * 100)} %)` : ''}`
        : 'no interval published',
    n: `n = ${r.n}`,
    byClass: (r.by_class ?? []).map((c) => `${c.class.toLowerCase()} ${fmtScore(c.score)} (n = ${c.n})`),
  }))
}

// ---------------------------------------------------------------------------
// The resolution queue (admin side).
// ---------------------------------------------------------------------------

export interface QueueItem {
  id: string
  slug: string
  question: string
  outcome: string
  /** "Proposed: yes" / "Needs a judgement" / "Not yet determined". */
  label: string
  recordedAt: string
  evidenceCount: number
  judgements: { key: string; question: string }[]
  closing: boolean
}

/** Open questions whose newest proposal has no act after it, closing outcomes first. */
export function resolutionQueue(data: ScorecardResponse | null | undefined): QueueItem[] {
  if (!data || data.available === false) return []
  const out: QueueItem[] = []
  for (const q of data.questions ?? []) {
    const prop = q.proposal
    if (!prop || q.status !== 'open') continue
    const act = q.latest_act
    if (act && (parseTs(act.recorded_at) ?? 0) >= (parseTs(prop.recorded_at) ?? 0)) continue
    const closing = CLOSING.has(prop.outcome)
    out.push({
      id: q.id,
      slug: q.slug,
      question: q.question,
      outcome: prop.outcome,
      label: closing ? `Proposed: ${prop.outcome}` : prop.outcome === 'needs_judgement' ? 'Needs a judgement' : 'Not yet determined',
      recordedAt: prop.recorded_at ?? '',
      evidenceCount: prop.evidence_count ?? 0,
      judgements: prop.judgements ?? [],
      closing,
    })
  }
  return out.sort((a, b) => Number(b.closing) - Number(a.closing) || a.recordedAt.localeCompare(b.recordedAt))
}

export interface ResolutionRecord {
  id: string
  question_id: string
  record_kind: 'proposal' | 'confirmation' | 'override' | 'correction' | string
  outcome: string
  resolver_kind: string
  resolver_version: string
  as_of: string | null
  recorded_at: string | null
  actor: string
  evidence: Record<string, unknown>[]
  evidence_total: number
  clauses: { id?: string; text?: string; status?: string; evidence?: string[] }[]
  judgements: { key?: string; question?: string }[]
  searched: Record<string, unknown>
  void_rule: Record<string, unknown> | null
  reason: string | null
  content_sha256: string
}
export interface ResolutionRecordsResponse {
  version?: string
  available: boolean
  question_id: string
  evidence_cap?: number
  /** Null today: the console has no confirm route (SEAMS-free: confirming is a host script). */
  confirm_route: string | null
  records: ResolutionRecord[]
}

export interface EvidenceLine {
  ref: string
  title: string
  where: string
  when: string
  url: string | null
  clause: string
  span: string
}

/** One evidence item the resolver cites, in reading order; tolerant of either resolver shape. */
export function evidenceLine(e: Record<string, unknown>): EvidenceLine {
  const s = (k: string) => (typeof e[k] === 'string' ? (e[k] as string) : typeof e[k] === 'number' ? String(e[k]) : '')
  const value = s('value') ? `${s('value')}${s('unit') ? ` ${s('unit')}` : ''}` : ''
  return {
    ref: s('ref'),
    title: s('title') || (value ? `${s('series_id')} = ${value}` : s('ref')),
    where: s('publisher') || s('source_id') || s('series_id'),
    when: day(s('published_at') || s('valid_from') || s('fetched_at') || null),
    url: s('url') || s('source_url') || null,
    clause: s('clause'),
    span: s('span'),
  }
}

/** The host command that confirms a proposal (shown read-only; nothing in the console writes). */
export function confirmCommand(recordId: string): string {
  return `python3 scripts/resolve_forecasts.py confirm --record ${recordId} --actor operator:<you>`
}

// ---------------------------------------------------------------------------
// F-1 — deciding an outcome from the console (the admin side's queue).
// ---------------------------------------------------------------------------

export type Outcome = 'yes' | 'no' | 'void'
export type DeciderRole = 'owner' | 'admin' | 'contributor'

export interface QueueProposal {
  record_id: string
  outcome: string
  as_of?: string | null
  recorded_at?: string | null
  evidence: Record<string, unknown>[]
  evidence_total: number
  clauses?: { id?: string; text?: string; status?: string }[]
  judgements?: { key?: string; question?: string }[]
  void_rule?: Record<string, unknown> | null
}
export interface QueueEntry {
  question_id: string
  slug: string
  question: string
  resolver_kind: string
  resolver_spec?: Record<string, unknown> | null
  machine_resolvable?: boolean
  window_start?: string | null
  window_end?: string | null
  due_at: string
  grace_hours?: number
  condition?: string | null
  void_rules?: string | null
  proposal: QueueProposal
  /** What THIS person may do on it (the server's rule). */
  may: { confirm: boolean; reject: boolean; judgement: boolean }
}
export interface QueueResponse {
  version?: string
  available: boolean
  role: DeciderRole
  may_correct: boolean
  items: QueueEntry[]
  /** Questions held back from this person (a contributor's own numbers). */
  hidden: number
  hidden_reason?: string | null
}

export type DecisionVerb = 'confirm' | 'reject' | 'judgement'
export interface Decision {
  verb: DecisionVerb
  /** confirm: the proposal's outcome; judgement: the person's. */
  outcome?: Outcome
  reason: string
}

export function decisionPath(recordId: string, verb: DecisionVerb): string {
  return `/v3/ledger/resolutions/${encodeURIComponent(recordId)}/${verb}`
}

export function decisionBody(d: Decision): Record<string, unknown> {
  const reason = d.reason.trim()
  if (d.verb === 'reject') return { reason }
  if (d.verb === 'judgement') return { outcome: d.outcome, reason }
  return reason ? { outcome: d.outcome, reason } : { outcome: d.outcome }
}

/** Why a decision cannot be sent yet, in words — or null when it can. */
export function decisionProblem(e: QueueEntry, d: Decision): string | null {
  if (d.verb === 'confirm') {
    if (!e.may.confirm) return 'This proposal has no outcome to confirm.'
    if (d.outcome !== e.proposal.outcome) return 'The outcome to confirm is the proposal’s own.'
    if (d.outcome === 'void' && !d.reason.trim()) return 'Say which void rule applies: a void needs its reason.'
    return null
  }
  if (d.verb === 'reject') {
    if (!e.may.reject) return 'There is nothing to reject here.'
    return d.reason.trim() ? null : 'Say why the proposal is wrong: a rejection needs its reason.'
  }
  if (!e.may.judgement) return 'The rule decides this one; confirm or reject it instead.'
  if (!d.outcome) return 'Choose yes, no or void.'
  return d.reason.trim() ? null : 'Say what you decided and why: a judgement needs its reason.'
}

/** The "are you sure" sentence, shown before anything is sent. */
export function decisionSummary(e: QueueEntry, d: Decision): string {
  const permanent =
    'It is recorded for good: it cannot be edited later, and a wrong decision is corrected by a new record with a reason, both kept.'
  if (d.verb === 'reject') {
    return `Reject the proposal that this forecast resolved "${e.proposal.outcome}". Nothing closes: the forecast stays open and the resolver keeps reading it. ${permanent}`
  }
  const closes =
    d.outcome === 'void'
      ? 'This voids the forecast: it will not be scored.'
      : `This closes the forecast as "${d.outcome}", and it counts in the track record.`
  const how = d.verb === 'judgement' ? 'Decide on your judgement' : 'Confirm the proposal'
  return `${how}: the forecast resolved "${d.outcome}". ${closes} ${permanent}`
}

export interface DecisionResult {
  status: 'confirmed' | 'rejected' | 'decided' | 'corrected' | 'already_recorded' | string
  record_id: string
  record_kind: string
  outcome: string
  question_id: string
  slug: string
  actor_role?: string | null
  question_status?: string
}

/** What happened, stated after the route answered. */
export function decisionDone(r: DecisionResult): string {
  const who = r.actor_role ? ` as ${roleWords(r.actor_role)}` : ''
  if (r.status === 'already_recorded') {
    return `This was already on record (${r.record_kind} "${r.outcome}"); nothing new was written.`
  }
  if (r.status === 'rejected') {
    return `Rejected${who}. Nothing closed: the forecast stays open and the resolver keeps reading it.`
  }
  const verb = r.status === 'decided' ? 'Decided' : r.status === 'corrected' ? 'Corrected' : 'Confirmed'
  if (r.outcome === 'void') return `${verb}${who}: the forecast is voided and will not be scored.`
  return `${verb}${who}: the forecast resolved "${r.outcome}". It now counts in the track record.`
}

/** A refusal's own words (`{detail: {error, message}}`), or a plain fallback. */
export function refusalWords(status: number | null, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail
  if (detail && typeof detail === 'object') {
    const m = (detail as { message?: unknown }).message
    if (typeof m === 'string' && m) return m
  }
  if (typeof detail === 'string' && detail) return detail
  if (status === 403) return 'Your role cannot make this decision.'
  if (status === 409) return 'The forecast changed since you opened it; read it again.'
  return 'The decision was not recorded; nothing was changed.'
}

// ---------------------------------------------------------------------------
// F-1 — a seal's inclusion in a daily anchor, checked in the browser.
// ---------------------------------------------------------------------------

export interface PathStep {
  side: 'left' | 'right'
  hash: string
}
export interface LeafProof {
  kind: string
  id: string
  content_sha256: string
  /** The text the leaf hashes: `forecast_question:<id><hash>`. */
  leaf_input: string
  included: boolean
  leaf: string
  day?: string
  position?: number
  leaf_count?: number
  path?: PathStep[]
  root?: string
  anchor_version?: string
  timestamps?: { calendar: string; status: string; submitted_at?: string | null; has_proof?: boolean; verified_here?: boolean }[]
  first_stored_day?: string | null
  issuer_kind?: string
}
export interface SealProofResponse {
  version?: string
  available: boolean
  question_id: string
  sealed_at?: string | null
  question?: LeafProof
  numbers?: LeafProof[]
  first_stored_day?: string | null
}

export type Sha256 = (bytes: Uint8Array) => Promise<Uint8Array>

/** The browser's SHA-256 (Web Crypto). */
export const webSha256: Sha256 = async (bytes) => new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', bytes))

export function hexToBytes(hex: string): Uint8Array {
  if (!/^([0-9a-f]{2})*$/i.test(hex)) throw new Error('not hex')
  const out = new Uint8Array(hex.length / 2)
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16)
  return out
}
export function bytesToHex(b: Uint8Array): string {
  return [...b].map((x) => x.toString(16).padStart(2, '0')).join('')
}

/** Fold a leaf up its sibling path: sha256(sibling || node) for a left sibling, sha256(node || sibling) for a right one. */
export async function foldPath(leafHex: string, path: PathStep[], sha: Sha256): Promise<string> {
  let node = hexToBytes(leafHex)
  for (const step of path) {
    const sib = hexToBytes(step.hash)
    const joined = new Uint8Array(64)
    if (step.side === 'left') {
      joined.set(sib, 0)
      joined.set(node, 32)
    } else {
      joined.set(node, 0)
      joined.set(sib, 32)
    }
    node = await sha(joined)
  }
  return bytesToHex(node)
}

export interface LeafCheck {
  ok: boolean
  /** Why it failed, in words; '' when ok. */
  reason: string
}

/**
 * One leaf's proof, checked HERE: the leaf recomputed from the hash the
 * Ledger route serves (never the proof's own leaf), folded up the path, and
 * the result compared with the root the ANCHORS route lists for that day.
 */
export async function checkLeafProof(
  p: LeafProof,
  expectedInput: string,
  anchors: AnchorRow[],
  sha: Sha256,
): Promise<LeafCheck> {
  if (!p.included || !p.path || !p.root || !p.day) return { ok: false, reason: 'no stored anchor holds it' }
  try {
    const leaf = bytesToHex(await sha(new TextEncoder().encode(expectedInput)))
    if (leaf !== p.leaf.toLowerCase()) return { ok: false, reason: 'the leaf does not match this forecast’s hash' }
    const root = await foldPath(leaf, p.path, sha)
    if (root !== p.root.toLowerCase()) return { ok: false, reason: 'the path does not fold to the stated root' }
    const listed = anchors.filter((a) => a.day === p.day)
    if (listed.length === 0) return { ok: false, reason: `the anchors list has no anchor for ${p.day}` }
    if (!listed.every((a) => a.root_hash.toLowerCase() === root)) {
      return { ok: false, reason: `the root does not match the anchor listed for ${p.day}` }
    }
    return { ok: true, reason: '' }
  } catch {
    return { ok: false, reason: 'the proof could not be read' }
  }
}

export type SealProofState = 'reading' | 'unavailable' | 'not_stored' | 'included' | 'failed'

export interface SealProof {
  state: SealProofState
  text: string
  day: string | null
  /** Numbers whose own leaves checked, of those stated. */
  numbersChecked: number
  numbersTotal: number
}

/**
 * The seal's inclusion, in words — "Included in the anchor of <day>" ONLY
 * when the question's path checks in the browser against the anchors route's
 * root. The outside timestamp on that root is never verified here.
 */
export async function sealProofCheck(
  f: Pick<LedgerForecast, 'id' | 'content_sha256' | 'sealed_at'>,
  proof: SealProofResponse | null | undefined,
  anchors: AnchorRow[] | null | undefined,
  sha: Sha256 = webSha256,
): Promise<SealProof> {
  const none = { day: null, numbersChecked: 0, numbersTotal: 0 }
  if (proof === undefined || anchors === undefined) return { state: 'reading', text: 'Checking the inclusion path…', ...none }
  if (proof === null || !proof.available || !proof.question || !f.content_sha256 || anchors === null) {
    return { state: 'unavailable', text: 'No inclusion proof could be read; only the anchor coverage above is known.', ...none }
  }
  const q = proof.question
  if (!q.included) {
    const since = q.first_stored_day ?? proof.first_stored_day ?? null
    return {
      state: 'not_stored',
      text: since
        ? `No stored anchor holds this seal yet. Anchors keep their leaves from ${since}; before that a seal was anchored by the day's root only, so inclusion cannot be shown for it.`
        : 'No anchor has kept its leaves yet: until one does, a seal is anchored by the day’s root only and inclusion cannot be shown here.',
      ...none,
    }
  }
  const check = await checkLeafProof(q, `forecast_question:${f.id}${f.content_sha256}`, anchors, sha)
  if (!check.ok) {
    return {
      state: 'failed',
      text: `The inclusion path did not check: ${check.reason}. Do not rely on this seal’s anchoring until it does.`,
      day: q.day ?? null,
      numbersChecked: 0,
      numbersTotal: proof.numbers?.length ?? 0,
    }
  }
  const nums = proof.numbers ?? []
  let checked = 0
  for (const n of nums) {
    if ((await checkLeafProof(n, n.leaf_input, anchors, sha)).ok && n.leaf_input === `forecast_number:${n.id}${n.content_sha256}`) checked++
  }
  const submitted = anchors.filter((a) => a.day === q.day && a.status === 'submitted').length
  const ts = submitted
    ? `That root was submitted to ${countWord(submitted)} public timestamp calendar${submitted === 1 ? '' : 's'}; the timestamp proof is checked offline with the OpenTimestamps client.`
    : 'That root has not reached a public timestamp calendar yet.'
  const sealedDay = day(f.sealed_at ?? null)
  const earlier =
    f.sealed_at && q.day && sealedDay < q.day
      ? ` It was sealed on ${sealedDay}; the anchors before ${q.day} kept their roots only.`
      : ''
  const numbersText =
    nums.length === 0
      ? ''
      : nums.length === 1
        ? checked === 1
          ? ' Its stated number checked the same way.'
          : ' Its stated number did not check.'
        : checked === nums.length
          ? ` All ${countWord(nums.length)} stated numbers checked the same way.`
          : ` ${countWord(checked)} of its ${countWord(nums.length)} stated numbers checked the same way.`
  return {
    state: 'included',
    text: `Included in the anchor of ${q.day}: the path from this seal to that day's root checks in your browser.${numbersText}${earlier} ${ts}`,
    day: q.day ?? null,
    numbersChecked: checked,
    numbersTotal: nums.length,
  }
}
