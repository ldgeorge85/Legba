/**
 * consultContract — the console side of Q-1's TURN CONTRACT (U-8).
 *
 * The server (`src/legba/data/analysts/consult_contract.py`) resolves, for
 * every armed consult turn, ONE window in force, a grounding mode, a coverage
 * read by day, the typed competing explanations, the grounding accounting and
 * — when the per-turn cost ceiling bit — a cost-stop line, and returns them as
 * `contract` (`consult_contract/1`) on the response and on the turn's
 * persisted response bag.
 *
 * Two halves, both pure:
 *
 *   * SEND — what the user chose: {@link windowForRequest} builds the request's
 *     `window` field (the server's `ConsultRequest.window`: `{hours, end}` or
 *     `{start, end}`, ISO instants) and {@link GROUNDING_CHOICES} its
 *     `grounding_mode` (`'record_only' | 'default'`). The default window is
 *     {@link DEFAULT_WINDOW_HOURS} — the server's own declared default
 *     (`consult_window.DEFAULT_WINDOW_HOURS`), so choosing nothing and choosing
 *     the default ask the same question.
 *   * READ — {@link parseTurnContract} lifts the wire object into typed parts,
 *     each `null` when the server sent none. Absent is absent: the renderer
 *     draws no box for a part that is not there, and a count the server did
 *     not measure is never printed as 0.
 */

export const CONTRACT_SCHEMA = 'consult_contract/1'

// ---------------------------------------------------------------------------
// SEND
// ---------------------------------------------------------------------------

/** The server's declared default window (hours). */
export const DEFAULT_WINDOW_HOURS = 168

export type WindowChoiceId = 'band' | '24h' | '168h' | '720h'

export interface WindowChoice {
  id: WindowChoiceId
  label: string
  /** Fixed spans only; `band` follows the console's time band. */
  hours: number | null
}

export const WINDOW_CHOICES: readonly WindowChoice[] = [
  { id: '24h', label: 'last 24 h', hours: 24 },
  { id: '168h', label: 'last 7 days', hours: 168 },
  { id: '720h', label: 'last 30 days', hours: 720 },
  { id: 'band', label: 'the time band', hours: null },
]

export const DEFAULT_WINDOW_CHOICE: WindowChoiceId = '168h'

export function isWindowChoiceId(v: unknown): v is WindowChoiceId {
  return typeof v === 'string' && WINDOW_CHOICES.some((c) => c.id === v)
}

export type GroundingMode = 'default' | 'record_only'

export const GROUNDING_CHOICES: readonly { id: GroundingMode; label: string; hint: string }[] = [
  {
    id: 'default',
    label: 'Cite rows',
    hint: 'facts cite rows; an uncited sentence is counted in the turn’s accounting, not cut',
  },
  {
    id: 'record_only',
    label: 'Record only',
    hint: 'every sentence about the world must rest on a row a tool returned; the platform cuts or labels the rest after the answer is written',
  },
]

export const DEFAULT_GROUNDING: GroundingMode = 'default'

export function isGroundingMode(v: unknown): v is GroundingMode {
  return v === 'default' || v === 'record_only'
}

/** The request's `window` field (the server reads `start`/`hours`, `end` and
 *  an optional `label` it prints as the window's basis; at most four keys). */
export type RequestWindow =
  | { hours: number; end: string; label: string }
  | { start: string; end: string; label: string }

/**
 * The `window` a turn is sent with. A fixed span ends NOW (the turn's own
 * instant); `band` sends the time band's absolute ends. A band that cannot be
 * read (non-finite, or not forward) falls back to the default span rather than
 * sending a window the server would reject.
 */
export function windowForRequest(
  choice: WindowChoiceId,
  band: { startMs: number; endMs: number },
  nowMs: number = Date.now(),
): RequestWindow {
  if (choice === 'band') {
    if (Number.isFinite(band.startMs) && Number.isFinite(band.endMs) && band.endMs > band.startMs) {
      return { start: new Date(band.startMs).toISOString(), end: new Date(band.endMs).toISOString(), label: 'console time band' }
    }
    return { hours: DEFAULT_WINDOW_HOURS, end: new Date(nowMs).toISOString(), label: `console ${DEFAULT_WINDOW_HOURS}h` }
  }
  const hours = WINDOW_CHOICES.find((c) => c.id === choice)?.hours ?? DEFAULT_WINDOW_HOURS
  return { hours, end: new Date(nowMs).toISOString(), label: `console ${hours}h` }
}

