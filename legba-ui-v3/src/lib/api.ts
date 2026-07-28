/**
 * Thin REST client for the legba-registry API.
 *
 * Bearer-token is read from `localStorage.legba_token` (set by `auth/jwt.ts`).
 * Errors surface as thrown `ApiError`s with status + parsed body.
 */

import type { PanelRegistration, Mode } from '@/types'
import type { TimelineResponse } from '@/lib/timelineWindows'

export class ApiError extends Error {
  status: number
  body: unknown
  constructor(status: number, body: unknown, msg?: string) {
    super(msg ?? `API error ${status}`)
    this.status = status
    this.body = body
  }
}

const API_BASE = '/api/v1'

function authHeaders(): HeadersInit {
  const token = localStorage.getItem('legba_token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/**
 * Read an error response body EXACTLY ONCE.
 *
 * A `Response` body is a single-use stream: calling `res.json()` consumes and
 * locks it, so the old `try { res.json() } catch { res.text() }` pattern threw
 * `Failed to execute 'text' on 'Response': body stream already read` whenever
 * the error body wasn't valid JSON (e.g. a plain-text 500 or a proxy 502),
 * masking the real status. Read as text once, then opportunistically parse.
 */
export async function readErrorBody(res: Response): Promise<unknown> {
  const raw = await res.text()
  if (!raw) return raw
  try {
    return JSON.parse(raw)
  } catch {
    return raw
  }
}

/**
 * Timing instrumentation (#4): every `apiGet` records its wall time so the real
 * cost of a chain (e.g. the Inspector's ~9s lineage→findings walk) is
 * measurable. Each call logs `[api] <ms> GET <path>` at debug level, warns past
 * `SLOW_MS`, and drops a `performance.measure` mark so the timings show up in
 * the DevTools Performance panel without any extra console noise.
 */
const SLOW_MS = 2000

function recordTiming(path: string, ms: number): void {
  const rounded = Math.round(ms)
  try {
    performance.measure(`api GET ${path}`, { start: performance.now() - ms, duration: ms })
  } catch {
    // performance.measure with a detail object is unsupported on old engines —
    // timing is best-effort; never let instrumentation break a request.
  }
  if (ms >= SLOW_MS) {
    console.warn(`[api] SLOW ${rounded}ms GET ${path}`)
  } else {
    console.debug(`[api] ${rounded}ms GET ${path}`)
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const started = performance.now()
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      headers: { Accept: 'application/json', ...authHeaders() },
    })
    if (!res.ok) {
      throw new ApiError(res.status, await readErrorBody(res))
    }
    return (await res.json()) as T
  } finally {
    recordTiming(path, performance.now() - started)
  }
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
      ...authHeaders(),
    },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    throw new ApiError(res.status, await readErrorBody(res))
  }
  return res.json() as Promise<T>
}

/** One composed export artifact off `POST /api/v1/v3/export` (A10). */
export interface ExportArtifact {
  filename: string
  mime: string
  content: string
}

/**
 * POST the collection basket to the server-side export composer and return
 * the raw document (markdown text or pretty JSON) plus the server-suggested
 * filename. NOT `apiPost` — the markdown format answers `text/markdown`, so
 * the body must be read as text, and the filename rides the
 * `Content-Disposition` header.
 */
export async function exportCollection(body: {
  items: Array<{ kind: 'finding' | 'journal_entry'; id: string }>
  format: 'markdown' | 'json'
  title?: string | null
}): Promise<ExportArtifact> {
  const res = await fetch(`${API_BASE}/v3/export`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    throw new ApiError(res.status, await readErrorBody(res))
  }
  const mime = res.headers.get('Content-Type') ?? 'text/plain'
  const disposition = res.headers.get('Content-Disposition') ?? ''
  const match = /filename="?([^";]+)"?/.exec(disposition)
  const fallback = `legba-export.${body.format === 'json' ? 'json' : 'md'}`
  const raw = await res.text()
  // Pretty-print the JSON document for preview/download readability.
  let content = raw
  if (body.format === 'json') {
    try {
      content = JSON.stringify(JSON.parse(raw), null, 2)
    } catch {
      content = raw
    }
  }
  return { filename: match?.[1] ?? fallback, mime, content }
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
      ...authHeaders(),
    },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    throw new ApiError(res.status, await readErrorBody(res))
  }
  return res.json() as Promise<T>
}

