/**
 * Findings-feed data layer — saved views, sorting, and situation
 * clustering / supersession (UI-1 finish / Tier A).
 *
 * This module is intentionally the *only* place the Findings feed's
 * non-React logic lives, so it can be unit-tested without a DOM and so
 * the clustering hook has one clearly-marked home.
 *
 * --- Saved views ---
 * Filter presets persisted to localStorage under `legba.findings.views`.
 * A view captures the target/analyst/severity/sort knobs the operator
 * tuned; "save view" snapshots them, "apply view" restores them.
 *
 * --- Situation clustering (P-FS, backend LANDED) ---
 * The frozen `/findings` REST shape (substrate_reads_api.FindingRow)
 * mirrors `analyst_outputs` columns but does NOT surface the P-FS
 * supersession columns (`superseded_by`, `superseded_at`,
 * `situation_signature`). So we cluster CLIENT-SIDE from what the frozen
 * row shape DOES carry, exactly as the brief directed:
 *
 *   1. The finding's cluster KEY — derived with the same priority the
 *      backend `finding_supersession.derive_signature` uses: explicit
 *      `data.situation_id` / `data.situation_signature`, else a derived
 *      `sig:<topic>|<sorted entity tokens>`. `clusterKeyOf` is the single
 *      source of truth and matches the backend signature byte-for-byte.
 *
 *   2. The P-FS SUMMARY FINDING — the handler emits a normal finding row
 *      (readable via `/findings`) with `data.sub_handler ==
 *      'finding_supersession'` carrying `data.clusters: [{
 *      situation_signature, latest_finding_id, superseded_finding_ids,
 *      reason, score }]`. That is the AUTHORITATIVE latest/superseded map
 *      (the `finding_supersessions` link table isn't exposed over REST).
 *      `buildSupersessionIndex` parses these rows into a lookup.
 *
 * `clusterBySituation` groups rows by cluster key, picks the LATEST per
 * cluster (authoritative latest_finding_id when the summary names it,
 * else newest produced_at / largest id — matching backend `_pick_latest`),
 * and collapses the rest as supersession `history`. Findings with no
 * derivable key live in a single flat tail cluster. When NO row has a
 * derivable key the whole feed renders as one flat pseudo-cluster, and
 * `enabled=false` forces flat regardless.
 */

import type { RegistryEvent } from './ws'

/** Severity ordering — higher = more severe. Unknown severities sort last. */
export const SEVERITY_RANK: Record<string, number> = {
  critical: 4,
  high: 3,
  medium: 2,
  low: 1,
}

export type SortMode = 'recency' | 'severity'

/**
 * Compact relative-time label ("now", "12m ago", "3h ago", "5d ago") for the
 * tight feed rows — replaces the verbose `toLocaleString()` stamp the old
 * numbered feed never showed. Falls back to the raw string for an unparseable
 * timestamp so a row never renders a bare "NaN".
 */
export function relativeTime(iso: string, now: number = Date.now()): string {
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return iso
  const sec = Math.round((now - t) / 1000)
  if (sec < 0) return 'now'
  if (sec < 45) return 'now'
  const min = Math.round(sec / 60)
  if (min < 60) return `${min}m ago`
  const hr = Math.round(min / 60)
  if (hr < 24) return `${hr}h ago`
  const day = Math.round(hr / 24)
  if (day < 7) return `${day}d ago`
  const wk = Math.round(day / 7)
  if (wk < 5) return `${wk}w ago`
  const mo = Math.round(day / 30)
  if (mo < 12) return `${mo}mo ago`
  return `${Math.round(day / 365)}y ago`
}

