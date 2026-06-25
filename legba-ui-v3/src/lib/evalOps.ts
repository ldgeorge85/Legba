/**
 * UI-5 (Tiers E+F) — pure data-layer for the eval + ops panels.
 *
 * All grouping / trend / diff / lag-classification logic lives here so it is
 * unit-tested without a DOM (mirrors `@/lib/findingsViews`). The panels
 * (EvalScorecard / StreamLag / GovernorEvents / OptimizerDiff / AuditChain /
 * ActorHealth) stay thin render shells over these helpers.
 */

// ===========================================================================
// Eval / critic scorecard (system.eval)
// ===========================================================================

/**
 * One critic-scored output for an analyst. Mirrors the substrate-reads shape
 * `GET /api/v1/v3/eval/scorecard?analyst_id=&since=&limit=` returns: a row per
 * critic judgement with the per-axis rubric breakdown + an overall.
 */
export interface ScorecardRow {
  id: string
  analyst_id: string
  analyst_version: string | null
  /** per-rubric-axis scores in [0,1] (e.g. {calibration: 0.8, evidence: 0.6}) */
  scores: Record<string, number>
  overall_score: number
  /** optional ground-truth backtest accuracy in [0,1] when present */
  ground_truth_accuracy?: number | null
  produced_at: string
}

export interface AnalystScorecard {
  analyst_id: string
  /** rows newest-first */
  rows: ScorecardRow[]
  latest_overall: number
  /** mean overall across the window */
  mean_overall: number
  /** latest - earliest overall in the window (trend direction) */
  trend_delta: number
  /** mean per-axis scores across the window */
  axis_means: Record<string, number>
  /** latest ground-truth backtest accuracy if any row carries it */
  latest_accuracy: number | null
}

/** Group raw scorecard rows into per-analyst summaries with trend stats. */
export function buildScorecards(rows: ScorecardRow[]): AnalystScorecard[] {
  const byAnalyst = new Map<string, ScorecardRow[]>()
  for (const r of rows) {
    const list = byAnalyst.get(r.analyst_id) ?? []
    list.push(r)
    byAnalyst.set(r.analyst_id, list)
  }
  const out: AnalystScorecard[] = []
  for (const [analyst_id, list] of byAnalyst) {
    // newest-first for display; oldest needed for trend.
    const sorted = [...list].sort(
      (a, b) => Date.parse(b.produced_at) - Date.parse(a.produced_at),
    )
    const newest = sorted[0]
    const oldest = sorted[sorted.length - 1]
    const mean_overall =
      sorted.reduce((a, r) => a + r.overall_score, 0) / (sorted.length || 1)

    // per-axis means over every row that carries the axis.
    const axisSum: Record<string, number> = {}
    const axisN: Record<string, number> = {}
    for (const r of sorted) {
      for (const [axis, v] of Object.entries(r.scores)) {
        axisSum[axis] = (axisSum[axis] ?? 0) + v
        axisN[axis] = (axisN[axis] ?? 0) + 1
      }
    }
    const axis_means: Record<string, number> = {}
    for (const axis of Object.keys(axisSum)) {
      axis_means[axis] = axisSum[axis] / axisN[axis]
    }

    const latest_accuracy =
      sorted.find((r) => typeof r.ground_truth_accuracy === 'number')
        ?.ground_truth_accuracy ?? null

    out.push({
      analyst_id,
      rows: sorted,
      latest_overall: newest.overall_score,
      mean_overall,
      trend_delta: newest.overall_score - oldest.overall_score,
      axis_means,
      latest_accuracy,
    })
  }
  // worst-performing analysts first — they need attention.
  return out.sort((a, b) => a.latest_overall - b.latest_overall)
}