export async function apiDelete<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'DELETE',
    headers: { Accept: 'application/json', ...authHeaders() },
  })
  if (!res.ok) {
    throw new ApiError(res.status, await readErrorBody(res))
  }
  return res.json() as Promise<T>
}

// ---------------------------------------------------------------------------
// Config honesty — first-run readiness + model-component settings.
//
// The RUNTIME source of truth is the `stack_components` registry, not `.env`
// (which only seeds it once at bring-up). These helpers drive the Settings
// panel: read the required model-serving components, write them back through
// the registry, and route credentials through the vault (never plaintext into
// the component body). Mirrors `src/legba/data/registry/api.py`.
// ---------------------------------------------------------------------------

/** One required model-serving component's first-run readiness.
 *  Mirrors `RequiredComponentStatus` — identity/lifecycle only, no secrets. */
export interface RequiredComponentStatus {
  kind: 'llm_provider' | 'embedding' | 'nlp_service'
  configured: boolean
  active: boolean
  component_id: string | null
  name: string | null
  state: string | null
}

/** Aggregate first-run config readiness. Mirrors `ConfigStatusOut`. */
export interface ConfigStatus {
  first_run: boolean
  all_configured: boolean
  all_active: boolean
  required: RequiredComponentStatus[]
}

/** Ask the backend whether the required model components are configured yet.
 *  `first_run` true ⇒ show the "configure here" onboarding state. */
export async function fetchConfigStatus(): Promise<ConfigStatus> {
  return apiGet<ConfigStatus>('/registry/config/status')
}

/** A stack-component row as returned by `GET /registry/stack`. The backend
 *  NEVER returns plaintext credentials — `config` holds vault refs only. */
export interface StackComponentRow {
  component_id: string
  version: string
  schema_uri: string
  kind: string
  is_head: boolean
  state: string
  owner: string
  name: string
  body: Record<string, unknown>
  created_at: string
}

export async function listStackComponents(
  kind?: string,
): Promise<StackComponentRow[]> {
  const q = kind ? `?kind=${encodeURIComponent(kind)}&limit=500` : '?limit=500'
  return apiGet<StackComponentRow[]>(`/registry/stack${q}`)
}

/** Store a credential in the vault. The plaintext is sent over the
 *  already-TLS'd registry connection and NEVER echoed back. Returns the
 *  vault `secret_id` so it can be referenced from a component body. */
export async function storeSecret(
  secretId: string,
  plaintext: string,
  notes?: string,
): Promise<{ secret_id: string; version: number }> {
  return apiPost('/registry/vault/secrets', {
    secret_id: secretId,
    plaintext,
    notes: notes ?? null,
  })
}

export async function secretExists(secretId: string): Promise<boolean> {
  const out = await apiGet<{ secret_id: string; exists: boolean }>(
    `/registry/vault/secrets/${encodeURIComponent(secretId)}/exists`,
  )
  return out.exists
}

/** Register a brand-new stack component (`POST /registry/stack`). */
export async function registerStackComponent(
  body: Record<string, unknown>,
): Promise<StackComponentRow> {
  return apiPost<StackComponentRow>('/registry/stack', body)
}

/** Replace a stack component's head version (`PUT /registry/stack/{id}`). */
export async function updateStackComponent(
  componentId: string,
  body: Record<string, unknown>,
): Promise<StackComponentRow> {
  return apiPut<StackComponentRow>(
    `/registry/stack/${encodeURIComponent(componentId)}`,
    body,
  )
}

/** L-204 backend route — added in `legba.data.registry.api`. */
export async function fetchUiPanels(
  mode: Mode,
  opts: { includeRetired?: boolean } = {},
): Promise<PanelRegistration[]> {
  const params = new URLSearchParams({ mode })
  if (opts.includeRetired) params.set('include_retired', 'true')
  return apiGet<PanelRegistration[]>(`/registry/ui_panels?${params.toString()}`)
}

// ---------------------------------------------------------------------------
// Backfill / catch-up (P-12) — late-subscribed target replay.
// ---------------------------------------------------------------------------

/** The handoff boundary between catch-up and the live forward stream.
 *  Mirrors `BackfillCursor` (src/legba/runtime/subscription/backfill.py). */
export interface BackfillCursor {
  boundary_seq: number
  captured_at: string
  stream_present: boolean
  forward_start_seq: number
}

/** Outcome of one target's catch-up + forward bind.
 *  Mirrors `BackfillResult` (src/legba/runtime/subscription/backfill.py). */