/** Minimal shape the data layer needs from a finding row. */
export interface FindingLike {
  id: string
  severity: string | null
  produced_at: string
  /**
   * jsonb payload. May carry the P-FS situation binding
   * (`situation_id` / `situation_signature`) and/or the entity/topic keys
   * the derived signature is built from. The P-FS *summary* finding also
   * lands here with `sub_handler: 'finding_supersession'` + `clusters`.
   */
  data?: Record<string, unknown> | null
  /** Analyst sub-handler, when the row carries it — derived-signature topic fallback. */
  analyst_id?: string | null
  /**
   * S3 critic actuator. `critic_score` is the L-175 critic's `overall_score`
   * for THIS finding (the `/findings` REST shape now LEFT JOINs it), `null`
   * when the finding was never critiqued. `effective_confidence` is the
   * critic-folded surfaced confidence `min(confidence, critic_score)` the
   * backend already computed — the feed shows THIS so a poorly-graded finding
   * reads as lower-confidence instead of the critic score being a spectator.
   */
  critic_score?: number | null
  effective_confidence?: number | null
}

export interface FindingsFilter {
  target_id: string
  analyst_id: string
  severity: string // 'all' | severity
  sort: SortMode
}

export interface SavedView extends FindingsFilter {
  name: string
}

export const DEFAULT_FILTER: FindingsFilter = {
  target_id: '',
  analyst_id: '',
  severity: 'all',
  sort: 'recency',
}

const VIEWS_KEY = 'legba.findings.views'

// ---------------------------------------------------------------------------
// Saved views (localStorage)
// ---------------------------------------------------------------------------

