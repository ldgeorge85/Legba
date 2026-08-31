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
 *  'error' (recent poll errors) | 'paused'.
 *
 *  A7 (additive): `freshness_grade` grades the freshest signal's age against a
 *  budget derived from the source's OWN declared cadence (see
 *  `@/lib/sourceFreshness` + `legba.data.registry.source_freshness`) —
 *  'ok' | 'stale' | 'warn' | 'empty' | 'ungraded'. `budget_minutes` is `null`
 *  exactly when no honest budget was derivable (never a fabricated grade). */
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
  freshness_grade: 'ok' | 'stale' | 'warn' | 'empty' | 'ungraded' | string
  budget_minutes: number | null
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

// ---------------------------------------------------------------------------
// Journal proposals — THE HUMAN GATE (JOURNAL_ASSESSOR_PLAN §7.4 / §7.5).
//
// The journal SUGGESTS into `journal_proposals`; a human DISPOSES. Until this
// train the three routes had no UI at all, so the standing "journal writes are
// human-gated" rule was reachable only by curl. Mirrors
// `src/legba/data/registry/journal_proposals_api.py` 1:1.
// ---------------------------------------------------------------------------

/** §7.5(a) — the OBJECTIVE evidence attached to a `self_revision` proposal: the
 *  journal's OWN recent calibration + critic track record, so a beautifully
 *  argued self-revision is judged against what it has actually earned rather
 *  than against its prose. Present ONLY on `proposal_kind === 'self_revision'`
 *  (null everywhere else — never fabricated). Mirrors `CalibrationEvidence`. */
export interface ProposalSelfRevisionEvidence {
  available: boolean
  forecast_unproven: boolean
  calibration_thin: boolean
  brier_skill_score: number | null
  journal_critic_mean: number | null
  journal_critic_n: number
}

/** The three gated classes. `correction` retires a stale fact through the
 *  supersession path, `change` patches a descriptor/stack head, `self_revision`
 *  promotes a new system prompt for an analyst. Mirrors the apply worker's
 *  dispatch (`journal_proposals_apply.apply_accepted_proposal`). */
export type ProposalKind = 'correction' | 'change' | 'self_revision' | string

/** `pending` is the only actionable state; `archived` is where an apply failure
 *  or a §7.5(b) auto-reject lands (NOT an operator verdict). */
export type ProposalStatus =
  | 'pending'
  | 'accepted'
  | 'rejected'
  | 'archived'
  | string

/** One `journal_proposals` row. Mirrors `ProposalOut`. */
export interface JournalProposal {
  id: string
  proposal_kind: ProposalKind
  proposed_by_analyst_id: string
  run_id: string | null
  rationale: string
  /** The operation accepting would APPLY — the shape varies by kind; see
   *  `lib/journalGate.ts`, which renders it as what it would DO. */
  diff: Record<string, unknown>
  cited_substrate_refs: string[]
  status: ProposalStatus
  decided_by: string | null
  decision_reason: string | null
  decided_at: string | null
  produced_at: string
  self_revision_evidence: ProposalSelfRevisionEvidence | null
}

/** `GET /journal_proposals` envelope. Mirrors `ProposalsListOut`. */
export interface JournalProposalsResponse {
  proposals: JournalProposal[]
}

/** The recorded outcome of an accept/reject. `applied` is the apply worker's
 *  audit on a FRESH accept (null on reject and on a replay); `replayed` is true
 *  when the row was already decided — the idempotent no-op path, which is a
 *  real outcome the operator must see, not an error. Mirrors `DecisionOut`. */
export interface ProposalDecision {
  id: string
  status: ProposalStatus
  decided_by: string | null
  decision_reason: string | null
  applied: Record<string, unknown> | null
  replayed: boolean
}

export async function fetchJournalProposals(
  opts: { status?: ProposalStatus; limit?: number } = {},
): Promise<JournalProposalsResponse> {
  const params = new URLSearchParams()
  if (opts.status) params.set('status', opts.status)
  if (opts.limit != null) params.set('limit', String(opts.limit))
  const qs = params.toString()
  return apiGet<JournalProposalsResponse>(`/journal_proposals${qs ? `?${qs}` : ''}`)
}

/** Accept + APPLY. The journal proposed; this call is the human CAUSING it.
 *  Non-2xx throws an `ApiError` that must be surfaced verbatim — a 409 is the
 *  §7.5(b) protected-section auto-reject and a 422 is an apply failure; both
 *  leave the row `archived`, and neither is a success. */