/** Chronological (oldest→newest) overall-score series for a trend chart. */
export function critScoreTrend(
  rows: ScorecardRow[],
): Array<{ t: number; label: string; overall: number }> {
  return [...rows]
    .sort((a, b) => Date.parse(a.produced_at) - Date.parse(b.produced_at))
    .map((r) => ({
      t: Date.parse(r.produced_at),
      label: new Date(r.produced_at).toLocaleDateString(),
      overall: r.overall_score,
    }))
}

export type ScoreBand = 'good' | 'warn' | 'bad'
export function scoreBand(score: number): ScoreBand {
  if (score >= 0.8) return 'good'
  if (score >= 0.5) return 'warn'
  return 'bad'
}

// ===========================================================================
// NATS consumer-lag monitor (system.stream_lag)
// ===========================================================================

/**
 * One consumer's lag snapshot. Mirrors
 * `NatsStore.consumer_lag()` / `SubscriptionEngine.consumer_lag()` projected
 * over `GET /api/v1/v3/streams/consumer_lag` — one row per per-source /
 * per-target durable consumer.
 */
export interface ConsumerLagRow {
  stream: string
  durable: string
  /** the dimension this consumer fans out — source_id or target_id */
  scope_kind: 'source' | 'target' | string
  scope_id: string
  /** headline lag — messages on the stream not yet delivered */
  num_pending: number
  /** delivered but unacked */
  num_ack_pending: number
  num_redelivered: number
  num_waiting: number
  delivered_stream_seq: number | null
  ack_floor_stream_seq: number | null
}

export type LagSeverity = 'ok' | 'warn' | 'critical'

/**
 * Classify a consumer's health. Lag is the headline; redeliveries (poison
 * messages) and a large unacked backlog escalate independently.
 */
export function lagSeverity(
  row: Pick<ConsumerLagRow, 'num_pending' | 'num_redelivered' | 'num_ack_pending'>,
  thresholds: { warn: number; critical: number } = { warn: 100, critical: 1000 },
): LagSeverity {
  if (row.num_pending >= thresholds.critical || row.num_redelivered >= 25) {
    return 'critical'
  }
  if (
    row.num_pending >= thresholds.warn ||
    row.num_redelivered > 0 ||
    row.num_ack_pending >= thresholds.warn
  ) {
    return 'warn'
  }
  return 'ok'
}

/** Worst-lag-first, then by redelivered, for the monitor table. */
export function sortLag(rows: ConsumerLagRow[]): ConsumerLagRow[] {
  return [...rows].sort(
    (a, b) => b.num_pending - a.num_pending || b.num_redelivered - a.num_redelivered,
  )
}

// ===========================================================================
// Optimizer prompt-module diff (system.optimizer / optimizer.diff)
// ===========================================================================

/**
 * The candidate-vs-current prompt-module diff. Mirrors
 * `GET /api/v1/v3/optimizer/candidates/{id}/diff` — the candidate module text
 * and the currently-active module text, plus identity for the header.
 */
export interface PromptModuleDiff {
  candidate_id: string
  analyst_id: string
  /** path of the live/active module the candidate would replace */
  current_module_path: string
  candidate_module_path: string
  current_text: string
  candidate_text: string
  eval_score: number
  eval_score_delta: number
}

export type DiffOp = 'same' | 'add' | 'del'
export interface DiffLine {
  op: DiffOp
  /** line number in the current (old) text, null for added lines */
  oldNo: number | null
  /** line number in the candidate (new) text, null for deleted lines */
  newNo: number | null
  text: string
}

/**
 * Minimal line-level diff via LCS. Adequate for prompt-module review (these
 * are short, line-oriented texts); avoids a runtime diff dependency.
 */