export function loadSavedViews(): SavedView[] {
  try {
    const raw = localStorage.getItem(VIEWS_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    // Defensive hydration — drop anything that isn't a well-formed view.
    return parsed.filter(
      (v): v is SavedView =>
        v && typeof v.name === 'string' && typeof v.severity === 'string',
    )
  } catch {
    return []
  }
}

export function persistSavedViews(views: SavedView[]): void {
  try {
    localStorage.setItem(VIEWS_KEY, JSON.stringify(views))
  } catch {
    /* localStorage may be unavailable (private mode / SSR) — ignore. */
  }
}

/** Insert-or-replace a view by name; returns the new list (does not persist). */
export function upsertView(views: SavedView[], view: SavedView): SavedView[] {
  const without = views.filter((v) => v.name !== view.name)
  return [...without, view]
}

export function removeView(views: SavedView[], name: string): SavedView[] {
  return views.filter((v) => v.name !== name)
}

// ---------------------------------------------------------------------------
// Sorting
// ---------------------------------------------------------------------------

export function severityRank(severity: string | null): number {
  if (!severity) return 0
  return SEVERITY_RANK[severity] ?? 0
}

/**
 * S3 — the confidence the feed should SHOW for a finding: the backend's
 * critic-folded `effective_confidence` when present, else fall back to the
 * raw `confidence`. Single source of truth so every surface (card, sort,
 * badge) displays the actuated-by-critic value, not the un-graded one.
 */
export function surfacedConfidence(
  row: { confidence?: number | null; effective_confidence?: number | null },
): number | null {
  if (typeof row.effective_confidence === 'number') return row.effective_confidence
  if (typeof row.confidence === 'number') return row.confidence
  return null
}

/**
 * True when the critic GRADED this finding below the surfacing threshold —
 * i.e. the critic actuation knocked its confidence down. Lets a surface badge
 * "critic-flagged" findings whose critique fell under `threshold` (default
 * 0.5, the inline_target critic's mid-rubric mark).
 */
export function isCriticFlagged(
  row: { critic_score?: number | null },
  threshold = 0.5,
): boolean {
  return typeof row.critic_score === 'number' && row.critic_score < threshold
}

/**
 * Sort findings by the chosen mode. Severity sort is a stable two-key
 * sort (severity desc, then recency desc) so equal-severity rows still
 * read newest-first. Returns a NEW array.
 */
export function sortFindings<T extends FindingLike>(rows: T[], mode: SortMode): T[] {
  const copy = [...rows]
  if (mode === 'severity') {
    copy.sort((a, b) => {
      const sr = severityRank(b.severity) - severityRank(a.severity)
      if (sr !== 0) return sr
      return Date.parse(b.produced_at) - Date.parse(a.produced_at)
    })
  } else {
    copy.sort((a, b) => Date.parse(b.produced_at) - Date.parse(a.produced_at))
  }
  return copy
}

// ---------------------------------------------------------------------------
// Situation clustering (P-FS — backend landed; client-side from frozen shape)
// ---------------------------------------------------------------------------

export interface FindingsCluster<T extends FindingLike> {
  /**
   * The cluster KEY. For clustered groups this is the situation signature
   * (`sit:<id>` for explicitly-bound findings, `sig:<topic>|<entities>` for
   * derived). For the flat tail it is the sentinel `__flat__`.
   */
  situation_id: string
  /** True when this is a flat (unclustered) pseudo-cluster. */
  flat: boolean
  /**
   * Latest-first rows in the cluster. `rows[0]` is the canonical/latest
   * finding; `rows[1..]` are the superseded history. For a flat cluster
   * every row is just a row (no supersession relationship).
   */
  rows: T[]
  /** The canonical (latest, not-superseded) finding. `rows[0]` for clusters. */
  latest?: T
  /** Superseded prior findings, latest-first. Empty when the cluster is a singleton. */
  history?: T[]
  /**
   * Why these were clustered, when the P-FS summary finding names this
   * signature: `'situation_id'` (explicit) or `'signature_match'` (derived).
   */
  reason?: string
  /** Supersession confidence from the P-FS summary (1.0 for exact matches). */
  score?: number
  /**
   * True when the latest/superseded split was confirmed by the P-FS summary
   * finding's `data.clusters` map (authoritative), vs. inferred client-side
   * purely from the shared signature + recency.
   */
  confirmed?: boolean
}

/** Sentinel id for the flat (unclustered) pseudo-cluster. */
export const FLAT_CLUSTER_ID = '__flat__'

/** Sub-handler tag the P-FS summary finding carries in `data.sub_handler`. */
export const SUPERSESSION_SUB_HANDLER = 'finding_supersession'

/** One entry of the P-FS summary finding's `data.clusters`. */
export interface SupersessionClusterRef {
  situation_signature: string
  latest_finding_id: string
  superseded_finding_ids: string[]
  reason?: string
  score?: number
}

/**
 * Authoritative latest/superseded map reconstructed from the P-FS summary
 * finding(s). Maps a finding id → its canonical (latest) finding id, and a
 * situation signature → its latest finding id + metadata.
 */
export interface SupersessionIndex {
  /** finding id → latest/canonical finding id for its situation. */
  latestOf: Map<string, string>
  /** finding id → true when this finding was superseded by a newer one. */
  superseded: Set<string>
  /** situation signature → ref metadata (latest id, reason, score). */
  bySignature: Map<string, SupersessionClusterRef>
}

// ---------------------------------------------------------------------------
// NATS live-tail envelope mapping
// ---------------------------------------------------------------------------

/** Subject filter for the finding live-tail (per `analyst.<id>.finding`). */
export const FINDINGS_TAIL_FILTER = 'analyst.*.finding'

/**
 * Map a NATS event payload (the finding envelope published by the
 * `nats_stream` output kind) onto the feed's row shape, best-effort.
 *
 * The envelope is the finding object itself; field presence is not
 * guaranteed across analyst versions, so every field is defensively
 * read. Returns `null` when the payload has no usable id (we never
 * append a row we can't key or open lineage on).
 */
export function mapTailEnvelope(
  payload: Record<string, unknown> | undefined,
): UnifiedRow | null {
  if (!payload) return null
  const id = strOrNull(payload.id ?? payload.finding_id)
  if (!id) return null
  return {
    id,
    source: 'finding',
    kind: strOr(payload.kind, 'finding'),
    title: strOr(payload.title ?? payload.summary, '(live finding)'),
    body: strOr(payload.body, ''),
    confidence: numOrNull(payload.confidence),
    severity: strOrNull(payload.severity),
    target_id: strOrNull(payload.target_id),
    analyst_id: strOrNull(payload.analyst_id),
    analyst_version: strOrNull(payload.analyst_version),
    produced_at: strOr(payload.produced_at ?? payload.created_at, new Date().toISOString()),
    derived_from: Array.isArray(payload.derived_from)
      ? (payload.derived_from as unknown[]).map((d) => String(d))
      : [],
    schema_uri: strOr(payload.schema_uri, ''),
    data: isRecord(payload.data) ? (payload.data as Record<string, unknown>) : null,
    // A live finding has not been critiqued yet (the critic runs after the
    // finding lands), so the tail carries no critic score — null until the
    // REST refetch surfaces a critique.
    critic_score: numOrNull(payload.critic_score),
    effective_confidence: numOrNull(payload.effective_confidence ?? payload.confidence),
    live: true,
  }
}

/** A feed row that may have arrived over the live tail. */
export interface TailFinding extends FindingLike {
  kind: string
  title: string
  body: string
  confidence: number | null
  target_id: string | null
  analyst_id: string | null
  analyst_version: string | null
  derived_from: string[]
  schema_uri: string
  /** True when this row arrived via the NATS live-tail (badge it). */
  live?: boolean
}

// ---------------------------------------------------------------------------
// #90 feed merge — the UNIFIED row (findings + signals in one feed)
// ---------------------------------------------------------------------------

/**
 * The single row type the merged feed renders. Extends TailFinding (so it
 * satisfies FindingLike with zero new constraints — clusterBySituation /
 * sortFindings / buildSupersessionIndex / the sparkline all consume
 * `UnifiedRow[]` unchanged) plus a `source` discriminant and signal-only
 * display fields. A finding maps in 1:1; a signal maps via signalRestToRow /
 * signalTailToRow with `severity:null` (signals carry no severity column).
 */
export interface UnifiedRow extends TailFinding {
  /** Discriminant — NOT `kind` (kind stays the substrate OutputKind). */
  source: 'finding' | 'signal'
  /** Signal source descriptor id, for the source chip (signals only). */
  source_id?: string | null
  /** Signal typed `tags` column (top-level, also mirrored in data). */
  tags?: string[]
  /** Signal typed `geo` column (top-level, also mirrored in data). */
  geo?: string[]
  /** Stable dedup key for an id-less live-tail signal; else unset (use id). */
  dedupKey?: string
}

/** The unified dedup key — composite so a finding and a signal that happen to
 *  share a UUID never collide. Id-less tail signals carry an explicit dedupKey. */
export function rowDedupKey(row: UnifiedRow): string {
  return row.dedupKey ?? `${row.source}:${row.id}`
}

/** Subject filter for the raw signal live-tail. */
export const SIGNALS_TAIL_FILTER = 'legba.signals.>'

/**
 * One row of `GET /signals` — mirrors the backend `SignalRow`
 * (substrate_reads_api.py). Only the fields the feed reads are typed.
 */
export interface SignalRestRow {
  id: string
  data?: Record<string, unknown> | null
  title?: string
  source_id?: string | null
  source_url?: string
  guid?: string
  category?: string
  event_timestamp?: string | null
  confidence?: number | null
  target_id?: string | null
  analyst_id?: string | null
  produced_at?: string
  derived_from?: string[]
  schema_uri?: string
  geo?: string[]
  tags?: string[]
}

/** Signals run ~100× findings volume and carry no severity column; a 0.0
 *  source_credibility is junk, so only show a confidence chip when > 0. */
function signalConfidence(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : null
}

/** Map a REST `/signals` row onto the unified feed row (lifted from the old
 *  LiveFeed.seedRow). `id` + `produced_at` are always present on the backend. */
export function signalRestToRow(r: SignalRestRow): UnifiedRow {
  return {
    id: r.id,
    source: 'signal',
    kind: 'signal',
    title: strOr(r.title, '(signal)'),
    body: '',
    confidence: signalConfidence(r.confidence),
    severity: null,
    target_id: strOrNull(r.target_id ?? null),
    analyst_id: null,
    analyst_version: null,
    produced_at: strOr(r.produced_at ?? r.event_timestamp ?? null, new Date().toISOString()),
    derived_from: Array.isArray(r.derived_from) ? r.derived_from.map((d) => String(d)) : [],
    schema_uri: strOr(r.schema_uri, ''),
    data: isRecord(r.data) ? (r.data as Record<string, unknown>) : null,
    source_id: strOrNull(r.source_id ?? null),
    tags: Array.isArray(r.tags) ? r.tags.map((t) => String(t)) : [],
    geo: Array.isArray(r.geo) ? r.geo.map((g) => String(g)) : [],
    critic_score: null,
    effective_confidence: null,
  }
}

/** Monotonic per-process counter that makes each id-less signal's ephemeral key
 *  unique even when two arrive on the same subject at the same timestamp. */
let _ephemeralSignalSeq = 0

/**
 * Map a NATS `legba.signals.>` envelope onto the unified row (lifted from the
 * old LiveFeed.toRow + idOf). Signal tail events normally carry `signal_id`
 * (a server-side UUID), but when none is present we synthesize a
 * visibly-ephemeral `sig:` id + dedupKey. The key includes a monotonic counter
 * so two distinct id-less signals on the same subject+ts never collapse.
 */
export function signalTailToRow(ev: RegistryEvent): UnifiedRow | null {
  const payload = ev.payload
  if (!payload) return null
  const realId =
    strOrNull(payload.id) ??
    strOrNull(payload.signal_id) ??
    strOrNull(payload.content_hash)
  // Per-event uniqueness via a monotonic counter (deterministic for tests; no
  // Math.random) so two id-less events sharing subject+ts get distinct keys.
  const ephemeralSuffix = `${ev.subject ?? 'sig'}:${ev.ts}:${_ephemeralSignalSeq++}`
  const id = realId ?? `sig:${ephemeralSuffix}`
  return {
    id,
    source: 'signal',
    kind: 'signal',
    title: strOr(payload.title, '(live signal)'),
    body: '',
    confidence: signalConfidence(payload.confidence),
    severity: null,
    target_id: strOrNull(payload.target_id),
    analyst_id: null,
    analyst_version: null,
    produced_at: strOr(payload.produced_at ?? payload.event_timestamp ?? ev.ts, new Date().toISOString()),
    derived_from: Array.isArray(payload.derived_from)
      ? (payload.derived_from as unknown[]).map((d) => String(d))
      : [],
    schema_uri: strOr(payload.schema_uri, ''),
    data: isRecord(payload.data) ? (payload.data as Record<string, unknown>) : null,
    source_id: strOrNull(payload.source_id),
    tags: Array.isArray(payload.tags) ? (payload.tags as unknown[]).map((t) => String(t)) : [],
    geo: Array.isArray(payload.geo) ? (payload.geo as unknown[]).map((g) => String(g)) : [],
    // id-less signals carry an explicit ephemeral dedup key.
    dedupKey: realId ? undefined : `sig:${ephemeralSuffix}`,
    critic_score: null,
    effective_confidence: null,
    live: true,
  }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}
function strOrNull(v: unknown): string | null {
  return typeof v === 'string' && v.length > 0 ? v : null
}
function strOr(v: unknown, fallback: string): string {
  return typeof v === 'string' && v.length > 0 ? v : fallback
}
function numOrNull(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/**
 * Read a finding's explicit situation id from its jsonb payload. Single
 * source of truth for the explicit P-FS binding field — when the backend
 * changes the field name, only this function changes.
 */
export function situationIdOf(row: FindingLike): string | null {
  const data = row.data
  const sid = data?.situation_id ?? data?.situation_signature
  return typeof sid === 'string' && sid.length > 0 ? sid : null
}

// ---------------------------------------------------------------------------
// Cluster key derivation — mirrors backend finding_supersession.derive_signature
// ---------------------------------------------------------------------------

/** Entity-bearing keys, in the exact set the backend reads. */
const ENTITY_KEYS = [
  'key_entities',
  'entities',
  'actors',
  'locations',
  'geo',
  'geo_countries',
] as const
/** Topic-bearing keys, in backend priority order. */
const TOPIC_KEYS = ['category', 'topic', 'situation_kind', 'event_type'] as const

/** Normalized, deduped, sorted entity tokens (>=2 chars), matching the backend. */
function entityTokens(data: Record<string, unknown>): string[] {
  const tokens = new Set<string>()
  for (const key of ENTITY_KEYS) {
    let vals = data[key]
    if (vals === undefined || vals === null || vals === '') continue
    if (typeof vals === 'string') vals = [vals]
    if (!Array.isArray(vals)) continue
    for (const v of vals) {
      const t = String(v).trim().toLowerCase()
      if (t.length >= 2) tokens.add(t)
    }
  }
  return [...tokens].sort()
}

function topicOf(data: Record<string, unknown>, fallback: string | null): string {
  for (const key of TOPIC_KEYS) {
    const v = data[key]
    if (v) return String(v).trim().toLowerCase()
  }
  return (fallback ?? '').trim().toLowerCase()
}

/**
 * The deterministic situation signature for a finding, or `null` ("do not
 * cluster"). Byte-for-byte identical to the backend `derive_signature`:
 *   1. explicit `situation_signature` / `situation_id` → `sit:<id>`
 *   2. derived `sig:<topic>|<entity tokens>` (needs >=1 entity token)
 * A bare summary finding (no explicit id, no entities) returns `null`.
 */
export function clusterKeyOf(row: FindingLike): string | null {
  // #90 feed merge — signals are atomic events, never situations. They are
  // first-class rows in the unified feed but MUST stay out of finding clusters:
  // a signal's `data` payload carries `geo` (an ENTITY_KEY), so without this
  // short-circuit a signal would falsely cluster with any finding sharing a geo
  // token (e.g. a whole country's signals collapsing under one finding).
  if ((row as { source?: string }).source === 'signal') return null
  const data = row.data
  if (!data) return null
  const explicit = data.situation_signature ?? data.situation_id
  if (explicit) return `sit:${String(explicit).trim()}`

  const tokens = entityTokens(data)
  if (tokens.length === 0) return null
  let topic = topicOf(data, typeof data.sub_handler === 'string' ? data.sub_handler : null)
  if (!topic) topic = '_'
  return `sig:${topic}|${tokens.join(',')}`
}

/** True if ANY row carries a derivable cluster key — i.e. clustering applies. */
export function hasClusteringData(rows: FindingLike[]): boolean {
  return rows.some((r) => clusterKeyOf(r) !== null)
}

// ---------------------------------------------------------------------------
// Supersession index — reconstructed from the P-FS summary finding(s)
// ---------------------------------------------------------------------------

/** True when a row is a P-FS supersession *summary* finding (not a clusterable finding). */
export function isSupersessionSummary(row: FindingLike): boolean {
  return row.data?.sub_handler === SUPERSESSION_SUB_HANDLER
}

function asRefs(raw: unknown): SupersessionClusterRef[] {
  if (!Array.isArray(raw)) return []
  const out: SupersessionClusterRef[] = []
  for (const c of raw) {
    if (!isRecord(c)) continue
    const sig = c.situation_signature
    const latest = c.latest_finding_id
    if (typeof sig !== 'string' || !sig) continue
    if (typeof latest !== 'string' || !latest) continue
    out.push({
      situation_signature: sig,
      latest_finding_id: latest,
      superseded_finding_ids: Array.isArray(c.superseded_finding_ids)
        ? c.superseded_finding_ids.map((x) => String(x))
        : [],
      reason: typeof c.reason === 'string' ? c.reason : undefined,
      score: typeof c.score === 'number' ? c.score : undefined,
    })
  }
  return out
}

/**
 * Walk the page's rows, pull every P-FS summary finding's `data.clusters`,
 * and build the authoritative latest/superseded lookup. Later summaries win
 * (the freshest run's view of a situation supersedes an earlier run's).
 */
export function buildSupersessionIndex(rows: FindingLike[]): SupersessionIndex {
  const bySignature = new Map<string, SupersessionClusterRef>()
  // Newest summary first so the freshest cluster view wins on collision.
  const summaries = rows
    .filter(isSupersessionSummary)
    .slice()
    .sort((a, b) => Date.parse(b.produced_at) - Date.parse(a.produced_at))
  for (const s of summaries) {
    for (const ref of asRefs(s.data?.clusters)) {
      if (!bySignature.has(ref.situation_signature)) {
        bySignature.set(ref.situation_signature, ref)
      }
    }
  }
  const latestOf = new Map<string, string>()
  const superseded = new Set<string>()
  for (const ref of bySignature.values()) {
    latestOf.set(ref.latest_finding_id, ref.latest_finding_id)
    for (const sid of ref.superseded_finding_ids) {
      latestOf.set(sid, ref.latest_finding_id)
      superseded.add(sid)
    }
  }
  return { latestOf, superseded, bySignature }
}

// ---------------------------------------------------------------------------
// Latest-per-cluster selection — mirrors backend _pick_latest
// ---------------------------------------------------------------------------

/** Deterministic latest within a cluster: newest produced_at, tie → largest id. */
function pickLatest<T extends FindingLike>(rows: T[]): T {
  return rows.reduce((best, r) => {
    const tr = Date.parse(r.produced_at)
    const tb = Date.parse(best.produced_at)
    if (tr > tb) return r
    if (tr < tb) return best
    return r.id > best.id ? r : best
  })
}

/**
 * THE clustering hook. Group findings by their situation signature
 * (`clusterKeyOf`), pick the canonical/latest per cluster, and collapse the
 * rest as supersession `history` (latest-first). Findings with no derivable
 * key land in one flat tail cluster. P-FS *summary* findings are excluded
 * from clustering (they describe the clustering, they aren't situations).
 *
 * When an optional `index` (from `buildSupersessionIndex`) is supplied, the
 * authoritative `latest_finding_id` it names overrides recency-based latest
 * selection and the cluster is marked `confirmed`, with `reason`/`score`
 * carried through from the P-FS summary.
 *
 * `enabled=false` forces a single flat cluster regardless of data.
 */
export function clusterBySituation<T extends FindingLike>(
  rows: T[],
  enabled = true,
  index?: SupersessionIndex,
): FindingsCluster<T>[] {
  if (!enabled || !hasClusteringData(rows)) {
    return [{ situation_id: FLAT_CLUSTER_ID, flat: true, rows }]
  }
  const groups = new Map<string, T[]>()
  const ungrouped: T[] = []
  for (const r of rows) {
    if (isSupersessionSummary(r)) {
      // Summary findings aren't situations — keep them visible in the flat tail.
      ungrouped.push(r)
      continue
    }
    const key = clusterKeyOf(r)
    if (key === null) {
      ungrouped.push(r)
      continue
    }
    const bucket = groups.get(key)
    if (bucket) bucket.push(r)
    else groups.set(key, [r])
  }

  const out: FindingsCluster<T>[] = []
  for (const [situation_id, members] of groups) {
    const ref = index?.bySignature.get(situation_id)
    // Authoritative latest from the P-FS summary, else deterministic recency.
    let latest: T | undefined
    if (ref) latest = members.find((m) => m.id === ref.latest_finding_id)
    if (!latest) latest = pickLatest(members)
    const history = members
      .filter((m) => m.id !== latest!.id)
      .sort((a, b) => Date.parse(b.produced_at) - Date.parse(a.produced_at))
    out.push({
      situation_id,
      flat: false,
      rows: [latest, ...history],
      latest,
      history,
      reason: ref?.reason,
      score: ref?.score,
      confirmed: ref !== undefined,
    })
  }
  // Clustered groups first, most-active (largest) first; then the flat tail.
  out.sort((a, b) => b.rows.length - a.rows.length)
  if (ungrouped.length > 0) {
    out.push({ situation_id: FLAT_CLUSTER_ID, flat: true, rows: ungrouped })
  }
  return out
}
