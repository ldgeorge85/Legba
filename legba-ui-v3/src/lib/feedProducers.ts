/**
 * feedProducers — the honest PRODUCER taxonomy behind the Live Feed's
 * analyst/unit drill-down.
 *
 * The feed's `analyst:` facet is a raw substrate id (`energy_security`,
 * `country_composition`, `finding_supersession`, …). An operator picking a
 * producer from a dropdown needs those grouped into the three classes the
 * platform actually has:
 *
 *   * **Units** — the bounded per-country reasoning units (the
 *     `kind: inline_target` descriptors). Always offered in full, even when the
 *     currently-loaded page carries none, because the `analyst:` facet pushes
 *     SERVER-SIDE (`GET /findings?analyst_id=…`) — so picking a unit with no
 *     rows on screen runs a real query rather than showing a false empty.
 *   * **Compositions** — the second-order per-country / per-region reads
 *     (`*_composition`). Same server-side push.
 *   * **Other producers** — everything else that has actually produced a row in
 *     view (deterministic sweeps, meta analysts, mining handlers…). Derived
 *     from the loaded rows ONLY: we never list a producer we have no evidence
 *     exists, and we never invent a display name for one.
 *
 * Labels come from the shared `humanizeAnalystId` formatter, so a producer
 * reads the same here as it does on a feed card's meta line.
 *
 * Pure + DOM-free (unit-tested without a DOM), like the rest of the feed model.
 */

import { humanizeAnalystId } from './analystNames'

/**
 * The bounded reasoning UNITS, in headline order.
 *
 * MIRRORS `UNITS` in `v4/why/CountryUnitsAssessment.tsx` (the canonical
 * roster the country desk renders) — kept as a local id-only copy so this pure
 * model module never imports a React panel, with the lockstep TEST-ENFORCED in
 * `feedProducers.test.ts` (the same mirror-plus-lockstep-test idiom the
 * registry uses for `_FAITH_FLOOR`). Adding a unit descriptor means adding it
 * in both places; the test fails loudly until you do.
 */
export const FEED_UNIT_IDS: readonly string[] = [
  'leadership_transition',
  'energy_security',
  'escalation',
  'narrative_coordination',
  'internal_stability',
  'military_posture',
  'economic_coercion',
  'proliferation_watch',
]

/**
 * The composition analysts that ship as descriptors
 * (`descriptors/analyst_*_composition.yaml`). Offered unconditionally for the
 * same server-side-push reason the units are.
 */
export const FEED_COMPOSITION_IDS: readonly string[] = [
  'country_composition',
  'region_composition',
  'escalation_composition',
]

/** A producer's class — the three groups the dropdown renders. */
export type ProducerClass = 'unit' | 'composition' | 'other'

/** Matches a composition analyst id (`country_composition`), but NOT a sweep
 *  that merely mentions compositions (`composition_lineage_sweep`). */
const COMPOSITION_RE = /_composition$/

/**
 * Classify a raw `analyst_id`. An empty/absent id is `'other'` — never
 * silently promoted into a unit.
 */
export function classifyProducer(analystId: string | null | undefined): ProducerClass {
  const id = (analystId ?? '').trim().toLowerCase()
  if (!id) return 'other'
  if (FEED_UNIT_IDS.includes(id)) return 'unit'
  if (FEED_COMPOSITION_IDS.includes(id) || COMPOSITION_RE.test(id)) return 'composition'
  return 'other'
}

/** Display label for a producer group (the dropdown's `<optgroup>` labels). */
export const PRODUCER_GROUP_LABEL: Record<ProducerClass, string> = {
  unit: 'Units',
  composition: 'Compositions',
  other: 'Other producers',
}

export interface ProducerOption {
  /** The raw `analyst_id` — the exact value the `analyst:` chip carries. */
  id: string
  /** Reader-facing label (`energy_security` → `Energy security`). */
  label: string
  group: ProducerClass
  /** True when this producer has at least one row in the loaded page. */
  present: boolean
}

/**
 * Build the grouped producer options: the canonical unit + composition rosters
 * (always), plus every OTHER producer actually present in `presentIds`, sorted
 * by label within its group. Units and compositions keep their canonical order.
 *
 * `presentIds` is the raw `analyst_id` of every row currently loaded (nulls and
 * blanks are ignored — a signal carries no analyst).
 */
export function buildProducerOptions(
  presentIds: Iterable<string | null | undefined>,
): ProducerOption[] {
  const present = new Set<string>()
  for (const raw of presentIds) {
    const id = (raw ?? '').trim().toLowerCase()
    if (id) present.add(id)
  }

  const opts: ProducerOption[] = []
  const emitted = new Set<string>()
  const push = (id: string, group: ProducerClass) => {
    if (emitted.has(id)) return
    emitted.add(id)
    opts.push({ id, label: humanizeAnalystId(id, id), group, present: present.has(id) })
  }

  for (const id of FEED_UNIT_IDS) push(id, 'unit')
  for (const id of FEED_COMPOSITION_IDS) push(id, 'composition')

  // Everything else we have EVIDENCE for, classified and label-sorted.
  const others: ProducerOption[] = []
  for (const id of present) {
    if (emitted.has(id)) continue
    emitted.add(id)
    others.push({ id, label: humanizeAnalystId(id, id), group: classifyProducer(id), present: true })
  }
  others.sort((a, b) => a.label.localeCompare(b.label))
  opts.push(...others)

  return opts
}

/**
 * The set of producer ids the feed can push SERVER-SIDE as an exact
 * `analyst_id=` filter: the canonical rosters plus anything seen in the loaded
 * rows. A hand-typed partial (`analyst:escal`) is deliberately NOT in this set,
 * so it stays a client-side substring match instead of becoming an exact-match
 * server query that returns nothing.
 */
export function exactProducerIds(
  presentIds: Iterable<string | null | undefined>,
): Set<string> {
  const out = new Set<string>(FEED_UNIT_IDS)
  for (const id of FEED_COMPOSITION_IDS) out.add(id)
  for (const raw of presentIds) {
    const id = (raw ?? '').trim().toLowerCase()
    if (id) out.add(id)
  }
  return out
}
