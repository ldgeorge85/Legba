/**
 * The Flow — live telemetry (F.C). A side-effect hook the orchestrator calls
 * once in `flow/index.tsx`; it wires the batched live tail + a couple of polled
 * REST signals into `useFlowState.mergeTelemetry`, keyed by **descriptorId** so
 * the canvas (F.B) can paint node/edge health from the same store.
 *
 * Five inputs converge:
 *   - `legba.signals.>`     → ratePerSec per source (rolling, last batch)
 *   - `analyst.*.finding`   → fires per analyst (cumulative counter)
 *   - `legba.dlq.>`         → errors per descriptor (cumulative)
 *   - `governor.events.>`   → errors per pack on `decision==='block'` (cumulative)
 *   - GET /budget/ledger    → budgetSpent (tokens) per analyst (~30s poll)
 *   - GET /registry/sources + /v3/runtime/actors → stalled per source (~30s poll)
 *
 * Hook order is fixed: every `useBatchedTail` / `useQuery` / `useEffect` runs
 * unconditionally on every render. The `projection` arg is accepted so the
 * signal→descriptor remap can prefer the live graph's known sources, but the
 * hook never branches on its presence.
 *
 * Subject / payload assumptions (verified against the backend, 2026-06):
 *   - Signals publish on `legba.signals.<tenant>.<source_id>.<modality>.<class>`
 *     and the payload is a `Signal` dump. We prefer `payload.source_id`; else we
 *     read the source token out of the subject (skipping the tenant token).
 *   - Findings publish on `analyst.<analyst_id>.finding`; analyst id is the
 *     second subject token (payload may also carry `analyst_id`).
 *   - DLQ rows publish on `legba.dlq.descriptor.<row_id>` with the failing
 *     `descriptor_id` in the payload.
 *   - Governor events publish on `governor.events.<tenant>.<pack_id>.<decision>`
 *     with `pack_id` + `decision` in the payload; only blocks count as errors.
 *   - `/budget/ledger` rows carry `analyst_id` + `tokens_used` (int).
 *   - Source cadence lives at `body.cadence.schedule.raw` (a cron string).
 */
import { useEffect, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import { useBatchedTail } from '@/lib/liveTail'
import type { RegistryEvent } from '@/lib/ws'
import { useFlowState, type NodeTelemetry } from './flowState'
import type { GraphProjection } from './types'

// ---------------------------------------------------------------------------
// Tuning constants
// ---------------------------------------------------------------------------

/** Tail flush window — must match the `intervalMs` we pass to useBatchedTail. */
const TAIL_INTERVAL_MS = 5000
const TAIL_INTERVAL_SEC = TAIL_INTERVAL_MS / 1000

/** Poll period for the REST-derived signals (budget burn, source stall). */
const POLL_INTERVAL_MS = 30_000

/** Fallback stall threshold when a source's cadence can't be derived. */
const DEFAULT_STALL_THRESHOLD_MS = 30 * 60_000

/** A source is "stalled" once it's been silent for this multiple of cadence. */
const STALL_CADENCE_MULTIPLE = 2

// ---------------------------------------------------------------------------
// REST response shapes (subset of the backend models we actually read)
// ---------------------------------------------------------------------------

interface BudgetLedgerRow {
  analyst_id: string
  bucket: string
  tokens_used: number
  // cost_estimate_usd is a stringified Decimal; we surface tokens here.
  cost_estimate_usd?: string
}

interface SourceRow {
  descriptor_id: string
  state?: string
  body?: {
    identity?: { id?: string }
    cadence?: { schedule?: { raw?: string } | null } | null
    [k: string]: unknown
  }
  [k: string]: unknown
}

interface ActorRow {
  actor_id: string
  actor_kind: string
  descriptor_id: string
  last_run_at: string | null
  updated_at?: string
}

// ---------------------------------------------------------------------------
// Subject / payload helpers
// ---------------------------------------------------------------------------

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' ? (v as Record<string, unknown>) : {}
}

function asString(v: unknown): string | undefined {
  return typeof v === 'string' && v.length > 0 ? v : undefined
}

/** source_id for a signal event: payload.source_id, else the subject's source
 *  token. Subject is `legba.signals.<tenant>.<source_id>.<modality>.<class>`,
 *  so after the `legba`/`signals` prefix the tenant is [0] and source is [1];
 *  we tolerate a missing tenant by also accepting [0]. */
function signalSourceId(ev: RegistryEvent): string | undefined {
  const fromPayload = asString(asRecord(ev.payload).source_id)
  if (fromPayload) return fromPayload
  const subject = ev.subject ?? ''
  const prefix = 'legba.signals.'
  if (!subject.startsWith(prefix)) return undefined
  const rest = subject.slice(prefix.length).split('.').filter(Boolean)
  // [tenant, source, modality, class] — prefer the source token, fall back to
  // the first token for older `legba.signals.<source>...` shapes.
  return rest[1] ?? rest[0]
}