export async function acceptJournalProposal(
  id: string,
  decisionReason?: string,
): Promise<ProposalDecision> {
  // OPTIONAL by design, unlike reject's. The asymmetry is the server's: a
  // refusal is only legible through its reason, whereas an accept is already
  // described by the diff it applied. Blank/whitespace is normalized to NULL
  // server-side, so sending `{}` (the shape this call used before the reason
  // existed) stays exactly as valid as it was.
  const reason = decisionReason?.trim()
  return apiPost<ProposalDecision>(
    `/journal_proposals/${encodeURIComponent(id)}/accept`,
    reason ? { decision_reason: reason } : {},
  )
}

/** Reject → `rejected`, WITH the server-required decision reason (the API 422s
 *  on an empty one; the panel blocks the click before it gets that far). */
export async function rejectJournalProposal(
  id: string,
  decisionReason: string,
): Promise<ProposalDecision> {
  return apiPost<ProposalDecision>(
    `/journal_proposals/${encodeURIComponent(id)}/reject`,
    { decision_reason: decisionReason },
  )
}

// ---------------------------------------------------------------------------
// Situation register + trajectory (Continuity P2) — the frames, and how each
// one MOVED. Mirrors `situation_trajectory_api.py` + the `/situations` read.
// ---------------------------------------------------------------------------

/** One frame in the register, as `GET /api/v1/situations` serves it. A partial
 *  of the server's `SituationRow` — only the fields this surface renders. */
export interface SituationFrame {
  id: string
  name: string
  status: string
  category: string
  last_event_at: string | null
  event_count: number
  intensity_score: number
  target_id: string | null
  produced_at: string
}

export interface SituationsPage {
  data: SituationFrame[]
  next_cursor: string | null
}

/** One append-only trajectory ledger row, exactly as written — the route
 *  synthesizes NOTHING across rows. Mirrors `TrajectoryEventOut`. */
export interface TrajectoryEvent {
  id: string
  /** `escalates` | `de_escalates` | `broadens` | `unchanged_checkpoint`. */
  delta: string
  /** EVIDENCE time — the newest backing finding's `produced_at`, NOT the time
   *  the tracker ran (that is `created_at`). */
  occurred_at: string | null
  state_from: string
  state_to: string
  why: string
  /** The NEW findings that moved it. Empty only on `unchanged_checkpoint`. */
  derived_from: string[]
  /** The graded `situation_update` finding whose prose asserted this delta. */
  source_output_id: string
  created_at: string | null
}

/** Mirrors `SituationTrajectoryOut`. The honesty contract lives in the shape:
 *  `measured: false` means "we could not look", `measured: true` with an empty
 *  `events` means "nothing recorded", and `state: null` is never backfilled
 *  with a fabricated default. `lib/trajectoryModel.ts` keeps the three apart. */
export interface SituationTrajectory {
  situation_id: string
  name: string
  state: string | null
  events: TrajectoryEvent[]
  measured: boolean
}

export async function fetchSituationFrames(
  opts: { limit?: number; state?: string } = {},
): Promise<SituationsPage> {
  const params = new URLSearchParams()
  if (opts.limit != null) params.set('limit', String(opts.limit))
  if (opts.state) params.set('state', opts.state)
  const qs = params.toString()
  return apiGet<SituationsPage>(`/situations${qs ? `?${qs}` : ''}`)
}

/** An UNKNOWN situation is a 404 (an `ApiError`) — deliberately distinct from a
 *  known frame with an empty ledger, which is a 200 with `events: []`. */
export async function fetchSituationTrajectory(
  situationId: string,
  opts: { limit?: number } = {},
): Promise<SituationTrajectory> {
  const qs = opts.limit != null ? `?limit=${opts.limit}` : ''
  return apiGet<SituationTrajectory>(
    `/v3/situations/${encodeURIComponent(situationId)}/trajectory${qs}`,
  )
}

// ---------------------------------------------------------------------------
// Reified narratives (P4-1 / P4-2; A11) — contested-claim families + the
// directed source-echo graph. Mirrors `narratives_api.py`.
// ---------------------------------------------------------------------------

/** One reified contested-claim family. Mirrors `NarrativeOut`. */
export interface Narrative {
  contention_id: string
  subject_key: string
  predicate_key: string
  status: string
  surfaced_value: string | null
  variant_count: number
  carrier_source_count: number
  publish_dated_source_count: number
  signal_count: number
  fact_count: number
  first_seen_at: string | null
  last_seen_at: string | null
  span_hours: number | null
  lead_source_id: string | null
  lead_first_seen_at: string | null
  max_echo_lag_hours: number | null
  carriers: Array<Record<string, unknown>>
  variants: Array<Record<string, unknown>>
  opened_at: string | null
  contention_surfaced_at: string | null
  computed_at: string | null
}

