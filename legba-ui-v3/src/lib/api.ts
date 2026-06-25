/**
 * Thin REST client for the legba-registry API.
 *
 * Bearer-token is read from `localStorage.legba_token` (set by `auth/jwt.ts`).
 * Errors surface as thrown `ApiError`s with status + parsed body.
 */

import type { PanelRegistration, Mode } from '@/types'

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

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: 'application/json', ...authHeaders() },
  })
  if (!res.ok) {
    throw new ApiError(res.status, await readErrorBody(res))
  }
  return res.json() as Promise<T>
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
// Journal (JOURNAL_ASSESSOR_PLAN §9 / Wave 3) — the reflective voice's read
// surface. The open consolidation + recent entries, each cited ref already
// resolved server-side to its (kind, title) so a per-claim provenance chip can
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

/** One `journal_entries` row hydrated for the panel. */
export interface JournalEntry {
  id: string
  entry_kind: 'entry' | 'consolidation' | string
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

/** `GET /journal` body. Mirrors `JournalOut`. */
export interface JournalResponse {
  consolidation: JournalEntry | null
  entries: JournalEntry[]
  next_cursor: string | null
  calibration: JournalCalibration
}

export async function fetchJournal(
  opts: { limit?: number; cursor?: string } = {},
): Promise<JournalResponse> {
  const params = new URLSearchParams()
  if (opts.limit != null) params.set('limit', String(opts.limit))
  if (opts.cursor) params.set('cursor', opts.cursor)
  const qs = params.toString()
  return apiGet<JournalResponse>(`/journal${qs ? `?${qs}` : ''}`)
}
