/**
 * crossFramingLink — the deep link into the Cross-framing drill
 * (`analysis.cross_framing`).
 *
 * UI-B1/shell retired the bridge this file used to be: the `window` park plus
 * the `legba:open-cross-framing` DOM event are gone (UI-D1 §3 — "a surface
 * reachable only by an event is unaddressable"). A request is now an ordinary
 * `open_panel` drive action with the claim in its params
 * (`lib/commands.ts::bridgeActions.crossFraming`), run through the ONE
 * executor; the claim reaches the tile through `state/panelParams`, which
 * serves the first-open case (written before the tile mounts) and the
 * already-mounted case (the tile subscribes) without a park.
 */
import { drive } from '@/lib/applyAction'
import { bridgeActions } from '@/lib/commands'

/**
 * What a caller knows about the claim it wants framed.
 *
 * Only `targetId` is required, because only the desk is always knowable at a
 * call site. A caller with a `claim_contentions.claim_id` passes it (exact);
 * a caller holding only the sentence passes `claimText` and the panel matches
 * it against the record's own spans; a caller with neither gets the desk's most
 * contested claim, which is the research note's own default.
 */
export interface CrossFramingSubject {
  targetId: string
  claimId?: string | null
  claimText?: string | null
  /** Where the click came from, for the panel's own "opened from" line. */
  origin?: string
}

/** Open (or focus) the Cross-framing drill on this claim. */
export function requestCrossFraming(subject: CrossFramingSubject): void {
  drive(bridgeActions.crossFraming(subject))
}

/**
 * Read a Cross-framing subject back out of an `open_panel`'s params (the
 * `subject` ref plus `claim_id` / `claim_text` / `origin`). Null when the
 * params name no desk.
 */
export function crossFramingSubjectFromParams(
  params: Readonly<Record<string, unknown>>,
): CrossFramingSubject | null {
  const subject = params.subject as { kind?: unknown; id?: unknown } | undefined
  const targetId = subject && subject.kind === 'target' && typeof subject.id === 'string' ? subject.id : null
  if (!targetId) return null
  return {
    targetId,
    claimId: typeof params.claim_id === 'string' ? params.claim_id : null,
    claimText: typeof params.claim_text === 'string' ? params.claim_text : null,
    ...(typeof params.origin === 'string' ? { origin: params.origin } : {}),
  }
}
