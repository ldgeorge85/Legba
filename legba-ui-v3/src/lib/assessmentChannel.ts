/**
 * assessmentChannel — WHICH interpretive voice belongs to the record on screen.
 *
 * The Assessment shipped as one constant: `world_assessment`, hard-coded in the
 * reader beside the world spine. That was true while exactly one voice existed.
 * With a per-COUNTRY channel it stops being true in the worst way — a constant
 * cannot say "this tier has no voice", so a country or thematic read fell
 * through to the newest WORLD assessment and rendered it under a spine-mismatch
 * note. The reader was showing the operator a voice written about a different
 * record and explaining, in small type, that it had done so.
 *
 * So the voice is a FUNCTION of the record: its producer and the tier its own
 * payload declares. Three answers, and "none" is one of them.
 *
 *   world spine      (`world_assessor`,      tier `world`)   → `world_assessment`
 *   country assembly (`country_composition`, tier `country`) → `country_assessment`
 *   region, thematic, anything else                          → NO channel
 *
 * ── WHY BOTH THE PRODUCER AND THE TIER ────────────────────────────────────────
 * The producer id is what the read path filters on; the tier is what the payload
 * says the record IS. They should never disagree, and when they do the honest
 * answer is no band at all: a voice is fenced to the record it was written from
 * (`derived_from == [spine_id]`), so a channel chosen off a producer whose
 * payload claims another tier is a channel that cannot be about this record.
 *
 * ── WHY THE PICK IS NOT "THE NEWEST ROW" ──────────────────────────────────────
 * {@link pickAssessmentRow} prefers the row whose `derived_from[0]` IS the
 * record on screen, and only then falls back to the newest — which the band
 * renders with `spineMismatch`, so "written from another run" is stated rather
 * than implied. For a target-scoped channel the fallback is confined to the SAME
 * desk: a country's band may show an older assessment of that country, never a
 * current assessment of its neighbour.
 *
 * Pure and DOM-free: rows and payloads in, a descriptor out. No fetch lives here.
 */
import type { AssemblyPayload, AssemblyTier, ReadFindingRow } from './assemblyModel'

/** The world spine's producer (`standing_auditor.WORLD_ANALYST_ID`). */
export const WORLD_SPINE_ANALYST = 'world_assessor'
/** The per-country assembly's producer (`composition_slice`). */
export const COUNTRY_SPINE_ANALYST = 'country_composition'

/** One interpretive channel, described by the record it is fenced to. */
export interface AssessmentChannel {
  /** The producer whose `kind='finding'` rows carry `data.data.assessment`. */
  analystId: string
  /** The assembly tier this channel is written from. */
  tier: AssemblyTier
  /**
   * True when the channel's rows are per-desk and carry a `target_id` — so the
   * read path can be narrowed with `target_id=` and the client-side fallback
   * must never cross desks.
   */
  targetScoped: boolean
  /** What the band calls this voice in its label line. */
  label: string
  /** What the band says when the channel has not published for this record. */
  absentReason: string
}

/**
 * The channel each spine producer owns. Keyed by the SPINE's analyst id, because
 * that is what the reader already knows before any channel row is fetched.
 */
export const ASSESSMENT_CHANNELS: Readonly<Record<string, AssessmentChannel>> = {
  [WORLD_SPINE_ANALYST]: {
    analystId: 'world_assessment',
    tier: 'world',
    targetScoped: false,
    label: 'Assessment',
    absentReason:
      'The interpretive channel has not published for this record. The record above stands without it — it never depended on the channel.',
  },
  [COUNTRY_SPINE_ANALYST]: {
    analystId: 'country_assessment',
    tier: 'country',
    targetScoped: true,
    label: 'Country assessment',
    absentReason:
      'No country assessment has been written from this record yet. The channel is new and starts in draft, so an absence here is its normal state and not a failure — the record above stands without it, exactly as it does on every other desk.',
  },
}

/** The channel producers themselves — rows that are a VOICE, never a spine. */
export const ASSESSMENT_ANALYSTS: ReadonlySet<string> = new Set(
  Object.values(ASSESSMENT_CHANNELS).map((c) => c.analystId),
)

/**
 * Whether an analyst id names an interpretive channel rather than a record.
 *
 * The reader uses it to refuse to render a channel row AS a spine: scoping to an
 * assessment row means "read the record it was written from", never "treat the
 * voice as the record".
 */
export function isAssessmentAnalyst(analystId: string | null | undefined): boolean {
  return !!analystId && ASSESSMENT_ANALYSTS.has(analystId)
}

/**
 * The SPINE producer a channel row was written from.
 *
 * Scoping to a voice means "read the record it grades": the reader resolves the
 * channel back to its spine rather than trying to render a voice as a record, or
 * falling back to whatever producer the panel happened to default to.
 */
export function spineAnalystFor(analystId: string | null | undefined): string | null {
  if (!analystId) return null
  for (const [spine, channel] of Object.entries(ASSESSMENT_CHANNELS)) {
    if (channel.analystId === analystId) return spine
  }
  return null
}

/**
 * The channel that belongs to the record on screen, or null when its tier has
 * no interpretive voice (region, thematic) or the row carries no assembly at
 * all (a legacy prose run, which the reader renders as composed prose).
 */
export function assessmentChannelFor(
  row: ReadFindingRow | null | undefined,
  assembly: AssemblyPayload | null | undefined,
): AssessmentChannel | null {
  if (!row || !assembly) return null
  const channel = row.analyst_id ? ASSESSMENT_CHANNELS[row.analyst_id] : undefined
  if (!channel) return null
  // The producer and the payload must agree about what this record is.
  if (assembly.tier !== channel.tier) return null
  return channel
}

/**
 * The channel row to render beside a given record.
 *
 * Returns the row FENCED to this record (`derived_from[0] === spine.id`) when
 * one exists; otherwise the newest row the channel has for this record's desk,
 * which the band renders as a spine mismatch. Null when the channel has nothing
 * that could be about this record — the honest absence.
 */
export function pickAssessmentRow(
  rows: readonly ReadFindingRow[] | undefined,
  channel: AssessmentChannel | null,
  spine: ReadFindingRow | null,
): ReadFindingRow | null {
  if (!channel || !spine || !rows || rows.length === 0) return null
  const candidates = rows.filter(
    (r) =>
      r.analyst_id === channel.analystId &&
      (!channel.targetScoped || (r.target_id ?? null) === (spine.target_id ?? null)),
  )
  if (candidates.length === 0) return null
  const onSpine = candidates.find((r) => (r.derived_from ?? [])[0] === spine.id)
  if (onSpine) return onSpine
  return (
    [...candidates].sort((a, b) => Date.parse(b.produced_at) - Date.parse(a.produced_at))[0] ?? null
  )
}
