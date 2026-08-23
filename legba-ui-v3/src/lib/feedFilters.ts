/**
 * feedFilters — the ONE typed-facet filter model for the unified Live Feed
 * (S7-T4 feed reform). Pure + DOM-free so it is unit-tested and reused by both
 * the FeedFilterBar UI and the panel's row-filter pass.
 *
 * A filter is a set of typed `key:value` CHIPS plus a free-text run:
 *
 *   severity:high verified:true country:iran kind:finding analyst:escalation last:7d coup
 *   └──────────── chips (AND-combined facets) ────────────────────────────┘ └ free text
 *
 * Verification/faithfulness is a FIRST-CLASS facet: `verified:true|false`,
 * `judge:llm|deterministic|unsampled` (how the verify pass ran — the unsampled
 * stratum is one chip, not a client-side sieve) and
 * `confidence:high|moderate|low|unverified` filter on the SAME two-axis
 * `verdictModel` vocabulary the VerdictBadge renders (ICD-203), so "high-severity
 * VERIFIED findings for Iran, last 7d" is one line of chips. `verified:` and
 * `judge:` push server-side (GLASS-1 — `/findings?verified=&judge_status=`), so
 * they filter the whole corpus, not just the fetched page.
 *
 * The whole filter round-trips to a single string (`serializeFilter`) so it
 * serializes to a saved view AND to the `#view=` URL hash (addressability with
 * no router — the S7-T2 shareable-state pattern).
 */

import { surfacedConfidence, type UnifiedRow } from './findingsViews'
import { buildVerdict, type VerdictInput, type Verdict, type VerificationBlock } from './verdictModel'

// ---------------------------------------------------------------------------
// Facet vocabulary
// ---------------------------------------------------------------------------

/** The canonical facet keys the feed understands. */
export const FACET_KEYS = [
  'severity',
  'verified',
  'judge',
  'confidence',
  'likelihood',
  'target',
  'kind',
  'analyst',
  'since',
  'minconf',
] as const
export type FacetKey = (typeof FACET_KEYS)[number]

/** Human-typed aliases → canonical facet key. `country`→`target`, `last`→`since`. */
const FACET_ALIASES: Record<string, FacetKey> = {
  severity: 'severity',
  sev: 'severity',
  verified: 'verified',
  verify: 'verified',
  // How the verify pass ran (`judge:llm|deterministic|unsampled`) — the
  // verdict model's judgeStatus axis, incl. the honest J2 unsampled state.
  judge: 'judge',
  judge_status: 'judge',
  confidence: 'confidence',
  conf: 'confidence',
  likelihood: 'likelihood',
  target: 'target',
  target_id: 'target',
  country: 'target',
  desk: 'target',
  kind: 'kind',
  analyst: 'analyst',
  analyst_id: 'analyst',
  unit: 'analyst',
  producer: 'analyst',
  since: 'since',
  last: 'since',
  within: 'since',
  // The surfaced-confidence BAND gate (`minconf:0.5` — "only rows whose
  // effective confidence clears the 0.50 floor").
  minconf: 'minconf',
  min_confidence: 'minconf',
  mineff: 'minconf',
  floor: 'minconf',
}

/** One typed facet chip. `value` is always lower-cased. */
export interface FeedChip {
  key: FacetKey
  value: string
}

/** A parsed filter: the AND-combined facet chips + the residual free text. */
export interface ParsedFilter {
  chips: FeedChip[]
  text: string
}

export const EMPTY_FILTER: ParsedFilter = { chips: [], text: '' }

/** Resolve a raw `key` token to its canonical facet, or null if unknown. */
export function resolveFacetKey(raw: string): FacetKey | null {
  return FACET_ALIASES[raw.trim().toLowerCase()] ?? null
}

/** Dedupe chips: a later chip of the same (key,value) is dropped; for the
 *  single-valued facets (severity/verified/confidence/likelihood/since/target)
 *  a later value REPLACES the earlier (last-wins) so a dropdown re-pick swaps
 *  cleanly. `kind`/`analyst` may repeat (OR within, AND across is not modelled —
 *  kept simple: last-wins too). */
const SINGLE_VALUED: ReadonlySet<FacetKey> = new Set<FacetKey>([
  'severity',
  'verified',
  'judge',
  'confidence',
  'likelihood',
  'since',
  'target',
  'kind',
  'analyst',
  'minconf',
])