/** One directed source-echo edge. Mirrors `PropagationEdgeOut`. DESCRIPTIVE
 *  co-carriage timing only — see `honesty_note` on the envelope. */
export interface NarrativeEchoEdge {
  leader_source_id: string
  follower_source_id: string
  co_carried: number
  lead_count: number
  follow_within_count: number
  echo_ratio: number | null
  median_lag_hours: number | null
  mean_lag_hours: number | null
  min_lag_hours: number | null
  max_lag_hours: number | null
  echo_window_hours: number
  systematic: boolean
  computed_at: string | null
}

/** Both envelopes carry `honesty_note` verbatim from the server. The panel
 *  RENDERS it rather than paraphrasing — the server attaches it precisely so a
 *  client cannot present echo-lead as a causal or coordination claim. */
export interface NarrativeListResponse {
  narratives: Narrative[]
  count: number
  honesty_note?: string
}

export interface NarrativeEchoResponse {
  edges: NarrativeEchoEdge[]
  count: number
  honesty_note?: string
}

export async function fetchNarratives(
  opts: { status?: string; minCarriers?: number; limit?: number } = {},
): Promise<NarrativeListResponse> {
  const params = new URLSearchParams()
  if (opts.status) params.set('status', opts.status)
  if (opts.minCarriers != null) params.set('min_carriers', String(opts.minCarriers))
  if (opts.limit != null) params.set('limit', String(opts.limit))
  const qs = params.toString()
  return apiGet<NarrativeListResponse>(`/v3/narratives${qs ? `?${qs}` : ''}`)
}

export async function fetchNarrativeEcho(
  opts: {
    systematicOnly?: boolean
    leader?: string
    follower?: string
    limit?: number
  } = {},
): Promise<NarrativeEchoResponse> {
  const params = new URLSearchParams()
  if (opts.systematicOnly) params.set('systematic_only', 'true')
  if (opts.leader) params.set('leader', opts.leader)
  if (opts.follower) params.set('follower', opts.follower)
  if (opts.limit != null) params.set('limit', String(opts.limit))
  const qs = params.toString()
  return apiGet<NarrativeEchoResponse>(`/v3/narratives/echo${qs ? `?${qs}` : ''}`)
}

// ---------------------------------------------------------------------------
// GLASS-3 — the ops deck.
//
// Seven server surfaces that existed and were consumed by NOTHING, plus one new
// one. Mirrors, field for field: `production_gauge_api.py`,
// `v3_api.py::StalenessDebtOut`, `source_quality_api.py`,
// `source_assurance_api.py`, `v3_api.py::DeskBaselineBoard`,
// `since_api.py::BandTrajectoryResponse`, `v3_api.py::eval_analyst_runtime`,
// and the new `judge_stats_api.py`.
//
// The `/system/*` family NEVER 500s on a read failure — it returns its own
// all-defaults payload at HTTP 200 with `measured: false`. So `measured` is the
// field that separates "the engine is quiet" from "we could not look", and a
// panel that ignores it renders a failed read as an all-clear.
// ---------------------------------------------------------------------------

/** One gauged production loop. `ratio` is null EXACTLY when `state` is
 *  `ungauged` — "no expectation" must never be readable as a measured 0.0. */
export interface ProductionGaugeRow {
  loop_class: string
  loop_id: string
  label: string
  state: 'ok' | 'deficit' | 'ungauged' | string
  severity: string
  ratio: number | null
  expected: string
  actual: string
  quiet_reason: string | null
  last_production_at: string | null
  /** Whether THIS row would page — the same predicate the alert plane uses. */
  pages: boolean
  /** Shape varies by `loop_class` on purpose; never assume keys. */
  evidence: Record<string, unknown>
}

/** Totals are computed over the FULL read BEFORE any filter, so a filtered
 *  request cannot lie about its denominator. Never derive one from `loops`. */
export interface ProductionGaugeTotals {
  loops: number
  gauged: number
  ok: number
  deficit: number
  ungauged: number
  paging: number
  by_severity: Record<string, number>
  by_class: Record<string, Record<string, number>>
}