/**
 * The time band's window in short words ("24h", "7d", "70h") — what Consult
 * shows, read-only, for the window its next turn is sent with. Measured from
 * the window's own ends, so a brushed window reads as what it is.
 */
export function bandWindowWords(startMs: number, endMs: number): string {
  const ms = endMs - startMs
  if (!Number.isFinite(ms) || ms <= 0) return `${DEFAULT_WINDOW_HOURS / 24}d`
  const h = Math.round(ms / 3_600_000)
  if (h < 48) return `${Math.max(1, h)}h`
  const d = ms / 86_400_000
  return Number.isInteger(Math.round(d * 10) / 10) ? `${Math.round(d)}d` : `${h}h`
}

/** The short words the point line prints for the window a turn will be sent with. */
export function windowChoiceWords(choice: WindowChoiceId, bandSpanText: string): string {
  if (choice === 'band') return `${bandSpanText} (time band)`
  const c = WINDOW_CHOICES.find((x) => x.id === choice)
  return c?.hours != null ? (c.hours % 24 === 0 && c.hours >= 48 ? `${c.hours / 24}d` : `${c.hours}h`) : bandSpanText
}

// ---------------------------------------------------------------------------
// READ
// ---------------------------------------------------------------------------

export interface ContractWindow {
  start: string
  end: string
  hours: number | null
  /** `user_words` | `console` | `default`. */
  source: string
  basis: string | null
  deictic: boolean
  consoleRejected: string | null
  /** Q-2 — how the asker's words were read (a numeric date, bare ordinals,
   *  early/mid/late), so a wrong reading can be corrected. */
  reading: string | null
  /** Q-2 — an event anchor the question named ("since the ceasefire"). */
  anchor: ContractAnchor | null
}

export interface ContractAnchor {
  words: string
  resolved: boolean
  /** The date the asker's own words gave (resolved anchors only). */
  date: string | null
  /** Why it was not resolved. */
  reason: string | null
}

/** Q-2 — where a RECOVERED turn's contract terms came from. */
export interface ContractRecovery {
  windowFrom: string | null
  coverageFrom: string | null
  originalCostStop: boolean
}

export interface ContractReach {
  state: 'measured' | 'unmeasured'
  citedDated: number | null
  inWindow: number | null
  beforeWindow: number | null
  afterWindow: number | null
  narrowed: string | null
  narrowedReason: string | null
  widerReach: string | null
  widerReason: string | null
}

export interface CoverageDay {
  day: string
  signals: number
  sources: number
  findings: number
  events: number
}

export interface ContractCoverage {
  state: 'measured' | 'unavailable'
  reason: string | null
  days: CoverageDay[]
  zeroDays: string[]
  sparseDays: string[]
  sparseRule: string | null
  pausedSources: { id: string; name: string }[]
  errorSources: { id: string; errors: number }[]
  subjectTargets: string[] | null
  clippedToDays: number | null
}

export interface ContractHypothesis {
  explanation: string
  p: number | null
  forRefs: string[]
  againstRefs: string[]
  observable: string | null
  by: string | null
  pObservable: number | null
}

export interface ContractHypotheses {
  conforming: boolean | null
  repair: string | null
  problems: string[]
  items: ContractHypothesis[]
  somethingElseP: number | null
}

export interface ContractGrounding {
  mode: string
  applied: boolean
  notAppliedReason: string | null
  counts: Record<string, number>
  numbersUnsupported: number | null
  /** Q-2 — cited rows the number check read only in part, and the characters
   *  of them it did not read; `null` when the server did not say. */
  numberRowsClipped: number | null
  numberCharsUnchecked: number | null
  verifierState: string | null
  verifierItems: number | null
}

