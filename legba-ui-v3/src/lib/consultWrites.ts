/**
 * C-2b-1 — the consult WRITE plane, the workstation side (design C-2a §9).
 *
 * The registry's `consult_writes` ledger is the authority on what a consult
 * turn changed, staged or was refused: the pending card, the receipt and the
 * reversed receipt all render FROM a ledger row (`GET /consult/writes`), never
 * from the answer's prose. The operator's second word — Confirm, Cancel,
 * Revert — goes straight to the executor with no model in the loop.
 *
 * Absence renders as absence: a cost the receipt did not measure is "cost not
 * measured", an activation not yet probed is "activation pending" — never a
 * zero, never "verified".
 */
import { ApiError, apiGet, apiPost } from './api'

/** A route failure in the operator's words (status + the server's detail). */
export function writeErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body as { detail?: unknown } | string | null
    const detail = typeof body === 'string' ? body : body && 'detail' in body ? body.detail : null
    return `${err.status}: ${typeof detail === 'string' ? detail : JSON.stringify(detail ?? err.message)}`
  }
  return err instanceof Error ? err.message : String(err)
}

/** One entry of a turn's `writes` bag (the reload copy; the ledger is the authority). */
export interface ConsultWriteEntry {
  write_id: string | null
  status: string
  tool?: string | null
  summary?: string | null
  cause?: string | null
}

export interface ConsultWriteReceipt {
  class?: string
  verb?: string
  object?: string
  plane?: string | null
  tier?: 'A' | 'B'
  pre?: { version?: string | null; state?: string | null } | null
  post?: { version?: string | null; state?: string | null } | null
  reversal?: { kind?: string; why?: string } | null
  turn_cost_usd_at_write?: number | null
  confirmed_via?: string | null
  activation?: ConsultWriteActivation | null
}

export interface ConsultWriteActivation {
  status: string
  at?: string | null
  first_poll_at?: string | null
  /** A unit / composition head: its first traced run at the new version (W6). */
  first_run_at?: string | null
  /** A rerun: the forced run's own trace (`run_landed`), or why none landed. */
  run_started_at?: string | null
  trace_status?: string | null
  error?: string | null
  rolled_back_to?: string | null
  why?: string | null
}

/** One `consult_writes` row as `GET /consult/writes` serves it. */
export interface ConsultWriteRow {
  id: string
  session_id: string | null
  request_id: string | null
  turn_index: number | null
  class: string
  verb: string
  object_ref: string
  tier: 'A' | 'B'
  /** The stored canonical args — what a confirm executes (never re-asked of the model). */
  args?: { to_state?: string; patch?: Record<string, unknown> } | null
  status: 'staged' | 'pending_confirmation' | 'executed' | 'refused' | 'failed' | 'cancelled' | 'expired'
  expires_at: string | null
  confirmed_via: string | null
  pre_image: { version?: string | null; state?: string | null } | null
  post_image: { version?: string | null; state?: string | null } | null
  reversal: { kind?: string; why?: string } | null
  reverses_write_id: string | null
  reversed_by_write_id: string | null
  activation: ConsultWriteActivation | null
  error: { cause?: string | null; detail?: string | null } | null
  receipt: ConsultWriteReceipt | null
  created_at: string | null
  executed_at: string | null
  /** Present on a pending row only while the registry process holds it. */
  confirm_code?: string
}

export interface ConsultWriteActResult {
  status: string
  write_id?: string
  cause?: string
  detail?: string
  line?: string
}

const BASE = '/consult/writes'

export async function listConsultWrites(filter: {
  sessionId?: string | null
  requestId?: string | null
}): Promise<ConsultWriteRow[]> {
  const params = new URLSearchParams()
  if (filter.sessionId) params.set('session_id', filter.sessionId)
  if (filter.requestId) params.set('request_id', filter.requestId)
  const body = await apiGet<{ writes: ConsultWriteRow[] }>(`${BASE}?${params.toString()}`)
  return body.writes ?? []
}

export function confirmConsultWrite(id: string, sessionId?: string | null): Promise<ConsultWriteActResult> {
  return apiPost(`${BASE}/${encodeURIComponent(id)}/confirm`, sessionId ? { session_id: sessionId } : {})
}

export function cancelConsultWrite(id: string, sessionId?: string | null): Promise<ConsultWriteActResult> {
  return apiPost(`${BASE}/${encodeURIComponent(id)}/cancel`, sessionId ? { session_id: sessionId } : {})
}

export function revertConsultWrite(id: string, sessionId?: string | null): Promise<ConsultWriteActResult> {
  return apiPost(`${BASE}/${encodeURIComponent(id)}/revert`, sessionId ? { session_id: sessionId } : {})
}

// ---------------------------------------------------------------------------
// Pure renderers (tested)
// ---------------------------------------------------------------------------

const VERB_WORDS: Record<string, string> = {
  update: 'Update',
  create: 'Create',
  retire: 'Retire',
  revert: 'Revert',
}

function short(version?: string | null): string {
  return version ? `${version.slice(0, 4)}…` : '—'
}