export interface ProductionGaugeResponse {
  generated_at: string | null
  window_days: number
  /** The alert floor, published so a reader can see WHICH rows page without
   *  guessing at the alert plane's private threshold. */
  alert_min_severity: string
  totals: ProductionGaugeTotals
  loops: ProductionGaugeRow[]
  measured: boolean
}

export async function fetchProductionGauge(
  opts: {
    loopClass?: string
    state?: string
    deficitsOnly?: boolean
    pagingOnly?: boolean
    windowDays?: number
    limit?: number
  } = {},
): Promise<ProductionGaugeResponse> {
  const params = new URLSearchParams()
  if (opts.loopClass) params.set('loop_class', opts.loopClass)
  if (opts.state) params.set('state', opts.state)
  if (opts.deficitsOnly) params.set('deficits_only', 'true')
  if (opts.pagingOnly) params.set('paging_only', 'true')
  if (opts.windowDays != null) params.set('window_days', String(opts.windowDays))
  if (opts.limit != null) params.set('limit', String(opts.limit))
  const qs = params.toString()
  return apiGet<ProductionGaugeResponse>(
    `/v3/system/production-gauge${qs ? `?${qs}` : ''}`,
  )
}

export interface StalenessDebtReason {
  reason: string
  open_flags: number
}

/** `match_verified` is hard-false on the wire today — render it as a CAVEAT,
 *  never as a checkmark. */
export interface StalenessDebtResponse {
  staleness_debt: number
  open_flags: number
  superseded_consumer_flags: number
  flagged_consumers: number
  moved_foundations: number
  closed_flags: number
  oldest_open_at: string | null
  newest_open_at: string | null
  by_reason: StalenessDebtReason[]
  last_matcher_run_at: string | null
  match_verified: boolean
}

export async function fetchStalenessDebt(): Promise<StalenessDebtResponse> {
  return apiGet<StalenessDebtResponse>('/v3/system/staleness-debt')
}

/** What a source ASSERTS about itself (admiralty grade, dossier, host score) —
 *  claims, not evidence. Weigh against `earned`. */
export interface AssertedQuality {
  admiralty_reliability: string | null
  admiralty_credibility: string | null
  admiralty_grade: string | null
  admiralty_rater: string | null
  admiralty_method: string | null
  admiralty_rated_at: string | null
  public_rating_count: number
  private_rating_count: number
  has_dossier: boolean
  dossier_compiled_at: string | null
  dossier_compiled_by: string | null
  host_matched: string | null
  host_score: number | null
  host_tier: string | null
  host_state_affiliation: boolean | null
  host_rationale: string | null
  host_scored_by: string | null
  host_scored_at: string | null
}

/** What a source EARNED — its contested-claim track record. `low_sample` is the
 *  honesty flag: a 100% win rate over two contests is not a 100% win rate. */
export interface SourceEarned {
  wins: number
  losses: number
  contested_total: number
  win_rate_raw: number | null
  win_rate_smoothed: number
  win_rate_lower: number
  low_sample: boolean
  corroborated: number
  corroboration_total: number
  corroboration_rate: number | null
  lag_hours: number
  sample_as_of: string
  computed_at: string
}

export interface ComputedQuality {
  freshness_grade: string
  budget_minutes: number | null
  cadence_raw: string | null
  last_signal_at: string | null
  age_seconds: number | null
  signals_24h: number
  signals_7d: number
}

/** `earned` is null ONLY when no track-record row exists at all — a row with
 *  `contested_total: 0` is still returned, and means something different. */
export interface SourceQualityRow {
  source_id: string
  registered: boolean
  declared_state: string | null
  declared_kind: string | null
  endpoint_host: string | null
  asserted: AssertedQuality
  earned: SourceEarned | null
  computed: ComputedQuality
}

/** NOTE: a BARE ARRAY on the wire, not an envelope. 503 when migration 0115's
 *  `source_quality` view is absent. */
export async function fetchSourceQuality(
  opts: {
    sourceId?: string
    gradedOnly?: boolean
    contestedOnly?: boolean
    freshnessGrade?: string
    limit?: number
  } = {},
): Promise<SourceQualityRow[]> {
  const params = new URLSearchParams()
  if (opts.sourceId) params.set('source_id', opts.sourceId)
  if (opts.gradedOnly) params.set('graded_only', 'true')
  if (opts.contestedOnly) params.set('contested_only', 'true')
  if (opts.freshnessGrade) params.set('freshness_grade', opts.freshnessGrade)
  if (opts.limit != null) params.set('limit', String(opts.limit))
  const qs = params.toString()
  return apiGet<SourceQualityRow[]>(`/v3/source-quality${qs ? `?${qs}` : ''}`)
}