export interface TurnContract {
  schema: string
  armed: boolean
  groundingMode: string | null
  groundingSource: string | null
  window: ContractWindow | null
  reach: ContractReach | null
  coverage: ContractCoverage | null
  hypotheses: ContractHypotheses | null
  grounding: ContractGrounding | null
  /** The server's "Stopped for cost" line — the answer is a PARTIAL synthesis. */
  costStop: string | null
  /** Q-2 — set on a recovered ("synthesize from evidence") turn. */
  recovery: ContractRecovery | null
}

type Obj = Record<string, unknown>

const isObj = (v: unknown): v is Obj => !!v && typeof v === 'object' && !Array.isArray(v)
const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null)
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [])

function refText(v: unknown): string | null {
  if (typeof v === 'string') return v || null
  if (isObj(v) && typeof v.id === 'string') return typeof v.kind === 'string' ? `${v.kind}:${v.id}` : v.id
  return null
}

function parseWindow(v: unknown): ContractWindow | null {
  if (!isObj(v) || !str(v.start) || !str(v.end)) return null
  return {
    start: v.start as string,
    end: v.end as string,
    hours: num(v.hours),
    source: str(v.source) ?? 'unstated',
    basis: str(v.basis),
    deictic: v.deictic === true,
    consoleRejected: str(v.console_rejected),
    reading: str(v.reading),
    anchor:
      isObj(v.anchor) && str(v.anchor.words)
        ? {
            words: v.anchor.words as string,
            resolved: v.anchor.resolved === true,
            date: str(v.anchor.date),
            reason: str(v.anchor.reason),
          }
        : null,
  }
}

function parseRecovery(v: unknown): ContractRecovery | null {
  if (!isObj(v)) return null
  return {
    windowFrom: str(v.window_from),
    coverageFrom: str(v.coverage_from),
    originalCostStop: !!str(v.original_cost_stop),
  }
}

function parseReach(v: unknown): ContractReach | null {
  if (!isObj(v)) return null
  if (v.state === 'unmeasured') {
    return {
      state: 'unmeasured', citedDated: null, inWindow: null, beforeWindow: null, afterWindow: null,
      narrowed: null, narrowedReason: null, widerReach: null, widerReason: null,
    }
  }
  return {
    state: 'measured',
    citedDated: num(v.cited_dated),
    inWindow: num(v.in_window),
    beforeWindow: num(v.before_window),
    afterWindow: num(v.after_window),
    narrowed: str(v.narrowed),
    narrowedReason: str(v.narrowed_reason),
    widerReach: str(v.wider_reach),
    widerReason: str(v.wider_reason),
  }
}

function parseCoverage(v: unknown): ContractCoverage | null {
  if (!isObj(v)) return null
  const gaps = isObj(v.gaps) ? v.gaps : {}
  const subject = isObj(v.subject_filter) ? strs(v.subject_filter.target_ids) : null
  if (v.state !== 'measured') {
    return {
      state: 'unavailable', reason: str(v.reason) ?? 'not measured', days: [], zeroDays: [], sparseDays: [],
      sparseRule: null, pausedSources: [], errorSources: [], subjectTargets: subject, clippedToDays: null,
    }
  }
  const days: CoverageDay[] = []
  for (const d of Array.isArray(v.days) ? v.days : []) {
    // A day row missing a count is unreadable and dropped — never a zero.
    if (!isObj(d) || !str(d.day)) continue
    const [signals, sources, findings, events] = [num(d.signals), num(d.sources), num(d.findings), num(d.events)]
    if (signals == null || sources == null || findings == null || events == null) continue
    days.push({ day: d.day as string, signals, sources, findings, events })
  }
  return {
    state: 'measured',
    reason: null,
    days,
    zeroDays: strs(v.zero_days),
    sparseDays: strs(v.sparse_days),
    sparseRule: str(v.sparse_rule),
    pausedSources: (Array.isArray(gaps.paused_sources) ? gaps.paused_sources : [])
      .filter(isObj)
      .map((p) => ({ id: String(p.id ?? ''), name: String(p.name ?? '') })),
    errorSources: (Array.isArray(gaps.error_sources) ? gaps.error_sources : [])
      .filter(isObj)
      .filter((e) => num(e.errors) != null)
      .map((e) => ({ id: String(e.id ?? ''), errors: num(e.errors) as number })),
    subjectTargets: subject && subject.length ? subject : null,
    clippedToDays: num(v.clipped_to_days),
  }
}