export interface BackfillResult {
  target_id: string
  cursor: BackfillCursor
  delivered: number
  delivered_ids: string[]
  forward_consumer: string | null
}

/**
 * Trigger a one-time predicate backfill over the persistent signal pool for a
 * late-subscribed target, then (re)bind its forward consumer at boundary+1.
 * Backed by the P-12 backfill seam (`Backfiller.catch_up_and_forward`) exposed
 * at `POST /api/v1/registry/targets/{target_id}/backfill`.
 *
 * `limit_per_binding` caps the catch-up per binding (the replay is oldest-first
 * across the union of the target's bindings, deduped on signal id).
 */
export async function triggerBackfill(
  targetId: string,
  opts: { limitPerBinding?: number } = {},
): Promise<BackfillResult> {
  return apiPost<BackfillResult>(
    `/registry/targets/${encodeURIComponent(targetId)}/backfill`,
    opts.limitPerBinding != null ? { limit_per_binding: opts.limitPerBinding } : {},
  )
}

// ---------------------------------------------------------------------------
// Consult model picker (F1) — which registered LLM plane answers a consult /
// deep_consult request. "opus" = the billed Anthropic Opus plane (the default,
// preserving today's behavior); "core" = the free self-hosted core plane.
// The registry maps this friendly value → a sanctioned component id server-side.
// Mirrors `CONSULT_MODEL_ALLOWLIST` in `consult_api.py`.
// ---------------------------------------------------------------------------

export type ConsultModel = 'opus' | 'core'

/** Dropdown options — value + operator-facing label (order = display order). */
export const CONSULT_MODEL_OPTIONS: { value: ConsultModel; label: string }[] = [
  { value: 'opus', label: 'Opus (Anthropic · billed)' },
  { value: 'core', label: 'Core (free)' },
]

const CONSULT_MODEL_STORAGE_KEY = 'legba_consult_model'

/** Last-chosen plane, persisted across panel opens; defaults to the Opus plane. */
export function loadConsultModel(): ConsultModel {
  try {
    return localStorage.getItem(CONSULT_MODEL_STORAGE_KEY) === 'core'
      ? 'core'
      : 'opus'
  } catch {
    return 'opus'
  }
}

export function saveConsultModel(model: ConsultModel): void {
  try {
    localStorage.setItem(CONSULT_MODEL_STORAGE_KEY, model)
  } catch {
    // Ignore storage failures (private mode, quota) — the choice still applies
    // for this session, it just won't persist.
  }
}

// ---------------------------------------------------------------------------
// Deep consult (anchor §5 PIECE 4) — the DETACHED staged workflow. Submit
// returns a task id; poll status until completed → a lineage-walkable finding.
// Mirrors `src/legba/data/registry/deep_consult_api.py`.
// ---------------------------------------------------------------------------

export interface DeepConsultSubmit {
  task_id: string
  status: string
  run_id?: string | null
}

export interface DeepConsultStatus {
  task_id: string
  status: 'running' | 'completed' | 'failed' | 'unknown'
  finding_id?: string | null
  answer?: string | null
  uncertainty?: number | null
  cited_refs: string[]
  fact_ids: string[]
  hypothesis_ids: string[]
  detail?: string | null
}

export async function submitDeepConsult(body: {
  question: string
  scope_predicate?: string | null
  emit_facts?: boolean
  emit_hypotheses?: boolean
  // F1 model picker — the LLM plane the deep workflow runs on (default: opus).
  model?: ConsultModel
}): Promise<DeepConsultSubmit> {
  return apiPost<DeepConsultSubmit>('/deep_consult', body)
}

export async function getDeepConsultStatus(
  taskId: string,
): Promise<DeepConsultStatus> {
  return apiGet<DeepConsultStatus>(`/deep_consult/${encodeURIComponent(taskId)}`)
}

// ---------------------------------------------------------------------------
// Consult audit trail (0038) — prior-session history + continue.
// Mirrors `src/legba/data/registry/consult_sessions_api.py`.
// ---------------------------------------------------------------------------

/** One row in the session-history list. */
export interface ConsultSessionSummary {
  id: string
  mode: 'chat' | 'deep'
  title: string
  task_id?: string | null
  run_id?: string | null
  turn_count: number
  created_at?: string | null
  updated_at?: string | null
}

