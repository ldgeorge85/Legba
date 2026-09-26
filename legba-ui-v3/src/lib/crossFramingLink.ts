/**
 * crossFramingLink — the deep link into the Cross-framing panel
 * (`analysis.cross_framing`), and the durable slot that makes it survive a
 * first click.
 *
 * The house idiom for "open THAT panel on THIS subject" is the one
 * `Optimizer` → `OptimizerDiff` established: park the request on `window` as a
 * replayable pending slot AND fire a DOM event. The event serves the already-
 * mounted case; the slot serves the first-open case, because a `CustomEvent`
 * with no listener is simply lost. The panel drains the slot once on mount, so
 * a remount never replays a stale subject and a fresh click re-parks a fresh
 * one.
 *
 * The panel materialisation itself — turning the event into an `addPanel` when
 * no tile is open yet — is the central bridge in `App.tsx`, and it is the
 * integrator's line to add (three `system.optimizer.diff` lines sit beside it
 * already). Until it exists, the subject still reaches the panel: the operator
 * opens Cross-framing from ⌘K and the parked subject is waiting. Nothing is
 * lost either way, which is the whole point of parking rather than only firing.
 */

/** Kept in sync by literal with `panels/analysis/CrossFraming.tsx`. */
export const OPEN_CROSS_FRAMING_EVENT = 'legba:open-cross-framing'
const PENDING_CROSS_FRAMING_KEY = '__legbaPendingCrossFraming'

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

function slot(): Record<string, CrossFramingSubject | undefined> {
  return window as unknown as Record<string, CrossFramingSubject | undefined>
}

/** Park the subject AND fire the open event. */
export function requestCrossFraming(subject: CrossFramingSubject): void {
  slot()[PENDING_CROSS_FRAMING_KEY] = subject
  window.dispatchEvent(
    new CustomEvent<CrossFramingSubject>(OPEN_CROSS_FRAMING_EVENT, { detail: subject }),
  )
}

/** Read AND clear the parked subject. Returns `null` when nothing is parked. */
export function drainPendingCrossFraming(): CrossFramingSubject | null {
  const w = slot()
  const subject = w[PENDING_CROSS_FRAMING_KEY]
  if (!subject) return null
  delete w[PENDING_CROSS_FRAMING_KEY]
  return subject
}
