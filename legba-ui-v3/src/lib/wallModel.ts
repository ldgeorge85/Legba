/**
 * wallModel — the Wall tile's non-DOM logic (P1-7).
 *
 * The Wall (`system.wall`) is the mission-control anchor: "world at a glance +
 * what changed since I last looked" in one screen. Everything that is not
 * rendering lives here so it is unit-testable without a DOM:
 *
 *   * the SINCE-CURSOR lifecycle — the client owns the cursor (the server is
 *     stateless, P1-6): `localStorage.legba_wall_cursor` holds the
 *     `server_now` of the last successful look; on open the panel fetches
 *     `/v3/since?cursor=<stored>` and stores the response's `server_now` back.
 *     First-ever open = a 24h default lookback. A cursor older than the
 *     server's 90d hard bound is clamped (the route 400s beyond it).
 *   * movers grouping/sorting — band changes first (worst direction first),
 *     then the superseded-reversal count, then situation lifecycle edges.
 *   * the health-corner rollup over the System Status routes' rows.
 *
 * The response types mirror `src/legba/data/registry/since_api.py`'s pydantic
 * models 1:1 (SinceFinding / SupersededFinding / BandChange / SituationChange /
 * SinceAlert + the per-section {items,total,truncated} envelope). Honesty: each
 * section's `total` + `truncated` are carried through — a capped list is never
 * presented as the whole story.
 */

import { severityRank } from '@/lib/findingsViews'
import { humanizeId } from '@/lib/deskNames'
import type { AnalystCadenceRow, SourceFiringRow } from '@/lib/api'

// ---------------------------------------------------------------------------
// `/v3/since` response mirror (since_api.py)
// ---------------------------------------------------------------------------

export interface SinceFinding {
  id: string
  analyst_id: string | null
  target_id: string | null
  title: string
  severity: string | null
  confidence: number
  faithfulness_score: number
  effective_confidence: number
  produced_at: string
}

export interface SupersededBy {
  id: string
  analyst_id: string | null
  title: string | null
  produced_at: string | null
}

export interface SupersededFinding {
  id: string
  analyst_id: string | null
  target_id: string | null
  title: string
  severity: string | null
  superseded_at: string
  superseded_by: SupersededBy | null
}

export interface BandChange {
  target_id: string
  dimension: string
  from_band: string
  to_band: string
  /** 'deterioration' | 'improvement' | 'evidence-gained' | 'evidence-lost' | 'indeterminate' */
  direction: string
  severity: string
  from_scorecard_row_id: string
  to_scorecard_row_id: string
  changed_at: string
}

export interface SituationChange {
  id: string
  name: string
  target_id: string | null
  category: string
  /** 'appeared' | 'escalating' | 'quieted' | 'resolved' */
  change: string
  from_status: string | null
  to_status: string
  status: string
  last_event_at: string | null
  updated_at: string
  intensity_score: number
}

export interface SinceAlert {
  id: string
  severity: string | null
  channel: string
  summary: string
  target_id: string | null
  produced_at: string
}

export interface SinceSection<T> {
  items: T[]
  total: number
  truncated: boolean
}

export interface SinceResponse {
  cursor: string
  server_now: string
  counts: Record<string, number>
  new_findings: SinceSection<SinceFinding>
  superseded: SinceSection<SupersededFinding>
  band_changes: SinceSection<BandChange>
  situations: SinceSection<SituationChange>
  alerts: SinceSection<SinceAlert>
}

// ---------------------------------------------------------------------------
// Cursor lifecycle
// ---------------------------------------------------------------------------

/** The one localStorage key the Wall's since-cursor lives under. */
export const WALL_CURSOR_KEY = 'legba_wall_cursor'

/** First-ever open: how far back the default cursor looks. */
export const DEFAULT_LOOKBACK_HOURS = 24

/** Mirror of `since_api.MAX_LOOKBACK_DAYS` — the route 400s beyond it, so a
 *  very stale stored cursor is clamped just inside the bound. */
export const MAX_LOOKBACK_DAYS = 90

/** Safety margin inside the 90d bound so clock skew can't tip a clamped
 *  cursor over the server's rejection line. */
const CLAMP_MARGIN_MS = 5 * 60_000

export interface WallCursor {
  /** The ISO cursor to send as `?cursor=`. */
  cursor: string
  /** True when no stored cursor existed (first-ever open → 24h default). */
  firstVisit: boolean
  /** True when the stored cursor was older than the server's 90d bound and
   *  was clamped forward (the lookback shown is honest-but-capped). */
  clamped: boolean
}