/** analyst_id for a finding: payload.analyst_id, else subject token [1] of
 *  `analyst.<analyst_id>.finding`. */
function findingAnalystId(ev: RegistryEvent): string | undefined {
  const fromPayload = asString(asRecord(ev.payload).analyst_id)
  if (fromPayload) return fromPayload
  return asString(ev.subject?.split('.')[1])
}

/** descriptor_id a DLQ event touches: payload.descriptor_id is authoritative. */
function dlqDescriptorId(ev: RegistryEvent): string | undefined {
  return asString(asRecord(ev.payload).descriptor_id)
}

/** pack_id a governor BLOCK touches; non-block decisions are not errors. */
function governorBlockedPackId(ev: RegistryEvent): string | undefined {
  const p = asRecord(ev.payload)
  const decision = asString(p.decision)
  if (decision && decision !== 'block') return undefined
  if (decision === 'block') return asString(p.pack_id)
  // No decision in payload — fall back to the subject's trailing token.
  const tokens = (ev.subject ?? '').split('.')
  if (tokens[tokens.length - 1] === 'block') return asString(p.pack_id) ?? asString(tokens[tokens.length - 2])
  return undefined
}

/** Best-effort cron→milliseconds for the common poll-source shapes:
 *  `*​/N * * * *` (every N minutes) and `0 *​/N * * *` (every N hours). Returns
 *  undefined for anything we can't read; the caller then uses the default
 *  threshold. */
function cronToMs(raw: string | undefined): number | undefined {
  if (!raw) return undefined
  const fields = raw.trim().split(/\s+/)
  if (fields.length < 5) return undefined
  const [minute, hour] = fields
  const stepMin = /^\*\/(\d+)$/.exec(minute)
  if (stepMin && (hour === '*' || hour === '*/1')) {
    const n = Number(stepMin[1])
    return n > 0 ? n * 60_000 : undefined
  }
  if (minute === '0' || minute === '*') {
    const stepHour = /^\*\/(\d+)$/.exec(hour)
    if (stepHour) {
      const n = Number(stepHour[1])
      return n > 0 ? n * 3_600_000 : undefined
    }
    // Fixed minute, fixed hour → at most once an hour.
    if (/^\d+$/.test(hour)) return 24 * 3_600_000
    if (hour === '*') return 3_600_000
  }
  return undefined
}

// ---------------------------------------------------------------------------
// The hook
// ---------------------------------------------------------------------------

