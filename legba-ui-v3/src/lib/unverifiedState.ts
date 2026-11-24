/**
 * unverifiedState — the honesty state of a finding with NO verification block
 * (X4 READPATH, W11-F53).
 *
 * A finding is written, THEN judged: the verify pass runs after the INSERT and
 * its critique lands minutes later (p50 ≈ 69 s, max 549 s over 7 days,
 * measured 2026-10-13). A roll that recreates the runtime mid-pass loses the
 * critique for good. Before this module a block-less row rendered as a plain
 * current read beside its judged neighbours.
 *
 * THE REGISTRY SAYS WHICH, THE UI DOES NOT INFER IT
 * (`registry/verification_state.py`):
 *
 *   * `/findings?fields=judgment` — `verification` IS the state block,
 *     `{score_state: "pending"|"absent", since, judge_window_s, reason}`.
 *   * `/findings` (full weight) — `verification` stays `null` (two dozen
 *     readers depend on that) and `verification_state` carries the block.
 *
 * Two cases the registry cannot speak to, each decided here once:
 *
 *   * a LIVE-TAIL row (`live: true`) arrived over the write event, which by
 *     construction precedes its verify pass → `pending`;
 *   * a row from a registry that predates the state block (no block, no
 *     state) → `absent`: "not judged" is literally true of it at read time,
 *     and "pending" would be a promise nobody made.
 *
 * Never a score, never a 0: absence renders as absence.
 */

export type UnverifiedState = 'pending' | 'absent'

/** The two render strings — verbatim on every findings surface. */
export const UNVERIFIED_LABEL: Record<UnverifiedState, string> = {
  pending: 'unverified · judge pending',
  absent: 'unverified · not judged',
}

/** The registry's state block (`verification_state.unverified_state`). */
export interface VerificationStateBlock {
  score_state: UnverifiedState
  /** The finding's own `produced_at` — the clock the window runs from. */
  since: string | null
  /** The window inside which a verdict may still land, seconds. */
  judge_window_s: number | null
  /** `awaiting_verify` | `no_verdict` | `verify_exempt` (or null when unstated). */
  reason: string | null
}

export interface UnverifiedInfo {
  state: UnverifiedState
  /** Where the state came from — the registry's block, the live tail, or neither. */
  basis: 'registry' | 'live_tail' | 'unstated'
  block: VerificationStateBlock | null
}

/** The minimal row shape every findings surface already carries. */
export interface UnverifiedRowLike {
  verification?: unknown
  verification_state?: unknown
  live?: boolean
}

function asObject(v: unknown): Record<string, unknown> | null {
  return v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

/** A state block, when `v` is one: `score_state` pending|absent and NO score. */
export function asStateBlock(v: unknown): VerificationStateBlock | null {
  const o = asObject(v)
  if (!o) return null
  const s = o.score_state
  if (s !== 'pending' && s !== 'absent') return null
  if (typeof o.faithfulness_score === 'number') return null
  return {
    score_state: s,
    since: typeof o.since === 'string' ? o.since : null,
    judge_window_s: typeof o.judge_window_s === 'number' ? o.judge_window_s : null,
    reason: typeof o.reason === 'string' ? o.reason : null,
  }
}

/**
 * The row's unverified state, or `null` when it carries a REAL verify block
 * (the verdict badge speaks for those).
 */
export function unverifiedStateOf(row: UnverifiedRowLike | null | undefined): UnverifiedInfo | null {
  if (!row) return null
  const inline = asStateBlock(row.verification)
  if (inline) return { state: inline.score_state, basis: 'registry', block: inline }
  if (asObject(row.verification)) return null
  const beside = asStateBlock(row.verification_state)
  if (beside) return { state: beside.score_state, basis: 'registry', block: beside }
  if (row.live === true) return { state: 'pending', basis: 'live_tail', block: null }
  return { state: 'absent', basis: 'unstated', block: null }
}

/** True when the registry said the producer never enters the verify pass. */
export function isVerifyExemptState(info: UnverifiedInfo | null): boolean {
  return info?.block?.reason === 'verify_exempt'
}

/** One sentence for the mark's tooltip — what the state means, with its clock. */
export function unverifiedExplain(info: UnverifiedInfo): string {
  const since = info.block?.since ? ` Written ${info.block.since.replace('T', ' ').slice(0, 19)}Z.` : ''
  const windowMin =
    info.block?.judge_window_s != null ? `${Math.round(info.block.judge_window_s / 60)} minutes` : 'minutes'
  if (info.state === 'pending') {
    if (info.basis === 'live_tail') {
      return 'Arrived over the live tail, ahead of its verify pass. Refresh the feed to read the verdict once it lands.'
    }
    return `No verify verdict yet. The verify pass runs after a finding is written and lands within ${windowMin}.${since}`
  }
  if (info.block?.reason === 'verify_exempt') {
    return `This producer never goes through the faithfulness verify pass, so no verdict will be written.${since}`
  }
  if (info.basis === 'unstated') {
    return 'The registry returned no verify verdict for this finding and did not say whether one is coming.'
  }
  return `No verify verdict was written, and the ${windowMin} in which one lands have passed. Nothing will grade this finding as it stands.${since}`
}
