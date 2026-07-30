/**
 * provenance — the reusable in-panel lineage/provenance grammar (P4-5).
 *
 * Two pure, DOM-free pieces (adapted from worldmonitor's `LayerExplanation`
 * grammar), tested without a DOM:
 *
 *   1. The `live | fallback | absent` enum on DISPLAYED NUMBERS. Wherever a
 *      panel shows a computed number that COULD come from live data, a fallback
 *      table, or nothing, `resolveNumberProvenance` stamps which — so a viewer
 *      knows a number is real-live vs a degraded fallback vs honestly absent.
 *      HONESTY: `fallback` is only ever returned when the caller passes an
 *      EXPLICIT backend fallback signal. When the backend carries no such
 *      signal (the common case today), a present value reads `live` and an
 *      empty one reads `absent` — we NEVER fabricate a fallback state. The
 *      `fallback` input is the seam a backend fallback-flag follow-up fills.
 *
 *   2. The ProvenanceCard grammar: `describeProvenance` maps what the substrate
 *      ALREADY carries (lineage `derived_from`, verify state, source, produced/
 *      fetched_at, confidence) onto a purpose / source / freshness / confidence
 *      / limitations card — no walker click, no fabricated fields.
 */

// ---------------------------------------------------------------------------
// The live | fallback | absent enum on displayed numbers
// ---------------------------------------------------------------------------

export type ProvenanceState = 'live' | 'fallback' | 'absent'

export interface NumberProvenanceInput {
  /** The value to classify. `null`/`undefined`/`NaN` → `absent`. */
  value: number | null | undefined
  /**
   * EXPLICIT backend fallback signal. `true` = the route told us this number
   * came from a degraded fallback (a canned table / last-known value) rather
   * than live data. `undefined`/`false` = no fallback (or the route carries no
   * such signal yet — the follow-up seam). NEVER synthesize `true` here.
   */
  fallback?: boolean
  /**
   * Force `absent` even when a value is technically present — e.g. a
   * sentinel/`insufficient` marker that should read as "no honest number yet"
   * rather than a live figure. Optional; default false.
   */
  treatAsAbsent?: boolean
}

/**
 * Resolve the `live | fallback | absent` state for a displayed number.
 *
 *   * `treatAsAbsent` → `absent` (a sentinel that isn't a real number).
 *   * value missing / non-finite → `absent`.
 *   * explicit `fallback === true` → `fallback`.
 *   * otherwise → `live`.
 */
export function resolveNumberProvenance(input: NumberProvenanceInput): ProvenanceState {
  if (input.treatAsAbsent) return 'absent'
  const v = input.value
  const present = typeof v === 'number' && Number.isFinite(v)
  if (!present) return 'absent'
  if (input.fallback === true) return 'fallback'
  return 'live'
}

/** Convenience: classify a bare value (present→live, empty→absent), with an
 *  optional explicit fallback flag. */
export function provenanceOf(
  value: number | null | undefined,
  fallback?: boolean,
): ProvenanceState {
  return resolveNumberProvenance({ value, fallback })
}

/** Presentation metadata for each state — label + tooltip + tone. Tone maps to
 *  the panel token palette (ok/warn/muted), not a raw color, so the badge flips
 *  with the light/dark theme like the rest of the chrome. */
export const PROVENANCE_META: Record<
  ProvenanceState,
  { label: string; title: string; tone: 'ok' | 'warn' | 'muted' }
> = {
  live: {
    label: 'live',
    title: 'Live — computed from data present in the substrate right now.',
    tone: 'ok',
  },
  fallback: {
    label: 'fallback',
    title:
      'Fallback — served from a degraded fallback source, not live data. Treat as indicative, not current.',
    tone: 'warn',
  },
  absent: {
    label: 'absent',
    title: 'Absent — no data for this number (an honest empty, not a zero).',
    tone: 'muted',
  },
}

// ---------------------------------------------------------------------------
// The ProvenanceCard grammar
// ---------------------------------------------------------------------------

/** The shaped, render-ready provenance for a datum/panel. `freshnessAt` is a
 *  raw ISO the card relative-times; `limitations` is always an array (possibly
 *  empty) so the card never branches on undefined. */
export interface ProvenanceFacts {
  purpose?: string
  source?: string
  /** ISO timestamp the card renders as a relative "N ago". */
  freshnessAt?: string
  /** Pre-formatted freshness, used when there is no single ISO to relative-time. */
  freshnessLabel?: string
  confidence?: string
  limitations: string[]
  /** Overall live/fallback/absent posture for the datum this card explains. */
  state: ProvenanceState
}