export function mergeChips(existing: FeedChip[], incoming: FeedChip[]): FeedChip[] {
  const out = [...existing]
  for (const chip of incoming) {
    if (SINGLE_VALUED.has(chip.key)) {
      const idx = out.findIndex((c) => c.key === chip.key)
      if (idx >= 0) out[idx] = chip
      else out.push(chip)
    } else if (!out.some((c) => c.key === chip.key && c.value === chip.value)) {
      out.push(chip)
    }
  }
  return out
}

/** Set (or, with an empty value, CLEAR) a single-valued facet chip. */
export function setChip(chips: FeedChip[], key: FacetKey, value: string): FeedChip[] {
  const without = chips.filter((c) => c.key !== key)
  return value ? mergeChips(without, [{ key, value: value.toLowerCase() }]) : without
}

/** Remove one chip by (key,value). */
export function removeChip(chips: FeedChip[], key: FacetKey, value: string): FeedChip[] {
  return chips.filter((c) => !(c.key === key && c.value === value))
}

/** The current value of a single-valued facet, or '' when unset. */
export function chipValue(chips: FeedChip[], key: FacetKey): string {
  return chips.find((c) => c.key === key)?.value ?? ''
}

// ---------------------------------------------------------------------------
// Parse / serialize
// ---------------------------------------------------------------------------

/**
 * Parse a raw filter string into chips + free text. A whitespace-delimited token
 * shaped `<key>:<value>` whose key resolves to a known facet becomes a chip;
 * every other token joins the free text. Idempotent: `serializeFilter` of the
 * result re-parses identically.
 */
export function parseFilterInput(input: string): ParsedFilter {
  const chips: FeedChip[] = []
  const textParts: string[] = []
  for (const raw of input.split(/\s+/)) {
    if (!raw) continue
    const m = /^([a-zA-Z_]+):(.+)$/.exec(raw)
    if (m) {
      const key = resolveFacetKey(m[1])
      if (key) {
        chips.push({ key, value: m[2].toLowerCase() })
        continue
      }
    }
    textParts.push(raw)
  }
  return { chips: mergeChips([], chips), text: textParts.join(' ') }
}

/** Serialize a filter back to a single canonical string (chips first, then text). */
export function serializeFilter(f: ParsedFilter): string {
  const chipStr = f.chips.map((c) => `${c.key}:${c.value}`).join(' ')
  return [chipStr, f.text.trim()].filter(Boolean).join(' ').trim()
}

// ---------------------------------------------------------------------------
// Time windows (`since:` / `last:`)
// ---------------------------------------------------------------------------

const SINCE_UNIT_MS: Record<string, number> = {
  m: 60_000,
  h: 3_600_000,
  d: 86_400_000,
  w: 604_800_000,
  mo: 2_592_000_000, // 30d
  y: 31_536_000_000,
}

/** Parse a relative window (`7d`, `24h`, `30m`, `1w`, `2mo`) to milliseconds, or
 *  null when it isn't a recognized window. */
export function parseSince(value: string): number | null {
  const m = /^(\d+)\s*(mo|[mhdwy])$/.exec(value.trim().toLowerCase())
  if (!m) return null
  const n = Number(m[1])
  const unit = SINCE_UNIT_MS[m[2]]
  if (!Number.isFinite(n) || n <= 0 || !unit) return null
  return n * unit
}

// ---------------------------------------------------------------------------
// Verdict derivation (the verification facet source of truth)
// ---------------------------------------------------------------------------

/**
 * Derive the two-axis {@link Verdict} for a feed row from the SAME inputs the
 * VerdictBadge uses — the row's probability (`confidence`/`effective_confidence`)
 * + its faithfulness-verify block + its resolved citation breadth. One honest
 * source so a `verified:true` chip and the row's badge always agree.
 */
export function deriveRowVerdict(row: UnifiedRow, citationCount: number): Verdict {
  const input: VerdictInput = {
    confidence: row.confidence,
    effectiveConfidence: row.effective_confidence,
    verification: (row.verification as VerificationBlock | null) ?? null,
    citationCount,
    // P0-4 — classify verify-exempt structural rows (server stamp when the
    // row came over REST; analyst_id registry mirror for live-tail rows) so
    // the feed badge renders `unverified — structural`, never a quiet blank.
    analystId: row.analyst_id,
    verifyExempt: row.verify_exempt ?? null,
  }
  return buildVerdict(input)
}