export function diffLines(oldText: string, newText: string): DiffLine[] {
  const a = oldText.split('\n')
  const b = newText.split('\n')
  const n = a.length
  const m = b.length

  // LCS length table.
  const lcs: number[][] = Array.from({ length: n + 1 }, () =>
    new Array<number>(m + 1).fill(0),
  )
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] =
        a[i] === b[j]
          ? lcs[i + 1][j + 1] + 1
          : Math.max(lcs[i + 1][j], lcs[i][j + 1])
    }
  }

  const out: DiffLine[] = []
  let i = 0
  let j = 0
  let oldNo = 1
  let newNo = 1
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ op: 'same', oldNo: oldNo++, newNo: newNo++, text: a[i] })
      i++
      j++
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      out.push({ op: 'del', oldNo: oldNo++, newNo: null, text: a[i] })
      i++
    } else {
      out.push({ op: 'add', oldNo: null, newNo: newNo++, text: b[j] })
      j++
    }
  }
  while (i < n) out.push({ op: 'del', oldNo: oldNo++, newNo: null, text: a[i++] })
  while (j < m) out.push({ op: 'add', oldNo: null, newNo: newNo++, text: b[j++] })
  return out
}

export interface DiffStat {
  added: number
  deleted: number
  unchanged: number
}
export function diffStat(lines: DiffLine[]): DiffStat {
  let added = 0
  let deleted = 0
  let unchanged = 0
  for (const l of lines) {
    if (l.op === 'add') added++
    else if (l.op === 'del') deleted++
    else unchanged++
  }
  return { added, deleted, unchanged }
}

// ===========================================================================
// Governor events (system.governor)
// ===========================================================================

/**
 * One governor decision. Mirrors `GovernorEvent.to_payload()` (the DB row +
 * the NATS telemetry payload). Served by
 * `GET /api/v1/registry/governor_events?pack_id=&decision=&limit=` and tailed
 * live on the `governor.events.>` subject via the registry-events WS.
 */
export interface GovernorEventRow {
  pack_id: string
  decision: 'allow' | 'block'
  cause: string
  tool_name: string | null
  budget_account: string
  requested_by: string
  tenant_id: string
  cap_dimension: string | null
  cap_limit: number | null
  observed_value: number | null
  detail: string
  occurred_at: string
  /** synthesised client-side for keying / live-badging; not from the wire */
  _key?: string
  _live?: boolean
}

const GOVERNOR_TAIL_FILTER = 'governor.events.>'
export { GOVERNOR_TAIL_FILTER }

/**
 * Map a `governor.events.>` WS envelope payload into a row. Tolerates partial
 * payloads (telemetry can lag the schema). Returns null when unusable.
 */
export function mapGovernorEnvelope(
  payload: Record<string, unknown> | undefined,
): GovernorEventRow | null {
  if (!payload) return null
  const decision = payload.decision
  if (decision !== 'allow' && decision !== 'block') return null
  const occurred_at =
    typeof payload.occurred_at === 'string'
      ? payload.occurred_at
      : new Date().toISOString()
  const pack_id = typeof payload.pack_id === 'string' ? payload.pack_id : '(unknown)'
  const num = (k: string): number | null =>
    typeof payload[k] === 'number' ? (payload[k] as number) : null
  const str = (k: string, dflt = ''): string =>
    typeof payload[k] === 'string' ? (payload[k] as string) : dflt
  return {
    pack_id,
    decision,
    cause: str('cause', 'ok'),
    tool_name: typeof payload.tool_name === 'string' ? payload.tool_name : null,
    budget_account: str('budget_account', 'system'),
    requested_by: str('requested_by', 'system'),
    tenant_id: str('tenant_id', 'default'),
    cap_dimension:
      typeof payload.cap_dimension === 'string' ? payload.cap_dimension : null,
    cap_limit: num('cap_limit'),
    observed_value: num('observed_value'),
    detail: str('detail'),
    occurred_at,
    _key: `${pack_id}:${decision}:${occurred_at}:${str('tool_name')}`,
    _live: true,
  }
}