/** The substrate-ish fields `describeProvenance` reads. Every field optional —
 *  panels pass whatever their row carries; absence degrades honestly. */
export interface ProvenanceSource {
  /** What the datum/panel is FOR (a one-liner the panel supplies). */
  purpose?: string
  /** Where it came from — a route label, analyst id, or source id. */
  source?: string | null
  analyst_id?: string | null
  /** Freshness candidates, most-specific first. */
  produced_at?: string | null
  fetched_at?: string | null
  created_at?: string | null
  /** Upstream lineage ids (`derived_from[]`). */
  derived_from?: string[] | null
  /** The faithfulness-verify detail block, when present. */
  verification?: { faithfulness_score?: number | null; judge_status?: string | null } | null
  /** `"structural"` for verify-exempt deterministic analysts, or
   *  `"structural-verified"` (C2b) when that structural finding's asserted
   *  quantities additionally passed the deterministic structural_claims
   *  verify profile. */
  verify_exempt?: string | null
  /** Confidence figures. */
  confidence?: number | null
  effective_confidence?: number | null
  /** Extra honest caveats the panel wants to append (e.g. "preview route"). */
  extraLimitations?: string[]
  /** Explicit backend fallback signal (see `NumberProvenanceInput.fallback`). */
  fallback?: boolean
  /** Force the datum's overall state to absent (e.g. an empty panel). */
  absent?: boolean
}

function pct(v: number): string {
  return `${Math.round(v * 100)}%`
}

/**
 * Map substrate fields onto the ProvenanceCard grammar. Pure + total: every
 * field degrades to omission (or an honest limitation) rather than a fabricated
 * value.
 *
 *   * source     — explicit `source`, else `analyst_id`.
 *   * freshness  — the first present of produced_at / fetched_at / created_at.
 *   * confidence — verify-exempt structural → the honest unverified stamp; else
 *                  a faithfulness reading when a verify block exists; else the
 *                  effective/raw confidence; else omitted.
 *   * limitations — no-lineage, verify-exempt, and any panel-supplied caveats.
 *   * state      — absent when forced or when nothing dates it; fallback only
 *                  on an explicit signal; otherwise live.
 */
export function describeProvenance(src: ProvenanceSource): ProvenanceFacts {
  const freshnessAt = src.produced_at ?? src.fetched_at ?? src.created_at ?? undefined

  let confidence: string | undefined
  if (src.verify_exempt === 'structural-verified') {
    // C2b — a structural finding whose asserted quantities passed the
    // deterministic structural_claims verify profile: distinct from (and
    // better than) the bare "unverified — structural" default below.
    confidence = 'structural — recomputation-verified'
  } else if (src.verify_exempt === 'structural') {
    confidence = 'unverified — structural'
  } else if (
    src.verification &&
    typeof src.verification.faithfulness_score === 'number' &&
    Number.isFinite(src.verification.faithfulness_score)
  ) {
    const faith = pct(src.verification.faithfulness_score)
    const status = src.verification.judge_status
    confidence = status ? `faithfulness ${faith} · ${status}` : `faithfulness ${faith}`
  } else {
    const c =
      typeof src.effective_confidence === 'number'
        ? src.effective_confidence
        : typeof src.confidence === 'number'
          ? src.confidence
          : null
    if (c !== null && Number.isFinite(c)) confidence = `confidence ${pct(c)}`
  }

  const limitations: string[] = []
  if (!src.derived_from || src.derived_from.length === 0) {
    limitations.push('no upstream lineage recorded')
  }
  if (src.verify_exempt === 'structural-verified') {
    limitations.push(
      'deterministic structural claims re-derived and matched — not routed through the ' +
        'faithfulness verify pass (structural analyst)',
    )
  } else if (src.verify_exempt === 'structural') {
    limitations.push('not routed through the faithfulness verify pass (structural analyst)')
  }
  for (const extra of src.extraLimitations ?? []) limitations.push(extra)

  const state: ProvenanceState = src.absent
    ? 'absent'
    : src.fallback === true
      ? 'fallback'
      : freshnessAt
        ? 'live'
        : 'absent'

  return {
    purpose: src.purpose,
    source: src.source ?? src.analyst_id ?? undefined,
    freshnessAt,
    confidence,
    limitations,
    state,
  }
}