/** True once the row carries a real faithfulness-verify pass (confidence axis
 *  is not `unassessed`) — the meaning of the `verified:true` facet. */
export function isVerified(verdict: Verdict): boolean {
  return verdict.confidence !== 'unassessed'
}

// ---------------------------------------------------------------------------
// Row matching
// ---------------------------------------------------------------------------

/** Normalize a likelihood band / confidence level for chip comparison (spaces →
 *  hyphens, lower-case) so `likelihood:very-likely` matches "very likely". */
function norm(s: string): string {
  return s.trim().toLowerCase().replace(/[\s_]+/g, '-')
}

function matchChip(row: UnifiedRow, verdict: Verdict, chip: FeedChip, now: number): boolean {
  const v = chip.value
  switch (chip.key) {
    case 'severity':
      if (v === 'none') return row.severity == null
      return (row.severity ?? '').toLowerCase() === v
    case 'verified':
      return (v === 'true' || v === 'yes') ? isVerified(verdict) : !isVerified(verdict)
    case 'judge':
      // Exact match on how the verify pass ran (llm / deterministic /
      // unsampled). A row with no judge_status (unverified, structural, a
      // legacy block) matches NO value — same rule the server facet enforces.
      return (verdict.judgeStatus ?? '').toLowerCase() === v
    case 'confidence': {
      // `unverified` is an alias for the honest `unassessed` level.
      const want = v === 'unverified' ? 'unassessed' : v
      return verdict.confidence === want
    }
    case 'likelihood':
      return norm(verdict.likelihood).startsWith(norm(v))
    case 'target':
      return (row.target_id ?? '').toLowerCase().includes(v)
    case 'kind':
      // kind:signal / kind:finding gate on the stream discriminant; any other
      // value gates on the substrate OutputKind.
      if (v === 'signal' || v === 'finding') return row.source === v
      return (row.kind ?? '').toLowerCase() === v
    case 'analyst':
      return (row.analyst_id ?? '').toLowerCase().includes(v)
    case 'since': {
      const windowMs = parseSince(v)
      if (windowMs == null) return true // an unparseable window never over-filters
      const t = Date.parse(row.produced_at)
      return Number.isFinite(t) && t >= now - windowMs
    }
    case 'minconf': {
      const min = Number(v)
      if (!Number.isFinite(min)) return true // garbage threshold never over-filters
      // The SURFACED confidence — the critic-folded `effective_confidence` when
      // the row has one, else the raw `confidence` (findingsViews is the single
      // source of that rule). A row that was never graded has NO surfaced
      // confidence and therefore cannot clear a floor: it fails the gate rather
      // than being fabricated over it.
      const c = surfacedConfidence(row)
      return c !== null && c >= min
    }
    default:
      return true
  }
}

/** Free-text haystack for a row — title, body preview, ids, source, tags. */
function rowHaystack(row: UnifiedRow): string {
  return [
    row.title,
    row.body,
    row.target_id,
    row.analyst_id,
    row.source_id,
    (row.tags ?? []).join(' '),
    (row.geo ?? []).join(' '),
  ]
    .filter((s): s is string => typeof s === 'string' && s.length > 0)
    .join(' ')
    .toLowerCase()
}

/**
 * True when a row satisfies the whole filter: every chip matches (AND) and every
 * free-text WORD is found in the row's haystack (AND). An empty filter matches
 * everything. `verdict` is passed in (precomputed once per row) so the caller
 * doesn't rebuild it per chip.
 */
export function matchesFilter(
  row: UnifiedRow,
  verdict: Verdict,
  filter: ParsedFilter,
  now: number = Date.now(),
): boolean {
  for (const chip of filter.chips) {
    if (!matchChip(row, verdict, chip, now)) return false
  }
  const text = filter.text.trim().toLowerCase()
  if (text) {
    const hay = rowHaystack(row)
    for (const word of text.split(/\s+/)) {
      if (word && !hay.includes(word)) return false
    }
  }
  return true
}

// ---------------------------------------------------------------------------
// Server-side push — which facets the REST routes can answer themselves
// ---------------------------------------------------------------------------

