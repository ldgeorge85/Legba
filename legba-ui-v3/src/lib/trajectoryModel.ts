/**
 * Situation-trajectory model — the pure layer under the trajectory surface.
 *
 * The route (`situation_trajectory_api.py`) is deliberately un-synthesizing:
 * no trend field, no direction summary, no prose. Its honesty contract is
 * carried by the WIRE SHAPE, and this module's whole job is to keep those
 * distinctions from collapsing into one grey "no data" in the UI:
 *
 *   measured=false            → "we could not look"      (the read itself failed)
 *   measured=true, events=[]  → "we have never assessed"  (a real, honest zero)
 *   state=null                → no state has ever been recorded — NOT `watching`
 *
 * The panel therefore never renders a default state, and never renders an empty
 * ledger and a failed read the same way.
 */

import type { SituationTrajectory, TrajectoryEvent } from '@/lib/api'

/** Which of the three honest zero-states a payload is in. */
export type TrajectoryStatus = 'unmeasured' | 'empty' | 'ok'

export function trajectoryStatus(t: SituationTrajectory | undefined): TrajectoryStatus {
  if (!t || !t.measured) return 'unmeasured'
  return t.events.length === 0 ? 'empty' : 'ok'
}

/** The operator-facing sentence for each zero-state. Deliberately different
 *  wording per state — conflating them is the failure this guards. */
export function trajectoryStatusText(status: TrajectoryStatus): string {
  switch (status) {
    case 'unmeasured':
      return 'The trajectory read failed — this is “we could not look”, not “nothing happened”.'
    case 'empty':
      return 'No trajectory recorded — this frame is known, but its movement has never been assessed.'
    default:
      return ''
  }
}

/** The four ledger deltas, plus a tolerant fallback for anything the tracker
 *  starts writing later. Ordered so a legend reads escalation → de-escalation. */
export const DELTA_LABELS: Record<string, string> = {
  escalates: 'escalates',
  de_escalates: 'de-escalates',
  broadens: 'broadens',
  unchanged_checkpoint: 'unchanged',
}

export function deltaLabel(delta: string): string {
  return DELTA_LABELS[delta] ?? delta
}

/** Tailwind classes per delta. Unknown deltas get neutral chrome rather than
 *  being dropped — a new delta kind must still render. */
export function deltaTone(delta: string): string {
  switch (delta) {
    case 'escalates':
      return 'border-rose-500/40 bg-rose-500/15 text-rose-300'
    case 'de_escalates':
      return 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300'
    case 'broadens':
      return 'border-amber-500/40 bg-amber-500/15 text-amber-300'
    case 'unchanged_checkpoint':
      return 'border-line bg-surf-2 text-ink-3'
    default:
      return 'border-line bg-surf-2 text-ink-2'
  }
}

/**
 * The state to display for a frame. `null` stays `null` — a frame whose ledger
 * has never spoken has NO state, and inventing `watching` here would turn
 * "never assessed" into "assessed and quiet".
 */
export function currentState(t: SituationTrajectory | undefined): string | null {
  return t?.state ?? null
}

/**
 * The EVIDENCE date of a ledger row, falling back to the row's write time only
 * when the evidence time is absent — and saying which it used, because the two
 * mean different things (when the world moved vs when we noticed).
 */
export function eventWhen(e: TrajectoryEvent): { iso: string | null; basis: 'evidence' | 'recorded' | 'none' } {
  if (e.occurred_at) return { iso: e.occurred_at, basis: 'evidence' }
  if (e.created_at) return { iso: e.created_at, basis: 'recorded' }
  return { iso: null, basis: 'none' }
}

/**
 * Count of ledger rows by delta — the only aggregate this surface computes, and
 * it is a count of rows as written, not a trend.
 *
 * Tolerant of frame-count changes by construction: it derives its keys from the
 * data rather than from a fixed delta list, so a FRAME-2 repair to the
 * register's aggregation changes the numbers here and nothing else.
 */
export function deltaCounts(events: readonly TrajectoryEvent[]): Array<{ delta: string; n: number }> {
  const counts = new Map<string, number>()
  for (const e of events) counts.set(e.delta, (counts.get(e.delta) ?? 0) + 1)
  return [...counts.entries()]
    .map(([delta, n]) => ({ delta, n }))
    .sort((a, b) => b.n - a.n || a.delta.localeCompare(b.delta))
}
