/**
 * consultLiveness — what the Consult panel says during a QUIET stretch of a
 * live run (W2 S-batch).
 *
 * The step stream goes silent for minutes during a long final synthesis: the
 * model is writing and no step frame is due until it returns. The relay kept
 * the connection warm with an SSE COMMENT, which an `EventSource` never hands
 * to script — so the panel showed nothing, and a run that was working looked
 * exactly like one that had hung.
 *
 * The relay now sends a NAMED `keepalive` event (`consult_stream_api.py`):
 * `event: keepalive` + `data: {"type":"keepalive","idle_s":N}`. A keepalive
 * that arrived AFTER the last frame is proof the run is alive and quiet, and
 * only then does the panel say so: "synthesising · Ns" on the last round when
 * the drilling has ended, "working · Ns" when it cannot tell. N is the
 * silence since the last frame, ticking locally between keepalives. With no
 * keepalive since the last frame — or one older than {@link KEEPALIVE_STALE_MS}
 * — nothing is claimed: a silence the relay has not vouched for is not
 * presented as progress.
 */
import type { StepFrame } from '@/state/consultSession'

/** The relay keeps alive every 25 s; past two missed beats it has not vouched
 *  for the silence any more. */
export const KEEPALIVE_STALE_MS = 60_000

/**
 * Step kinds after which the loop is no longer drilling: whatever it is doing
 * in the silence that follows is writing the answer. `awaiting_answer` is the
 * panel's own marker for the actor's bare `final` (the registry is persisting).
 */
export const SYNTHESIS_STEP_KINDS: ReadonlySet<string> = new Set([
  'wall_budget_reached',
  'total_budget_reserve_reached',
  'spend_ceiling_reached',
  'forced_final',
  'final',
  'awaiting_answer',
])

/** One live stream's clock, held by the panel for the turn it is relaying. */
export interface StreamLiveness {
  requestId: string
  /** Epoch ms of the last frame the stream delivered (or of its opening). */
  lastFrameAt: number
  /** Epoch ms of the newest keepalive SINCE that frame; `null` when none. */
  keepaliveAt: number | null
}

/** The relay's keepalive payload, or `null` for anything else. */
export function parseKeepalive(data: unknown): { idle_s: number | null } | null {
  let body: unknown = data
  if (typeof data === 'string') {
    try {
      body = JSON.parse(data)
    } catch {
      return null
    }
  }
  if (!body || typeof body !== 'object') return null
  const b = body as Record<string, unknown>
  if (b.type !== 'keepalive') return null
  return { idle_s: typeof b.idle_s === 'number' && Number.isFinite(b.idle_s) ? b.idle_s : null }
}

/** Has the loop stopped drilling? The last step says so, or the round cap is spent. */
export function drillingEnded(steps: readonly StepFrame[], roundCap?: number | null): boolean {
  const last = steps[steps.length - 1]
  if (last && SYNTHESIS_STEP_KINDS.has(String(last.kind ?? ''))) return true
  if (typeof roundCap === 'number' && roundCap > 0) {
    const reached = steps.reduce(
      (m, s) => (typeof s.round === 'number' && s.round > m ? s.round : m),
      0,
    )
    if (reached >= roundCap) return true
  }
  return false
}

export interface QuietRound {
  /** `synthesising · 42s` / `working · 42s`. */
  text: string
  synthesising: boolean
  seconds: number
}

/**
 * The quiet-stretch line for the last round, or `null` when the silence is
 * not vouched for (no keepalive since the last frame, or a stale one).
 */
export function quietRound(
  liveness: StreamLiveness | null | undefined,
  steps: readonly StepFrame[],
  roundCap: number | null | undefined,
  now: number,
): QuietRound | null {
  if (!liveness || liveness.keepaliveAt === null) return null
  if (now - liveness.keepaliveAt > KEEPALIVE_STALE_MS) return null
  const seconds = Math.max(0, Math.round((now - liveness.lastFrameAt) / 1000))
  const synthesising = drillingEnded(steps, roundCap)
  return {
    text: `${synthesising ? 'synthesising' : 'working'} · ${seconds}s`,
    synthesising,
    seconds,
  }
}