/**
 * The query params `GET /findings` accepts that map onto a feed facet
 * (`substrate_reads_api.list_findings`). `verified`/`judge_status` are the
 * GLASS-1 verification facet — the server filters over the SAME surfaced
 * verification block the verdict reads, so the facet reaches the whole corpus
 * and the page fill / next_cursor count the FILTERED population. `q` is
 * deliberately ABSENT: the route's `plainto_tsquery('simple', …)` matches whole
 * TOKENS, so pushing the free-text box server-side would blank the list on
 * every partial word the operator types ("cou" → 0 rows). Free text therefore
 * stays a client-side substring match over the loaded page, exactly as before.
 */
export const FINDINGS_SERVER_FACETS: readonly string[] = [
  'target_id',
  'analyst_id',
  'severity',
  'verified',
  'judge_status',
  'since',
]

/** The subset `GET /signals` accepts (no analyst/severity/verify columns on a
 *  signal — raw intake is never verify-assessed). */
export const SIGNALS_SERVER_FACETS: readonly string[] = ['target_id', 'since']

/** The four real severities the route's `Severity` enum accepts. `severity:none`
 *  ("has no severity") has no server equivalent and stays client-side. */
const SERVER_SEVERITIES: ReadonlySet<string> = new Set(['low', 'medium', 'high', 'critical'])

/** The route's `JudgeStatus` enum — the three ways a verify pass runs, with the
 *  J2 `unsampled` state first-class. A hand-typed other value stays client-side
 *  (where it matches nothing, exactly as the server would return nothing — but
 *  pushing it would 422 the whole page instead). */
const SERVER_JUDGE_STATUSES: ReadonlySet<string> = new Set([
  'llm',
  'deterministic',
  'unsampled',
])

export interface ServerFilterOptions {
  /** Which params the target route accepts — {@link FINDINGS_SERVER_FACETS} or
   *  {@link SIGNALS_SERVER_FACETS}. */
  supports: readonly string[]
  /**
   * Target ids the feed KNOWS exist verbatim (the desk roster + every
   * `target_id` in view). `target:` is a client-side SUBSTRING match, but the
   * route's `target_id=` is exact — pushing a hand-typed partial would return
   * an empty page and read as "no results" when the truth is "not an id". So a
   * value is only pushed when it is a known-exact id.
   */
  exactTargets?: ReadonlySet<string>
  /** Same contract for `analyst:` — see `feedProducers.exactProducerIds`. */
  exactAnalysts?: ReadonlySet<string>
  now?: number
}

/**
 * The server-side half of a filter: the facets the REST route can answer over
 * the WHOLE corpus instead of the 50-row page the client can see.
 *
 * Every param returned here is a strict NARROWING that `matchesFilter` also
 * enforces client-side, so the two passes can never disagree — the server pass
 * is a pure optimization that makes a facet reach past the current page, and
 * dropping any param (unknown id, unparseable window) only ever widens the
 * query, never hides a row.
 */
export function serverFilterParams(
  filter: ParsedFilter,
  opts: ServerFilterOptions,
): Record<string, string> {
  const supports = new Set(opts.supports)
  const now = opts.now ?? Date.now()
  const out: Record<string, string> = {}

  const target = chipValue(filter.chips, 'target')
  if (supports.has('target_id') && target && opts.exactTargets?.has(target)) {
    out.target_id = target
  }

  const analyst = chipValue(filter.chips, 'analyst')
  if (supports.has('analyst_id') && analyst && opts.exactAnalysts?.has(analyst)) {
    out.analyst_id = analyst
  }

  const severity = chipValue(filter.chips, 'severity')
  if (supports.has('severity') && SERVER_SEVERITIES.has(severity)) {
    out.severity = severity
  }

  const verified = chipValue(filter.chips, 'verified')
  if (supports.has('verified') && verified) {
    // Mirror `matchChip` exactly: 'true'/'yes' means verified; ANY other value
    // means "not verified" — so it pushes as false, and the two passes agree.
    out.verified = verified === 'true' || verified === 'yes' ? 'true' : 'false'
  }

  const judge = chipValue(filter.chips, 'judge')
  if (supports.has('judge_status') && SERVER_JUDGE_STATUSES.has(judge)) {
    out.judge_status = judge
  }

  const since = chipValue(filter.chips, 'since')
  if (supports.has('since') && since) {
    const windowMs = parseSince(since)
    if (windowMs != null) out.since = new Date(now - windowMs).toISOString()
  }

  return out
}

