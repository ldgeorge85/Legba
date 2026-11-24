/**
 * scaleStamp — read the scale + method stamps off an instrument row (K3).
 *
 * Every number the platform publishes is a reading on some scale, and until
 * K3 the reader had no way to know WHICH. Migration 0188 re-based every stored
 * `intensity_score` in August 2026 by the split frame's share of the members —
 * an instrument change with no code change at all — so an intensity of 59 in
 * July and 59 today are readings on two different scales, and nothing on the
 * surface said so.
 *
 * THE TWO STAMPS ANSWER DIFFERENT QUESTIONS, which is why both are shown and
 * neither substitutes for the other:
 *
 *   * `method_version` (H12) — was this number produced by the same CODE?
 *     Bumps on any rule change. `situation_clustering/2026-09.1`.
 *   * `scale_version`  (K3)  — does this number MEAN the same thing? Bumps
 *     only when the comparability frame moves, and is named for the QUANTITY
 *     rather than the module, because a scale outlives the handler publishing
 *     onto it. `intensity/2026-08`.
 *
 * THE STATES, and the honesty rule that produces them: a row carrying both is
 * `full`; a row carrying only a method version is `method-only` (true of every
 * server-flattened projection that has no scale of its own yet); a row carrying
 * neither and written BEFORE the stamps existed is `unstamped` and renders as
 * **"unstamped (pre-2026-09)"** — an absence rendered as absence. A row carrying
 * neither but written AFTER the stamps existed (W11-F9: a 2026-10 country
 * composition) is `undeclared` and renders as **"no declared scale"**: its
 * producer is not an instrument that publishes onto a declared scale, and
 * calling it "pre-2026-09" would date it wrongly. What there is NOT is a state where a missing
 * stamp is filled in from the current constant: back-labelling would assert a
 * frame that was never declared, and for `acute_forecasts` it would assert that
 * the voided pre-clamp batch was on today's probability scale, the precise
 * falsehood migration 0075 exists to prevent.
 *
 * Pure, DOM-free. The chip is `@/components/ScaleStamp`.
 */

/** What the reader is being told about this number's comparability.
 *
 *  `scale-only` is the shape our own writers cannot produce — every module
 *  declaring a scale declares a method too, pinned by
 *  `tests/data_pkg/test_scale_versions.py` — so it exists here only so an
 *  unexpected row is NAMED rather than quietly mislabelled `method-only`. */
export type ScaleStampState = 'full' | 'method-only' | 'scale-only' | 'unstamped' | 'undeclared'

/** The stamps carried by one instrument row, plus what they add up to. */
export interface ScaleStamp {
  /** The comparability frame, verbatim, or null. Never inferred. */
  scale: string | null
  /** The instrument revision, verbatim, or null. Never inferred. */
  method: string | null
  state: ScaleStampState
}

/** The label a row with no stamps at all earns. Dated to the release that
 *  introduced the stamps, so the reader knows what the absence MEANS: not
 *  "unknown scale" but "written before the platform recorded one". */
export const UNSTAMPED_LABEL = 'unstamped (pre-2026-09)'

/** The label a row with no stamps earns when it was written AFTER the stamps
 *  existed — its producer declares no scale, which is not the same absence as
 *  "written before the platform recorded one" (W11-F9). */
export const UNDECLARED_LABEL = 'scale not stated'

/** When the stamps began: roll 12 (2026-09-24 02:21Z) applied migration 0211
 *  and shipped H12's `method_version` on every instrument (docs/ANALYSIS.md
 *  §10.9's "Since 2026-09-24"). A row produced before this instant can only be
 *  unstamped; a row produced after it with no stamp is undeclared. */
export const STAMP_ERA_START = '2026-09-24T02:21:00Z'

/** What a scale change costs the reader, in one sentence — the half of the
 *  chip's tooltip docs/ANALYSIS.md §10.9.1 expands on. */
export const SCALE_EXPLAINER =
  'the frame this number is comparable within. Two readings on the same scale ' +
  'can be compared directly; across a scale change they cannot, however ' +
  'similar the numbers look.'

/** What a method version tells the reader, in one sentence. */
export const METHOD_EXPLAINER =
  'the instrument revision that computed this number. A method can be revised ' +
  'without moving the scale.'

/** Why a row can carry no stamps, in one sentence. */
export const UNSTAMPED_EXPLAINER =
  'this row was written before the platform stamped scales, so its frame of ' +
  'comparison is not recorded. It is not assumed to match today\'s.'