export interface GovernorSummary {
  total: number
  blocked: number
  allowed: number
  /** distinct packs that have at least one BLOCK */
  blocked_packs: string[]
  /** count of block events by cause */
  by_cause: Record<string, number>
}
export function summariseGovernor(rows: GovernorEventRow[]): GovernorSummary {
  let blocked = 0
  let allowed = 0
  const blockedPacks = new Set<string>()
  const byCause: Record<string, number> = {}
  for (const r of rows) {
    if (r.decision === 'block') {
      blocked++
      blockedPacks.add(r.pack_id)
      byCause[r.cause] = (byCause[r.cause] ?? 0) + 1
    } else {
      allowed++
    }
  }
  return {
    total: rows.length,
    blocked,
    allowed,
    blocked_packs: [...blockedPacks],
    by_cause: byCause,
  }
}

// ===========================================================================
// Audit-chain browser (system.audit)
// ===========================================================================

/**
 * One audit-chain entry. Mirrors `AuditEntryOut` from
 * `src/legba/data/registry/api.py`: each register/update/promote is signed
 * (Ed25519) and re-verified inline by the backend, exposing
 * `signature_verified` (true/false, or null in reader-only mode).
 */
export interface AuditEntryRow {
  id: string
  occurred_at: string
  actor_id: string
  actor_role: string
  namespace: string
  descriptor_id: string
  action: string
  from_version: string | null
  to_version: string | null
  change_summary: Record<string, unknown>
  signer_did: string
  signature_verified: boolean | null
}

export type VerifyStatus = 'verified' | 'failed' | 'unverifiable'
export function verifyStatus(entry: Pick<AuditEntryRow, 'signature_verified'>): VerifyStatus {
  if (entry.signature_verified === true) return 'verified'
  if (entry.signature_verified === false) return 'failed'
  return 'unverifiable'
}

export interface ChainHealth {
  total: number
  verified: number
  failed: number
  unverifiable: number
  /** true when every entry that *could* be checked passed */
  intact: boolean
}
export function chainHealth(entries: AuditEntryRow[]): ChainHealth {
  let verified = 0
  let failed = 0
  let unverifiable = 0
  for (const e of entries) {
    const s = verifyStatus(e)
    if (s === 'verified') verified++
    else if (s === 'failed') failed++
    else unverifiable++
  }
  return {
    total: entries.length,
    verified,
    failed,
    unverifiable,
    intact: failed === 0,
  }
}

// ===========================================================================
// Runtime actor health (system.runtime) — kind classification
// ===========================================================================

export const ACTOR_KINDS = [
  'all',
  'target',
  'analyst',
  'discovery',
  'source',
  'consult',
] as const
export type ActorKindFilter = (typeof ACTOR_KINDS)[number]

/** Roll up actor rows by kind + lifecycle for the health summary header. */
export function actorRollup(
  rows: Array<{ actor_kind: string; lifecycle: string; error_count: number }>,
): {
  byKind: Record<string, number>
  byLifecycle: Record<string, number>
  errors: number
  stale: number
} {
  const byKind: Record<string, number> = {}
  const byLifecycle: Record<string, number> = {}
  let errors = 0
  for (const r of rows) {
    byKind[r.actor_kind] = (byKind[r.actor_kind] ?? 0) + 1
    byLifecycle[r.lifecycle] = (byLifecycle[r.lifecycle] ?? 0) + 1
    errors += r.error_count
  }
  return { byKind, byLifecycle, errors, stale: byLifecycle['error'] ?? 0 }
}

/** Relative-time formatter shared by the ops panels. */
export function relTime(ts: string | null): string {
  if (!ts) return 'never'
  const d = new Date(ts).getTime()
  if (!Number.isFinite(d)) return 'never'
  const sec = Math.max(0, (Date.now() - d) / 1000)
  if (sec < 60) return `${sec.toFixed(0)}s ago`
  if (sec < 3600) return `${(sec / 60).toFixed(0)}m ago`
  if (sec < 86400) return `${(sec / 3600).toFixed(1)}h ago`
  return `${(sec / 86400).toFixed(1)}d ago`
}