/** `hh:mmZ` of an ISO instant; `—` when absent or unparseable. */
export function clockZ(iso?: string | null): string {
  if (!iso) return '—'
  const t = new Date(iso)
  if (Number.isNaN(t.getTime())) return '—'
  return `${t.toISOString().slice(11, 16)}Z`
}

function verbPhrase(row: ConsultWriteRow): string {
  if (row.verb === 'transition') {
    const to = row.receipt?.post?.state ?? row.args?.to_state ?? null
    if (to === 'paused') return 'Pause'
    if (to === 'active') return 'Activate'
    return to ? `Move to ${to}` : 'Transition'
  }
  return VERB_WORDS[row.verb] ?? row.verb
}

/** The amber pending card's sentence, from the ledger row alone. */
export function pendingCardText(row: ConsultWriteRow): string {
  const pre = row.pre_image
  const head = pre ? ` (head ${short(pre.version)}, ${pre.state ?? 'state not recorded'})` : ''
  const terminal = row.reversal?.kind === 'none' ? ' · cannot be undone' : ''
  return `${verbPhrase(row)} ${row.class} ${row.object_ref}${head}${terminal}`
}

/** "expires 14:32Z" — or "expired" once past. */
export function expiryLabel(row: ConsultWriteRow, now: number = Date.now()): string {
  if (!row.expires_at) return 'expiry not recorded'
  const at = new Date(row.expires_at).getTime()
  if (Number.isNaN(at)) return 'expiry not recorded'
  return at <= now ? 'expired' : `expires ${clockZ(row.expires_at)}`
}

/** A rerun (W6) has no new head to activate: its stamp is the forced run itself. */
function runText(a: ConsultWriteActivation | null | undefined): string {
  if (!a) return 'run pending'
  switch (a.status) {
    case 'run_landed':
      return `run landed ${clockZ(a.run_started_at ?? a.at)}${a.trace_status ? ` (${a.trace_status})` : ''}`
    case 'run_not_landed':
      return `run not landed${a.why ? ` (${a.why})` : ''}`
    case 'run_failed':
      return `run failed${a.error ? `: ${a.error}` : ''}`
    default:
      return `run ${a.status}`
  }
}

function activationText(a: ConsultWriteActivation | null | undefined): string {
  if (!a) return 'activation pending'
  switch (a.status) {
    case 'verified':
      return `activation verified ${clockZ(a.first_poll_at ?? a.first_run_at ?? a.at)}`
    case 'parse_failed':
      return `activation: parse_failed — rolled back${a.rolled_back_to ? ` to ${short(a.rolled_back_to)}` : ''}`
    case 'not_applicable':
      return `activation n/a${a.why ? ` (${a.why})` : ''}`
    case 'not_observed':
      return 'activation not observed in 3 min'
    case 'superseded':
      return 'activation: head moved before the probe'
    default:
      return `activation ${a.status}`
  }
}

/** The receipt line of an executed write. */
export function receiptText(row: ConsultWriteRow): string {
  const r = row.receipt ?? {}
  const pre = r.pre ?? row.pre_image
  const post = r.post ?? row.post_image
  const move = pre
    ? `${short(pre.version)}→${short(post?.version)}${pre.state !== post?.state ? ` (${pre.state}→${post?.state})` : ''}`
    : `created ${short(post?.version)}`
  const cost =
    typeof r.turn_cost_usd_at_write === 'number' ? `$${r.turn_cost_usd_at_write.toFixed(2)}` : 'cost not measured'
  const what =
    row.verb === 'revert' ? 'reverted' : row.verb === 'transition' ? 'moved' : row.verb === 'rerun' ? 'reran' : `${row.verb}d`
  const stamp = row.verb === 'rerun' ? runText : activationText
  return [
    `Written · ${row.class} ${row.object_ref} ${what} ${move}`,
    r.plane ?? 'plane not recorded',
    cost,
    clockZ(row.executed_at),
    stamp(row.activation ?? r.activation),
  ].join(' · ')
}

/** "Not written · stale_read: head moved since it was read". */
export function refusalText(row: ConsultWriteRow): string {
  const cause = row.error?.cause
  const detail = row.error?.detail
  if (row.status === 'expired') return `Not written · expired${detail ? `: ${detail}` : ''}`
  if (row.status === 'cancelled') return `Not written · cancelled${detail ? `: ${detail}` : ''}`
  return `Not written · ${cause ? `${cause}: ` : ''}${detail ?? row.status}`
}

/** The truth line the server ends every armed turn with, recomputed from bag entries. */
export function truthLine(entries: ConsultWriteEntry[] | null | undefined): string | null {
  if (entries == null) return null
  if (entries.length === 0) return 'writes: none'
  let executed = 0
  let staged = 0
  let refused = 0
  for (const e of entries) {
    if (e.status === 'executed') executed += 1
    else if (e.status === 'pending_confirmation') staged += 1
    else refused += 1
  }
  return `writes: executed ${executed} · staged ${staged} · refused ${refused}`
}