/** One persisted turn, as the client re-seeds it. */
export interface ConsultTurnOut {
  id: string
  role: 'user' | 'assistant'
  content: string
  steps: unknown[]
  tool_calls: unknown[]
  cited_refs: unknown[]
  finding_id?: string | null
  created_at?: string | null
}

/** A session header + its ordered turns. */
export interface ConsultSessionDetail {
  id: string
  mode: 'chat' | 'deep'
  title: string
  task_id?: string | null
  run_id?: string | null
  created_at?: string | null
  updated_at?: string | null
  turns: ConsultTurnOut[]
}

export async function listConsultSessions(
  opts: { limit?: number; mode?: 'chat' | 'deep' } = {},
): Promise<ConsultSessionSummary[]> {
  const params = new URLSearchParams()
  if (opts.limit != null) params.set('limit', String(opts.limit))
  if (opts.mode) params.set('mode', opts.mode)
  const qs = params.toString()
  return apiGet<ConsultSessionSummary[]>(`/consult/sessions${qs ? `?${qs}` : ''}`)
}

export async function loadConsultSession(
  sessionId: string,
): Promise<ConsultSessionDetail> {
  return apiGet<ConsultSessionDetail>(
    `/consult/sessions/${encodeURIComponent(sessionId)}`,
  )
}

// ---------------------------------------------------------------------------
// Journal / Voices (JOURNAL_ASSESSOR_PLAN §9 / Wave 3; Voices panel step 1,
// planning/VOICES_PANEL_SPEC.md §3) — the reflective voice's read surface. The
// open consolidation + recent entries, each cited ref already resolved
// server-side to its (kind, title) so a per-claim provenance chip can
// deep-link via `selectRow(kind, id, label)` without a second round-trip.
// Mirrors `src/legba/data/registry/journal_api.py`.
// ---------------------------------------------------------------------------

/** A cited substrate UUID resolved to its kind + label (chip deep-link target).
 *  `kind='unknown'` when the id resolves in no substrate table (superseded /
 *  pruned ref) — the chip still renders; the citation is never hidden (§9). */
export interface JournalRef {
  id: string
  kind: string
  title?: string | null
}

/** One cited claim — a span of the entry bound to its resolved refs (§3.6).
 *  `kind` is the CLAIM kind ('fact' | 'perspective'), not a substrate kind. A
 *  `[needs_citation]`-prefixed `text_span` is an uncited factual assertion that
 *  slipped the REFLECT flag — rendered in the unverified-perspective style,
 *  NEVER hidden (§4.5). */
export interface JournalClaim {
  text_span: string
  kind: 'fact' | 'perspective' | string
  refs: JournalRef[]
}

/** The `entry_kind` vocabulary the `kind` filter accepts (VOICES_PANEL_SPEC
 *  §3.1). `lens`/`lens_diff` are accepted by the API today (harmless — no such
 *  rows exist pre-LV-1) even though nothing in step 1 generates those chips. */
export type JournalEntryKind =
  | 'entry'
  | 'consolidation'
  | 'chronicle'
  | 'lens'
  | 'lens_diff'
  | string

/** One `journal_entries` row at `fields=full` weight (today's shape, plus
 *  §3.4's verify fields). Mirrors `JournalEntryOut`. */
export interface JournalEntry {
  id: string
  entry_kind: JournalEntryKind
  title: string
  body: string
  claims: JournalClaim[]
  cited_substrate_refs: JournalRef[]
  honesty_flags: string[]
  period_start: string
  period_end: string
  produced_at: string
  analyst_id: string | null
  analyst_version: string | null
  /** §3.4 — the 'Faithfulness verify' critique's gate score. `null` when no
   *  such critique exists yet (never fabricated — the honest-absence pill
   *  renders `—`). */
  verify_score: number | null
  /** §3.4 — the critique body text (full-only), naming each unsupported /
   *  contested span as a `  - [judge_contradicted] ...` /
   *  `  - [judge_unsupported] ...` / `  - [no_citation] ...` line. `null`
   *  when no critique exists. */
  verify_body: string | null
}

/** One `journal_entries` row at `fields=summary` weight (§3.3) — the
 *  grouped-list read. Mirrors `JournalEntrySummaryOut`: a DISTINCT shape (no
 *  `body`/`claims`/`cited_substrate_refs`/`verify_body`), not a partial
 *  `JournalEntry`, so a summary row can never be mistaken for a hydrated one. */