function parseHypotheses(v: unknown): ContractHypotheses | null {
  if (!isObj(v)) return null
  const items: ContractHypothesis[] = []
  for (const i of Array.isArray(v.items) ? v.items : []) {
    if (!isObj(i)) continue
    const test = isObj(i.test) ? i.test : {}
    items.push({
      explanation: String(i.explanation ?? '').trim(),
      p: num(i.p),
      forRefs: (Array.isArray(i.for) ? i.for : []).map(refText).filter((x): x is string => !!x),
      againstRefs: (Array.isArray(i.against) ? i.against : []).map(refText).filter((x): x is string => !!x),
      observable: str(test.observable),
      by: str(test.by),
      pObservable: num(test.p_observable),
    })
  }
  return {
    conforming: typeof v.conforming === 'boolean' ? v.conforming : null,
    repair: str(v.repair),
    problems: strs(v.problems),
    items,
    somethingElseP: num(v.something_else_p),
  }
}

function parseGrounding(v: unknown): ContractGrounding | null {
  if (!isObj(v)) return null
  const counts: Record<string, number> = {}
  if (isObj(v.counts)) for (const [k, n] of Object.entries(v.counts)) if (typeof n === 'number') counts[k] = n
  const verifier = isObj(v.verifier) ? v.verifier : null
  const nc = isObj(v.number_check) ? v.number_check : null
  return {
    mode: str(v.mode) ?? 'unstated',
    applied: v.applied === true,
    notAppliedReason: str(v.not_applied_reason),
    counts,
    numbersUnsupported: num(v.numbers_unsupported),
    numberRowsClipped: nc ? num(nc.rows_clipped) : null,
    numberCharsUnchecked: nc ? num(nc.chars_unchecked) : null,
    verifierState: verifier ? str(verifier.state) : null,
    verifierItems: verifier && Array.isArray(verifier.items) ? verifier.items.length : null,
  }
}

/** The response's `contract`, typed — `null` when the turn carried none. */
export function parseTurnContract(raw: unknown): TurnContract | null {
  if (!isObj(raw)) return null
  return {
    schema: str(raw.schema) ?? CONTRACT_SCHEMA,
    armed: raw.armed === true,
    groundingMode: str(raw.grounding_mode),
    groundingSource: str(raw.grounding_source),
    window: parseWindow(raw.window),
    reach: parseReach(raw.reach),
    coverage: parseCoverage(raw.coverage),
    hypotheses: parseHypotheses(raw.hypotheses),
    grounding: parseGrounding(raw.grounding),
    costStop: str(raw.cost_stop),
    recovery: parseRecovery(raw.recovery),
  }
}

/** Sum of a grounding action family (`cut`, `labelled`, `kept`, `moved`). */
export function groundingFamily(g: ContractGrounding, family: 'cut' | 'labelled' | 'kept' | 'moved'): number {
  let n = 0
  for (const [k, v] of Object.entries(g.counts)) if (k.startsWith(family)) n += v
  return n
}

// ---------------------------------------------------------------------------
// The choice, remembered per viewer (a convenience: storage may be absent).
// ---------------------------------------------------------------------------

const GROUNDING_KEY = 'legba_consult_grounding'

export function loadGroundingMode(): GroundingMode {
  try {
    const v = localStorage.getItem(GROUNDING_KEY)
    return isGroundingMode(v) ? v : DEFAULT_GROUNDING
  } catch {
    return DEFAULT_GROUNDING
  }
}

export function saveGroundingMode(v: GroundingMode): void {
  try {
    localStorage.setItem(GROUNDING_KEY, v)
  } catch {
    // as above
  }
}