// ---------------------------------------------------------------------------
// Feed view state — stream + sort + filter, serialized for saved views + hash
// ---------------------------------------------------------------------------

/** The two HARD-SEPARATED streams. Findings (finished intelligence) is primary;
 *  signals (raw intake) never interleave with it. */
export type FeedStream = 'intelligence' | 'signals'
export type FeedSort = 'recency' | 'severity' | 'confidence'

/** The full addressable feed view. */
export interface FeedViewState {
  stream: FeedStream
  sort: FeedSort
  /** The filter, as its canonical serialized string. */
  query: string
}

export interface FeedSavedView extends FeedViewState {
  name: string
}

export const DEFAULT_VIEW: FeedViewState = {
  stream: 'intelligence',
  sort: 'recency',
  query: '',
}

function isStream(v: unknown): v is FeedStream {
  return v === 'intelligence' || v === 'signals'
}
function isSort(v: unknown): v is FeedSort {
  return v === 'recency' || v === 'severity' || v === 'confidence'
}

/** Parse a serialized view-state blob defensively; returns null on any garbage. */
export function parseViewState(raw: string | null | undefined): FeedViewState | null {
  if (!raw) return null
  try {
    const o = JSON.parse(raw) as Record<string, unknown>
    if (!o || typeof o !== 'object') return null
    const stream = isStream(o.s) ? o.s : isStream(o.stream) ? o.stream : 'intelligence'
    const sort = isSort(o.o) ? o.o : isSort(o.sort) ? o.sort : 'recency'
    const query = typeof o.q === 'string' ? o.q : typeof o.query === 'string' ? o.query : ''
    return { stream, sort, query }
  } catch {
    return null
  }
}

/** Serialize a view-state to a compact blob (short keys) for the hash / storage. */
export function serializeViewState(v: FeedViewState): string {
  return JSON.stringify({ s: v.stream, o: v.sort, q: v.query })
}

// ---------------------------------------------------------------------------
// #view= URL hash (addressability without a router — coexists with #sel=)
// ---------------------------------------------------------------------------

/** Read the `#view=` param from the current hash, or null. */
export function readViewHash(): FeedViewState | null {
  try {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
    return parseViewState(params.get('view'))
  } catch {
    return null
  }
}

/** Write (or, with null, clear) the `#view=` param WITHOUT touching `#sel=` or
 *  adding a history entry — mirrors shareState's non-clobbering hash write. */
export function writeViewHash(v: FeedViewState | null): void {
  try {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
    // A default view carries no information — drop the param so a pristine feed
    // leaves a clean URL.
    if (v && (v.stream !== 'intelligence' || v.sort !== 'recency' || v.query.trim() !== '')) {
      params.set('view', serializeViewState(v))
    } else {
      params.delete('view')
    }
    const q = params.toString()
    const next = q ? `#${q}` : ''
    if (next !== window.location.hash) {
      const { pathname, search } = window.location
      window.history.replaceState(null, '', `${pathname}${search}${next}`)
    }
  } catch {
    /* URL/history unavailable — sharing is best-effort. */
  }
}

// ---------------------------------------------------------------------------
// Saved views (localStorage) — table-state serialization
// ---------------------------------------------------------------------------

const FEED_VIEWS_KEY = 'legba.feed.views'

export function loadFeedViews(): FeedSavedView[] {
  try {
    const raw = localStorage.getItem(FEED_VIEWS_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((v): v is FeedSavedView => v && typeof v.name === 'string')
      .map((v) => ({
        name: v.name,
        stream: isStream(v.stream) ? v.stream : 'intelligence',
        sort: isSort(v.sort) ? v.sort : 'recency',
        query: typeof v.query === 'string' ? v.query : '',
      }))
  } catch {
    return []
  }
}

export function persistFeedViews(views: FeedSavedView[]): void {
  try {
    localStorage.setItem(FEED_VIEWS_KEY, JSON.stringify(views))
  } catch {
    /* localStorage unavailable — ignore. */
  }
}

export function upsertFeedView(views: FeedSavedView[], view: FeedSavedView): FeedSavedView[] {
  return [...views.filter((v) => v.name !== view.name), view]
}

export function removeFeedView(views: FeedSavedView[], name: string): FeedSavedView[] {
  return views.filter((v) => v.name !== name)
}