export interface SourceRating {
  rating_id: string
  source_id: string
  rater: string
  visibility_class: string
  method: string
  admiralty_reliability: string | null
  admiralty_credibility: string | null
  grade: string | null
  rubric: Record<string, unknown>
  /** Wire spelling is `references`; the DB column is `refs`. */
  references: Array<Record<string, unknown>>
  rated_at: string
}

export interface SourceDossier {
  dossier_id: string
  source_id: string
  dossier_md: string
  references: Array<Record<string, unknown>>
  compiled_by: string
  compiled_at: string
}

export interface SourceQualityDetail extends SourceQualityRow {
  includes_private: boolean
  ratings: SourceRating[]
  dossier: SourceDossier | null
}

/** Per-source drill-down — the C3 LIVE successor. The older
 *  `/sources/{id}/assurance` carries `Deprecation`/`Sunset` headers and is
 *  deliberately not wired here: consuming a sunset route in a NEW panel would
 *  hand the ops deck a migration it does not need. */
export async function fetchSourceQualityDetail(
  sourceId: string,
  opts: { includePrivate?: boolean } = {},
): Promise<SourceQualityDetail> {
  const qs = opts.includePrivate ? '?include_private=true' : ''
  return apiGet<SourceQualityDetail>(
    `/v3/sources/${encodeURIComponent(sourceId)}/quality${qs}`,
  )
}

/** A descriptive statistical baseline over our OWN substrate — explicitly NOT a
 *  forecast or a skill claim. The server ships that disclaimer in `note`; the
 *  panel renders it rather than paraphrasing. */
export interface DeskBaselineRow {
  desk_id: string
  metric: string
  geo: string[]
  baseline_days: number
  n_sigma: number
  expected: number
  center_median: number
  robust_sigma: number
  band_low: number
  band_high: number
  current: number
  deviation: 'within' | 'above' | 'below' | string
  deviation_sigma: number | null
  min_current_floor: number
  sample_days: number
  active_days: number
  insufficient_history: boolean
  spillover_current: number
  features: Record<string, unknown>
  computed_at: string | null
}

export interface DeskBaselineBoard {
  available: boolean
  computed_at: string | null
  note: string
  counts: Record<string, number>
  rows: DeskBaselineRow[]
}

export async function fetchDeskBaselines(
  opts: { desk?: string; deviatingOnly?: boolean } = {},
): Promise<DeskBaselineBoard> {
  const params = new URLSearchParams()
  if (opts.desk) params.set('desk', opts.desk)
  if (opts.deviatingOnly) params.set('deviating_only', 'true')
  const qs = params.toString()
  return apiGet<DeskBaselineBoard>(`/v3/eval/desk_baselines${qs ? `?${qs}` : ''}`)
}

export interface TrajectoryPoint {
  ts: string
  band: string
  effective_confidence: number | null
  faithfulness_flagged: boolean
  scorecard_row_id: string
}

export interface DeskTrajectory {
  target_id: string
  dimensions: Record<string, TrajectoryPoint[]>
}

/** `total_rows` counts SCORECARD ROWS scanned (post-cap), not points; when
 *  `truncated` the LAST desk group may be incomplete. */
export interface BandTrajectoryResponse {
  days: number
  server_now: string
  desks: DeskTrajectory[]
  total_rows: number
  truncated: boolean
}

/** `days` outside [1, 90] is a 400 from the server, not a clamp. */
export async function fetchBandTrajectory(
  opts: { targetId?: string; days?: number } = {},
): Promise<BandTrajectoryResponse> {
  const params = new URLSearchParams()
  if (opts.targetId) params.set('target_id', opts.targetId)
  if (opts.days != null) params.set('days', String(opts.days))
  const qs = params.toString()
  return apiGet<BandTrajectoryResponse>(
    `/v3/eval/band_trajectory${qs ? `?${qs}` : ''}`,
  )
}

/** The one board with NO response_model server-side — the handler's dict
 *  literal IS the contract, and `window_hours` is echoed on every row. Unlike
 *  its siblings it has no degradation wrapper: a DB failure here really is a
 *  500, so the panel must surface the error rather than render empty. */
export interface AnalystRuntimeRow {
  analyst_id: string
  runs: number
  avg_seconds: number | null
  max_seconds: number | null
  last_run_at: string
  non_success: number
  window_hours: number
}