/** Why a row written after the stamps existed can still carry none. */
export const UNDECLARED_EXPLAINER =
  'this row was written after the platform began stamping scales, and its ' +
  'producer declares none: it is not a number on a declared scale, so no ' +
  'version-to-version comparison is claimed for it.'

/** True for both no-stamp states — a chip that hides or mutes an absent stamp
 *  treats the two alike; only the words differ. */
export function isStampAbsent(state: ScaleStampState): boolean {
  return state === 'unstamped' || state === 'undeclared'
}

function stringAt(source: unknown, key: string): string | null {
  if (!source || typeof source !== 'object') return null
  const v = (source as Record<string, unknown>)[key]
  return typeof v === 'string' && v ? v : null
}

function objectAt(source: unknown, key: string): unknown {
  if (!source || typeof source !== 'object') return null
  const v = (source as Record<string, unknown>)[key]
  return v && typeof v === 'object' ? v : null
}

/** The instant the row was produced, in epoch ms, or null when it says none. */
function producedMs(row: unknown): number | null {
  const at = stringAt(row, 'produced_at') ?? stringAt(row, 'created_at')
  if (!at) return null
  const ms = Date.parse(at)
  return Number.isFinite(ms) ? ms : null
}

/**
 * Read both stamps off a row, tolerating the three homes they live in, first
 * hit wins:
 *
 *   1. a top-level field — a server-flattened projection like
 *      `DeskBaselineRow` or `ForecastDue`; the projection the server chose;
 *   2. nested under `data` — a raw `situations` payload, or an instrument
 *      payload handed over on its own;
 *   3. nested under `data.data` — a `/findings` row (W11-F9). The route serves
 *      `analyst_outputs.data` as `row.data`, and that column holds the WHOLE
 *      finding envelope (`{title, body, data, …}`); the instrument's stamp
 *      rides the envelope's own `data` (docs/ANALYSIS.md §10.9: "It rides
 *      `data.method_version` on `analyst_outputs` rows"), one level deeper
 *      than this reader used to look — so every stamped finding read as
 *      unstamped.
 *
 * With no stamp in any home, the row's own `produced_at` decides which absence
 * it is: before {@link STAMP_ERA_START} → `unstamped`; on or after it →
 * `undeclared`. A row with no date stays `unstamped`, the older claim.
 *
 * An empty string is absence, not a stamp: a blank chip would read as a
 * version nobody can name.
 */
export function readScaleStamp(row: unknown): ScaleStamp {
  const nested = objectAt(row, 'data')
  const envelope = objectAt(nested, 'data')
  const scale =
    stringAt(row, 'scale_version') ??
    stringAt(nested, 'scale_version') ??
    stringAt(envelope, 'scale_version')
  const method =
    stringAt(row, 'method_version') ??
    stringAt(nested, 'method_version') ??
    stringAt(envelope, 'method_version')
  let state: ScaleStampState = 'unstamped'
  if (scale && method) state = 'full'
  else if (method) state = 'method-only'
  else if (scale) state = 'scale-only'
  else {
    const at = producedMs(row)
    if (at !== null && at >= Date.parse(STAMP_ERA_START)) state = 'undeclared'
  }
  return { scale, method, state }
}

/** The chip's visible text for a stamp — `scale · method`, the half that
 *  exists, or the honest unstamped label. Never a fabricated half. */
export function scaleStampLabel(stamp: ScaleStamp): string {
  if (stamp.scale && stamp.method) return `${stamp.scale} · ${stamp.method}`
  if (stamp.scale) return stamp.scale
  if (stamp.method) return stamp.method
  return stamp.state === 'undeclared' ? UNDECLARED_LABEL : UNSTAMPED_LABEL
}

/** The chip's tooltip — each half explained, so a reader meeting the chip for
 *  the first time learns what a scale change means for their comparison. */
export function scaleStampTooltip(stamp: ScaleStamp): string {
  if (stamp.state === 'unstamped') return UNSTAMPED_EXPLAINER
  if (stamp.state === 'undeclared') return UNDECLARED_EXPLAINER
  const parts: string[] = []
  if (stamp.scale) parts.push(`scale ${stamp.scale} — ${SCALE_EXPLAINER}`)
  if (stamp.method) parts.push(`method ${stamp.method} — ${METHOD_EXPLAINER}`)
  return parts.join('\n\n')
}