/**
 * Resolve the cursor for this visit from the stored value (or its absence).
 * Pure: `nowMs` is injectable for deterministic tests.
 *
 *  * no/invalid stored value → `now - 24h`, `firstVisit: true`.
 *  * stored in the future (clock skew) → clamped back to `now`.
 *  * stored beyond the 90d server bound → clamped to `now - 90d + margin`.
 */
export function resolveWallCursor(
  stored: string | null | undefined,
  nowMs: number = Date.now(),
): WallCursor {
  const parsed = stored ? Date.parse(stored) : NaN
  if (!Number.isFinite(parsed)) {
    return {
      cursor: new Date(nowMs - DEFAULT_LOOKBACK_HOURS * 3_600_000).toISOString(),
      firstVisit: true,
      clamped: false,
    }
  }
  const oldest = nowMs - MAX_LOOKBACK_DAYS * 86_400_000 + CLAMP_MARGIN_MS
  if (parsed < oldest) {
    return { cursor: new Date(oldest).toISOString(), firstVisit: false, clamped: true }
  }
  if (parsed > nowMs) {
    return { cursor: new Date(nowMs).toISOString(), firstVisit: false, clamped: true }
  }
  return { cursor: new Date(parsed).toISOString(), firstVisit: false, clamped: false }
}

/** Read the stored cursor (null when absent / storage unavailable). */
export function loadWallCursor(storage: Pick<Storage, 'getItem'> | null = defaultStorage()): string | null {
  try {
    return storage?.getItem(WALL_CURSOR_KEY) ?? null
  } catch {
    return null
  }
}

/**
 * Advance the stored cursor to `serverNow` (the response's capture time —
 * storing it after a successful display is what makes the NEXT visit a
 * "since last visit" diff). Never moves the cursor backwards: a stale poll
 * response cannot rewind a fresher stored value. Best-effort on storage
 * failure (private mode / quota).
 */
export function storeWallCursor(
  serverNow: string,
  storage: Pick<Storage, 'getItem' | 'setItem'> | null = defaultStorage(),
): void {
  const next = Date.parse(serverNow)
  if (!Number.isFinite(next)) return
  try {
    const prevRaw = storage?.getItem(WALL_CURSOR_KEY) ?? null
    const prev = prevRaw ? Date.parse(prevRaw) : NaN
    if (Number.isFinite(prev) && prev >= next) return
    storage?.setItem(WALL_CURSOR_KEY, serverNow)
  } catch {
    // storage unavailable — the cursor just won't persist this visit.
  }
}

function defaultStorage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

/** The `/v3/since` request path for a cursor (relative to the api client's
 *  `/api/v1` base). */
export function sincePath(cursor: string): string {
  return `/v3/since?cursor=${encodeURIComponent(cursor)}`
}

// ---------------------------------------------------------------------------
// Movers (quadrant 2) — grouping + sorting
// ---------------------------------------------------------------------------

/** Direction tone for a band change — drives the direction coloring. */
export type MoverTone = 'bad' | 'good' | 'neutral'

/** deterioration = up the risk ladder (bad); improvement = down (good);
 *  evidence-gained/-lost + indeterminate are coverage events (neutral). */
export function bandDirectionTone(direction: string): MoverTone {
  if (direction === 'deterioration') return 'bad'
  if (direction === 'improvement') return 'good'
  return 'neutral'
}

/** U-2 — the movers list must read as prose, not plumbing: a band change's
 *  `target_id` (`country_g20_br`) humanizes to its country name (`Brazil`)
 *  via the shared `lib/deskNames.ts` resolver, same one the Desks nav group
 *  and the Scorecard use, so a desk never disagrees with itself across
 *  surfaces. Total over any target id (never renders raw snake_case). */
export function bandChangeDeskLabel(targetId: string): string {
  return humanizeId(targetId)
}

const DIRECTION_ORDER: Record<string, number> = {
  deterioration: 0,
  'evidence-lost': 1,
  'evidence-gained': 2,
  improvement: 3,
}

const SITUATION_CHANGE_ORDER: Record<string, number> = {
  appeared: 0,
  escalating: 1,
  quieted: 2,
  resolved: 3,
}

export interface MoversView {
  /** Band changes, worst direction first, then newest first. */
  bandChanges: BandChange[]
  bandTotal: number
  bandTruncated: boolean
  /** Superseded-reversal rollup (count only — the wall stays glanceable). */
  supersededCount: number
  supersededTruncated: boolean
  /** Situation lifecycle edges, appeared/escalating first, then intensity. */
  situationEdges: SituationChange[]
  situationTotal: number
  situationTruncated: boolean
  alertCount: number
  /** True when NOTHING moved since the cursor — the honest empty state. */
  isEmpty: boolean
}