export async function fetchAnalystRuntime(
  opts: { windowHours?: number } = {},
): Promise<AnalystRuntimeRow[]> {
  const params = new URLSearchParams()
  if (opts.windowHours != null) params.set('window_hours', String(opts.windowHours))
  const qs = params.toString()
  return apiGet<AnalystRuntimeRow[]>(`/v3/eval/analyst_runtime${qs ? `?${qs}` : ''}`)
}

// --- Judge stats (the track's one NEW API) ---------------------------------
//
// `served_by` is the upstream provider a router actually dispatched to. It was
// recorded on every receipt from 2026-08-16 and read by nothing, while a
// provider change was measured to flip 13.6% of verdicts. Four sentinel labels
// stand in where attribution is impossible; their meanings arrive on the wire in
// `sentinels`, so nothing here hardcodes the glossary.

export interface JudgeStatsCell {
  day: string
  served_by: string
  judge_status: string
  judge_pipeline_version: string
  n: number
  faithfulness_n: number
  faithfulness_mean: number | null
}

/** `n` counts CRITIQUES attributed to this provider; `judge_calls` counts
 *  RECEIPTS it served. Different grains on purpose — a run that flipped provider
 *  mid-way contributes calls to each real provider but its verdicts to
 *  `(mixed)`, so a provider can legitimately carry calls with `n: 0`. */
export interface JudgeStatsProvider {
  served_by: string
  is_sentinel: boolean
  n: number
  by_status: Record<string, number>
  adjudicated_n: number
  adjudicated_share: number | null
  faithfulness_n: number
  faithfulness_mean: number | null
  judge_calls: number
  judge_call_errors: number
  latency_p95_ms: number | null
  /** Bounds of the RUNS this provider served, not of the individual calls —
   *  see the server model's note on why the per-call timestamp is not used. */
  first_call_at: string | null
  last_call_at: string | null
}

export interface JudgePipelineVersionRow {
  judge_pipeline_version: string
  n: number
  providers: string[]
  faithfulness_n: number
  faithfulness_mean: number | null
}

/** `attributed + unattributed === critiques` always. The two are reported
 *  separately because "served by a named provider" and "we could not say" are
 *  different statements. */
export interface JudgeStatsTotals {
  critiques: number
  by_status: Record<string, number>
  attributed: number
  unattributed: number
  providers: number
  adjudicated_n: number
  adjudicated_share: number | null
  faithfulness_n: number
  faithfulness_mean: number | null
  judge_calls: number
  judge_call_errors: number
}

export interface JudgeStatsResponse {
  generated_at: string | null
  window_days: number
  measured: boolean
  /** True when the window straddles a judge-pipeline stamp change — the pooled
   *  mean is then an average over two different graders. */
  pools_across_pipeline_versions: boolean
  totals: JudgeStatsTotals
  providers: JudgeStatsProvider[]
  pipeline_versions: JudgePipelineVersionRow[]
  cells: JudgeStatsCell[]
  sentinels: Record<string, string>
  judge_statuses: string[]
}

export async function fetchJudgeStats(
  opts: { days?: number; analystId?: string } = {},
): Promise<JudgeStatsResponse> {
  const params = new URLSearchParams()
  if (opts.days != null) params.set('days', String(opts.days))
  if (opts.analystId) params.set('analyst_id', opts.analystId)
  const qs = params.toString()
  return apiGet<JudgeStatsResponse>(`/v3/system/judge-stats${qs ? `?${qs}` : ''}`)
}

// ---------------------------------------------------------------------------
// Read telemetry rollup (D2e) — the oracle wager's scoreboard.
// ---------------------------------------------------------------------------

/** One (day, kind) cell of the read rollup. */
export interface ReadRollupDay {
  day: string
  event_kind: string
  events: number
  sessions: number
}

/** The wager's headline scalars, over the same window as the cells. */
export interface ReadRollupTotals {
  reads_today: number
  reads_this_week: number
  brief_reads_today: number
  brief_reads_this_week: number
  /** Days in the window on which the Morning Read was opened AT ALL. */
  brief_read_days: number
  /** Days in the window on which anything at all was read. */
  active_days: number
  sessions_this_week: number
  window_days: number
}

export interface ReadRollupResponse {
  since: string
  totals: ReadRollupTotals
  days: ReadRollupDay[]
}

export async function fetchReadRollup(
  opts: { days?: number } = {},
): Promise<ReadRollupResponse> {
  const qs = opts.days != null ? `?days=${encodeURIComponent(String(opts.days))}` : ''
  return apiGet<ReadRollupResponse>(`/read-events/rollup${qs}`)
}