export interface JournalEntrySummary {
  id: string
  entry_kind: JournalEntryKind
  title: string
  honesty_flags: string[]
  period_start: string
  period_end: string
  produced_at: string
  analyst_id: string | null
  analyst_version: string | null
  verify_score: number | null
}

/** The substrate-derived calibration posture for the §10 honesty banner —
 *  keyed off the live calibration metric, NOT a self-reported payload field. */
export interface JournalCalibration {
  available: boolean
  forecast_unproven: boolean
  calibration_thin: boolean
  brier_skill_score: number | null
  exogenous_sample_size: number | null
  forecast_acute_sample_size: number | null
  forecast_acute_status: string | null
  produced_at: string | null
}

/** `GET /journal?fields=full` body (default). Mirrors `JournalOut`. */
export interface JournalResponse {
  consolidation: JournalEntry | null
  entries: JournalEntry[]
  next_cursor: string | null
  calibration: JournalCalibration
}

/** `GET /journal?fields=summary` body — the SAME envelope, summary-weight
 *  rows. Mirrors `JournalSummaryOut`. */
export interface JournalSummaryResponse {
  consolidation: JournalEntrySummary | null
  entries: JournalEntrySummary[]
  next_cursor: string | null
  calibration: JournalCalibration
}

/** Build the shared query string for both fetch functions below — `kind` is
 *  repeatable (`?kind=entry&kind=chronicle`, matching FastAPI's `Query(list)`
 *  binding), everything else is a single value. */