export function useFlowTelemetry(projection?: GraphProjection | undefined): void {
  const mergeTelemetry = useFlowState((s) => s.mergeTelemetry)

  // source_id → descriptorId remap. Signals carry the identity `source_id`,
  // which can differ from the descriptor_id the canvas keys nodes by; the
  // sources roster (queried below) gives us the mapping. We also fold in the
  // live projection's source nodes so a freshly-added source resolves before
  // the next /registry/sources poll lands.
  const sourceRemap = useRef<Record<string, string>>({})

  // Cumulative counters survive across batches (the telemetry contract for
  // `fires`/`errors` is a running window count, not a per-batch delta).
  const findingFires = useRef<Record<string, number>>({})
  const descriptorErrors = useRef<Record<string, number>>({})

  // --- live: signal rate per source ----------------------------------------
  useBatchedTail(
    'legba.signals.>',
    (batch) => {
      const counts: Record<string, number> = {}
      for (const ev of batch) {
        const sid = signalSourceId(ev)
        if (!sid) continue
        counts[sid] = (counts[sid] ?? 0) + 1
      }
      const map: Record<string, NodeTelemetry> = {}
      for (const [sid, count] of Object.entries(counts)) {
        const key = sourceRemap.current[sid] ?? sid
        map[key] = { ratePerSec: count / TAIL_INTERVAL_SEC }
      }
      if (Object.keys(map).length > 0) mergeTelemetry(map)
    },
    { intervalMs: TAIL_INTERVAL_MS },
  )

  // --- live: analyst fires (cumulative) ------------------------------------
  useBatchedTail(
    'analyst.*.finding',
    (batch) => {
      const map: Record<string, NodeTelemetry> = {}
      for (const ev of batch) {
        const aid = findingAnalystId(ev)
        if (!aid) continue
        const next = (findingFires.current[aid] ?? 0) + 1
        findingFires.current[aid] = next
        map[aid] = { fires: next }
      }
      if (Object.keys(map).length > 0) mergeTelemetry(map)
    },
    { intervalMs: TAIL_INTERVAL_MS },
  )

  // --- live: DLQ errors per descriptor (cumulative) ------------------------
  useBatchedTail(
    'legba.dlq.>',
    (batch) => {
      const map: Record<string, NodeTelemetry> = {}
      for (const ev of batch) {
        const id = dlqDescriptorId(ev)
        if (!id) continue
        const next = (descriptorErrors.current[id] ?? 0) + 1
        descriptorErrors.current[id] = next
        map[id] = { errors: next }
      }
      if (Object.keys(map).length > 0) mergeTelemetry(map)
    },
    { intervalMs: TAIL_INTERVAL_MS },
  )

  // --- live: governor blocks per pack (cumulative) -------------------------
  useBatchedTail(
    'governor.events.>',
    (batch) => {
      const map: Record<string, NodeTelemetry> = {}
      for (const ev of batch) {
        const id = governorBlockedPackId(ev)
        if (!id) continue
        const next = (descriptorErrors.current[id] ?? 0) + 1
        descriptorErrors.current[id] = next
        map[id] = { errors: next }
      }
      if (Object.keys(map).length > 0) mergeTelemetry(map)
    },
    { intervalMs: TAIL_INTERVAL_MS },
  )

  // --- polled: budget burn per analyst -------------------------------------
  const budget = useQuery<BudgetLedgerRow[]>({
    queryKey: ['budget-ledger'],
    queryFn: () => apiGet<BudgetLedgerRow[]>('/budget/ledger'),
    refetchInterval: POLL_INTERVAL_MS,
  })

  // --- polled: source roster + runtime actors for stall detection ----------
  const sources = useQuery<SourceRow[]>({
    queryKey: ['source-stall', 'sources'],
    queryFn: () => apiGet<SourceRow[]>('/registry/sources'),
    refetchInterval: POLL_INTERVAL_MS,
  })
  const actors = useQuery<ActorRow[]>({
    queryKey: ['source-stall', 'actors'],
    queryFn: () => apiGet<ActorRow[]>('/v3/runtime/actors'),
    refetchInterval: POLL_INTERVAL_MS,
  })

  // Keep the source_id→descriptorId remap fresh from both the live projection
  // and the polled roster. Recomputed (not mutated in place) so the ref always
  // holds a consistent snapshot for the tail callbacks above.
  const projectionSourceIds = useMemo(() => {
    const out: string[] = []
    for (const n of projection?.nodes ?? []) {
      if (n.data?.kind === 'source') out.push(n.data.descriptorId)
    }
    return out
  }, [projection])

  useEffect(() => {
    const remap: Record<string, string> = {}
    // Projection source nodes: identity (descriptorId) maps to itself.
    for (const id of projectionSourceIds) remap[id] = id
    // Roster: body.identity.id (the published source_id) → descriptor_id.
    for (const s of sources.data ?? []) {
      const identityId = asString(s.body?.identity?.id)
      if (identityId) remap[identityId] = s.descriptor_id
      remap[s.descriptor_id] = s.descriptor_id
    }
    sourceRemap.current = remap
  }, [projectionSourceIds, sources.data])

  // Budget → budgetSpent (sum tokens_used across buckets per analyst).
  useEffect(() => {
    const rows = budget.data
    if (!rows || rows.length === 0) return
    const spent: Record<string, number> = {}
    for (const r of rows) {
      if (!r.analyst_id) continue
      spent[r.analyst_id] = (spent[r.analyst_id] ?? 0) + (Number(r.tokens_used) || 0)
    }
    const map: Record<string, NodeTelemetry> = {}
    for (const [aid, tokens] of Object.entries(spent)) map[aid] = { budgetSpent: tokens }
    if (Object.keys(map).length > 0) mergeTelemetry(map)
  }, [budget.data, mergeTelemetry])

  // Stall detection: a source is stalled when its last actor run is older than
  // STALL_CADENCE_MULTIPLE × cadence (or the default threshold when cadence is
  // not derivable). We emit `stalled` for every known source so a recovered
  // source flips back to `false`.
  useEffect(() => {
    const srcRows = sources.data
    const actorRows = actors.data
    if (!srcRows || srcRows.length === 0) return

    // Latest run per descriptor among source-kind actors.
    const lastRun: Record<string, number> = {}
    for (const a of actorRows ?? []) {
      if (a.actor_kind !== 'source') continue
      const ts = a.last_run_at ? Date.parse(a.last_run_at) : NaN
      if (Number.isNaN(ts)) continue
      const prev = lastRun[a.descriptor_id]
      if (prev === undefined || ts > prev) lastRun[a.descriptor_id] = ts
    }

    const now = Date.now()
    const map: Record<string, NodeTelemetry> = {}
    for (const s of srcRows) {
      // Only active sources can meaningfully stall; skip paused/retired/draft.
      if (s.state && s.state !== 'active') continue
      const cadenceMs = cronToMs(s.body?.cadence?.schedule?.raw ?? undefined)
      const threshold =
        cadenceMs !== undefined ? cadenceMs * STALL_CADENCE_MULTIPLE : DEFAULT_STALL_THRESHOLD_MS
      const last = lastRun[s.descriptor_id]
      // No run recorded yet → treat as not-stalled (the source may be warming);
      // a recorded run older than the threshold is a stall.
      const stalled = last !== undefined && now - last > threshold
      map[s.descriptor_id] = { stalled }
    }
    if (Object.keys(map).length > 0) mergeTelemetry(map)
  }, [sources.data, actors.data, mergeTelemetry])
}
