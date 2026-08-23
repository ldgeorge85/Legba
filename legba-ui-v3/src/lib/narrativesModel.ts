/**
 * Narratives model — the pure layer under the narratives surface.
 *
 * `narratives_api.py` attaches an `honesty_note` to every envelope precisely so
 * a client cannot render echo-lead as more than it is: narratives are
 * DETECT-ONLY reifications of contested-claim families, and echo-lead is
 * DESCRIPTIVE co-carriage timing (who published first, who followed inside the
 * window) — never a causal or coordination claim.
 *
 * This module holds that line in the vocabulary it exposes: every label here is
 * about PUBLICATION ORDER, never about influence. `echoStrengthLabel` grades
 * co-carriage consistency, not causation, and the "systematic" flag is
 * reported as the server computed it rather than re-derived with a friendlier
 * threshold.
 */

import type { Narrative, NarrativeEchoEdge } from '@/lib/api'

/** The fallback rendered when a response predates the `honesty_note` field.
 *  Kept verbatim in lockstep with `narratives_api.HONESTY_NOTE`. */
export const HONESTY_NOTE_FALLBACK =
  'Narratives are DETECT-ONLY reifications of contested-claim families and ' +
  'never mutate facts. Echo-lead is DESCRIPTIVE co-carriage timing (who ' +
  'published first, who followed within the window), computed only from ' +
  'publish-dated carriage — NOT a causal or coordination claim.'

export function honestyNote(envelope: { honesty_note?: string } | undefined): string {
  return envelope?.honesty_note || HONESTY_NOTE_FALLBACK
}

/** A contested family reads as "subject · predicate". */
export function narrativeTitle(n: Narrative): string {
  return `${n.subject_key} · ${n.predicate_key}`
}

/** Hours → a compact human span. `null` stays "—": an unmeasured lag is not 0h. */
export function formatLagHours(hours: number | null | undefined): string {
  if (hours == null || !Number.isFinite(hours)) return '—'
  if (hours < 1) return `${Math.round(hours * 60)}m`
  if (hours < 48) return `${hours.toFixed(1)}h`
  return `${(hours / 24).toFixed(1)}d`
}

/**
 * How much of a narrative's carriage is publish-DATED — the denominator the
 * echo timing is actually computed from. A family carried by ten sources of
 * which two carry publish dates has an echo story resting on two, and the
 * surface says so rather than implying ten.
 *
 * Returns null when there are no carriers at all (no ratio to state).
 */
export function datedCoverage(n: Narrative): { dated: number; total: number; ratio: number } | null {
  if (n.carrier_source_count <= 0) return null
  return {
    dated: n.publish_dated_source_count,
    total: n.carrier_source_count,
    ratio: n.publish_dated_source_count / n.carrier_source_count,
  }
}

/**
 * A word for how CONSISTENT a leader→follower co-carriage pattern is. This
 * grades the observation, not an influence claim — "consistent" means the
 * follower published after the leader most times they carried the same claim,
 * and nothing more.
 */
export function echoStrengthLabel(edge: NarrativeEchoEdge): string {
  const r = edge.echo_ratio
  if (r == null || !Number.isFinite(r)) return 'unrated'
  if (r >= 0.8) return 'consistent'
  if (r >= 0.5) return 'frequent'
  return 'occasional'
}

/** Chrome per strength band. `systematic` (the server's own flag) is what gets
 *  the strong treatment — the label above never upgrades an edge on its own. */
export function echoTone(edge: NarrativeEchoEdge): string {
  if (edge.systematic) return 'border-amber-500/40 bg-amber-500/15 text-amber-300'
  return 'border-line bg-surf-2 text-ink-2'
}

/** Contested vs surfaced — the two statuses the route filters on. */
export function narrativeStatusTone(status: string): string {
  switch (status) {
    case 'contested':
      return 'border-rose-500/40 bg-rose-500/15 text-rose-300'
    case 'surfaced':
      return 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300'
    default:
      return 'border-line bg-surf-2 text-ink-2'
  }
}

/**
 * A carrier entry as the mapper writes it, read defensively: the column is
 * free-form jsonb, so every field is optional and a missing one renders as
 * absent rather than as a guess.
 */
export interface CarrierView {
  sourceId: string
  firstSeenAt: string | null
  lagHours: number | null
  signalCount: number | null
}

export function carrierViews(n: Narrative): CarrierView[] {
  return n.carriers.map((c) => ({
    sourceId:
      typeof c.source_id === 'string'
        ? c.source_id
        : typeof c.sourceId === 'string'
          ? c.sourceId
          : '(unknown source)',
    firstSeenAt: typeof c.first_seen_at === 'string' ? c.first_seen_at : null,
    lagHours: typeof c.lag_hours === 'number' ? c.lag_hours : null,
    signalCount: typeof c.signal_count === 'number' ? c.signal_count : null,
  }))
}

/** A value-cluster entry, read with the same defensiveness as `carrierViews`. */
export interface VariantView {
  value: string
  count: number | null
}

export function variantViews(n: Narrative): VariantView[] {
  return n.variants.map((v) => ({
    value:
      typeof v.value === 'string'
        ? v.value
        : typeof v.surfaced_value === 'string'
          ? v.surfaced_value
          : '(unlabeled variant)',
    count:
      typeof v.count === 'number'
        ? v.count
        : typeof v.signal_count === 'number'
          ? v.signal_count
          : null,
  }))
}