function journalQueryString(opts: {
  limit?: number
  cursor?: string
  kind?: JournalEntryKind[]
  fields?: 'summary' | 'full'
}): string {
  const params = new URLSearchParams()
  if (opts.limit != null) params.set('limit', String(opts.limit))
  if (opts.cursor) params.set('cursor', opts.cursor)
  if (opts.fields) params.set('fields', opts.fields)
  for (const k of opts.kind ?? []) params.append('kind', k)
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

export async function fetchJournal(
  opts: {
    limit?: number
    cursor?: string
    kind?: JournalEntryKind[]
    fields?: 'full'
  } = {},
): Promise<JournalResponse> {
  return apiGet<JournalResponse>(`/journal${journalQueryString(opts)}`)
}

/** `fields=summary` variant — the grouped-list read (§2b/§3.3): list-cheap
 *  rows, no body/claims, `_resolve_refs` never runs server-side. */
export async function fetchJournalSummary(
  opts: { limit?: number; cursor?: string; kind?: JournalEntryKind[] } = {},
): Promise<JournalSummaryResponse> {
  return apiGet<JournalSummaryResponse>(
    `/journal${journalQueryString({ ...opts, fields: 'summary' })}`,
  )
}

/** `GET /journal/{id}` — a single row at full weight, for the reader pane's
 *  on-select fetch (§3.3). */
export async function fetchJournalEntry(id: string): Promise<JournalEntry> {
  return apiGet<JournalEntry>(`/journal/${encodeURIComponent(id)}`)
}

// ---------------------------------------------------------------------------
// System Status panel (#89-adjacent ops surface) — the at-a-glance health view
// the operator asked for: per-analyst cadence + per-source firing. These two
// routes read the TRUTH tables (analyst_traces / signals) rather than
// actor_state (whose last_run_at is NULL), so they reflect what actually ran /
// fired. Mirrors the v3 routes added in `src/legba/data/registry`.
// ---------------------------------------------------------------------------

/** One per-analyst cadence row. Mirrors `GET /api/v1/v3/system/analyst-cadence`.
 *  Sourced from analyst_traces GROUP BY analyst_id, max(run_started_at).
 *  status: 'never' (0 runs) | 'stale' (age > 6h) | 'healthy'. */
export interface AnalystCadenceRow {
  analyst_id: string
  last_run_at: string | null
  age_seconds: number | null
  runs_1h: number
  runs_24h: number
  last_outcome: string | null
  status: 'never' | 'stale' | 'healthy' | string
}

/** One per-source firing row. Mirrors `GET /api/v1/v3/system/source-firing`.
 *  signals (count + max fetched_at by source_id) LEFT JOIN source_poll_outcomes
 *  + source_descriptors. status: 'firing' | 'silent' (active, 0 signals/24h) |
 *  'error' (recent poll errors) | 'paused'. */
export interface SourceFiringRow {
  source_id: string
  state: string | null
  signals_24h: number
  signals_7d: number
  last_seen_at: string | null
  age_seconds: number | null
  last_poll_outcome: string | null
  recent_error_count: number
  status: 'firing' | 'silent' | 'error' | 'paused' | string
}

/** Per-analyst cadence truth (analyst_traces-backed). */
export async function getSystemAnalystCadence(): Promise<AnalystCadenceRow[]> {
  return apiGet<AnalystCadenceRow[]>('/v3/system/analyst-cadence')
}

/** Per-source firing matrix (signals + poll-outcome backed). */
export async function getSystemSourceFiring(): Promise<SourceFiringRow[]> {
  return apiGet<SourceFiringRow[]>('/v3/system/source-firing')
}

/** One escalation-delivery audit row. Mirrors a `public.alert_sink_deliveries`
 *  row (migration 0061) served by `GET /api/v1/v3/system/escalations`.
 *  status: 'delivered' | 'failed' | 'logged_only' | 'retrying'. */
export interface EscalationDeliveryRow {
  id: string
  alert_row_id: string | null
  channel_name: string | null
  sink_kind: string
  sink_target: string | null
  target_id: string | null
  severity: string | null
  effective_confidence: number | null
  status: 'delivered' | 'failed' | 'logged_only' | 'retrying' | string
  error_message: string | null
  attempt_number: number
  attempted_at: string
  delivered_at: string | null
  payload_summary: Record<string, unknown>
}

/** One (sink_kind, status) non-delivery tally over the window — the W1-T3
 *  integrity-sweep canary breakdown (failed / logged_only only). */
export interface EscalationNonDelivery {
  sink_kind: string
  status: 'failed' | 'logged_only'
  n: number
  sample_error: string | null
}

/** 24h rollup over `alert_sink_deliveries` — deliberately UNfiltered so the
 *  health signal is honest regardless of the row-list filter. `non_delivery`
 *  (= failed + logged_only) is the count the canary alarms on. */
export interface EscalationDeliverySummary {
  window_hours: number
  total: number
  delivered: number
  failed: number
  logged_only: number
  retrying: number
  other: number
  non_delivery: number
  by_sink_status: EscalationNonDelivery[]
}

/** `GET /api/v1/v3/system/escalations` payload: a 24h summary + recent rows. */
export interface EscalationDeliveriesResponse {
  summary: EscalationDeliverySummary
  rows: EscalationDeliveryRow[]
}

/** Optional filters for the escalation-delivery read route. */
export interface EscalationDeliveriesQuery {
  status?: string
  sink_kind?: string
  target_id?: string
  severity?: string
  window_hours?: number
  limit?: number
}

/** Recent escalation deliveries + a 24h non-delivery health summary
 *  (audit finding C3 / decision D1 — the human-visible alert edge). */
export async function getSystemEscalations(
  q: EscalationDeliveriesQuery = {},
): Promise<EscalationDeliveriesResponse> {
  const params = new URLSearchParams()
  if (q.status) params.set('status', q.status)
  if (q.sink_kind) params.set('sink_kind', q.sink_kind)
  if (q.target_id) params.set('target_id', q.target_id)
  if (q.severity) params.set('severity', q.severity)
  if (q.window_hours != null) params.set('window_hours', String(q.window_hours))
  if (q.limit != null) params.set('limit', String(q.limit))
  const qs = params.toString()
  return apiGet<EscalationDeliveriesResponse>(
    `/v3/system/escalations${qs ? `?${qs}` : ''}`,
  )
}

// ---------------------------------------------------------------------------
// Validity-window timeline (P4-4) — the `system.timeline` panel's read.
//
// `GET /api/v1/v3/timeline?target_id=&days=` returns facts / situations /
// findings as RANGED items ([start, end|open) + supersession-chain edges) over
// one window. Mirrors `timeline_api.TimelineResponse`; the shape + all pure
// shaping live in `lib/timelineWindows`.
// ---------------------------------------------------------------------------

/** Fetch the ranged validity-window items for the timeline panel. `target_id`
 *  desk-scopes the read; `days` bounds the window (backend caps at 90). */
export async function fetchTimeline(
  opts: { target_id?: string | null; days?: number } = {},
): Promise<TimelineResponse> {
  const params = new URLSearchParams()
  if (opts.target_id) params.set('target_id', opts.target_id)
  if (opts.days != null) params.set('days', String(opts.days))
  const qs = params.toString()
  return apiGet<TimelineResponse>(`/v3/timeline${qs ? `?${qs}` : ''}`)
}