/** Group + sort the /since sections into the movers quadrant's view. */
export function buildMovers(since: SinceResponse): MoversView {
  const bandChanges = [...(since.band_changes.items ?? [])].sort((a, b) => {
    const d = (DIRECTION_ORDER[a.direction] ?? 4) - (DIRECTION_ORDER[b.direction] ?? 4)
    if (d !== 0) return d
    return Date.parse(b.changed_at) - Date.parse(a.changed_at)
  })
  const situationEdges = [...(since.situations.items ?? [])].sort((a, b) => {
    const d =
      (SITUATION_CHANGE_ORDER[a.change] ?? 4) - (SITUATION_CHANGE_ORDER[b.change] ?? 4)
    if (d !== 0) return d
    return b.intensity_score - a.intensity_score
  })
  const supersededCount = since.superseded.total
  const alertCount = since.alerts.total
  return {
    bandChanges,
    bandTotal: since.band_changes.total,
    bandTruncated: since.band_changes.truncated,
    supersededCount,
    supersededTruncated: since.superseded.truncated,
    situationEdges,
    situationTotal: since.situations.total,
    situationTruncated: since.situations.truncated,
    alertCount,
    isEmpty:
      since.band_changes.total === 0 &&
      supersededCount === 0 &&
      since.situations.total === 0 &&
      since.new_findings.total === 0 &&
      alertCount === 0,
  }
}

// ---------------------------------------------------------------------------
// Newest high-severity verified findings (quadrant 3)
// ---------------------------------------------------------------------------

/**
 * The top-N most severe of the since-window's verified findings: severity
 * rank first (critical → info), then effective confidence, then recency.
 * `new_findings` items are ALREADY verified-only server-side (the P1-6 gate),
 * so no client-side re-filtering — only ordering.
 */
export function topSevereVerified(items: SinceFinding[], n = 5): SinceFinding[] {
  return [...items]
    .sort((a, b) => {
      const d = severityRank(b.severity) - severityRank(a.severity)
      if (d !== 0) return d
      const c = b.effective_confidence - a.effective_confidence
      if (c !== 0) return c
      return Date.parse(b.produced_at) - Date.parse(a.produced_at)
    })
    .slice(0, n)
}

// ---------------------------------------------------------------------------
// Health corner (quadrant 4) — rollup over the System Status routes
// ---------------------------------------------------------------------------

export interface WallHealth {
  /** Sum of per-source `signals_24h` — the ingest volume headline.
   *  (`/v3/system/source-firing` carries no per-hour signal count, so the
   *  freshest honest liveness read is `sourcesSeenLastHour` below.) */
  signals24h: number
  sourcesTotal: number
  /** Sources whose newest signal is under an hour old (age_seconds ≤ 3600). */
  sourcesSeenLastHour: number
  /** Sources in status 'error' (recent poll errors). */
  sourceErrors: number
  analystsTotal: number
  /** Analysts in status 'stale' (last run > 6h ago). */
  analystsStale: number
  /** Analysts with zero recorded runs. */
  analystsNever: number
  /** Traffic light for the corner banner. */
  worst: 'green' | 'amber' | 'red'
}

/** Roll the System Status rows up to the wall's compact corner. */
export function healthRollup(
  sources: SourceFiringRow[],
  analysts: AnalystCadenceRow[],
): WallHealth {
  let signals24h = 0
  let sourcesSeenLastHour = 0
  let sourceErrors = 0
  for (const s of sources) {
    signals24h += s.signals_24h ?? 0
    if (s.age_seconds != null && s.age_seconds <= 3600) sourcesSeenLastHour++
    if (s.status === 'error') sourceErrors++
  }
  let analystsStale = 0
  let analystsNever = 0
  for (const a of analysts) {
    if (a.status === 'stale') analystsStale++
    else if (a.status === 'never') analystsNever++
  }
  const worst: WallHealth['worst'] =
    sourceErrors > 0 || analystsNever > 0
      ? 'red'
      : analystsStale > 0 || (sources.length > 0 && sourcesSeenLastHour === 0)
        ? 'amber'
        : 'green'
  return {
    signals24h,
    sourcesTotal: sources.length,
    sourcesSeenLastHour,
    sourceErrors,
    analystsTotal: analysts.length,
    analystsStale,
    analystsNever,
    worst,
  }
}
