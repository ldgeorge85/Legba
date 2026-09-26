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
 * neither is `unstamped` and renders as **"unstamped (pre-2026-09)"** — an
 * absence rendered as absence. What there is NOT is a state where a missing
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
export type ScaleStampState = 'full' | 'method-only' | 'scale-only' | 'unstamped'

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

function stringAt(source: unknown, key: string): string | null {
  if (!source || typeof source !== 'object') return null
  const v = (source as Record<string, unknown>)[key]
  return typeof v === 'string' && v ? v : null
}

/**
 * Read both stamps off a row, tolerating the two homes they live in: a
 * top-level field (a server-flattened projection like `DeskBaselineRow` or
 * `ForecastDue`) or nested under `data` (a raw `analyst_outputs` / `situations`
 * payload, where the stamp rides the JSONB). Top level wins when both are
 * present — it is the projection the server chose to publish.
 *
 * An empty string is absence, not a stamp: a blank chip would read as a
 * version nobody can name.
 */
export function readScaleStamp(row: unknown): ScaleStamp {
  const nested = row && typeof row === 'object' ? (row as Record<string, unknown>).data : null
  const scale = stringAt(row, 'scale_version') ?? stringAt(nested, 'scale_version')
  const method = stringAt(row, 'method_version') ?? stringAt(nested, 'method_version')
  let state: ScaleStampState = 'unstamped'
  if (scale && method) state = 'full'
  else if (method) state = 'method-only'
  else if (scale) state = 'scale-only'
  return { scale, method, state }
}

/** The chip's visible text for a stamp — `scale · method`, the half that
 *  exists, or the honest unstamped label. Never a fabricated half. */
export function scaleStampLabel(stamp: ScaleStamp): string {
  if (stamp.scale && stamp.method) return `${stamp.scale} · ${stamp.method}`
  if (stamp.scale) return stamp.scale
  if (stamp.method) return stamp.method
  return UNSTAMPED_LABEL
}

/** The chip's tooltip — each half explained, so a reader meeting the chip for
 *  the first time learns what a scale change means for their comparison. */
export function scaleStampTooltip(stamp: ScaleStamp): string {
  if (stamp.state === 'unstamped') return UNSTAMPED_EXPLAINER
  const parts: string[] = []
  if (stamp.scale) parts.push(`scale ${stamp.scale} — ${SCALE_EXPLAINER}`)
  if (stamp.method) parts.push(`method ${stamp.method} — ${METHOD_EXPLAINER}`)
  return parts.join('\n\n')
}
