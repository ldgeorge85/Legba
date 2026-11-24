/**
 * consultCore — THE Consult request, built once for every shell (M-1).
 *
 * Until 2026-11-14 the desktop assembled this body inline in
 * `panels/system/Consult.tsx` `send()` and the phone kept a stripped copy
 * (`mobile/mobileConsult.ts` `buildConsultBody`) that had drifted: no window,
 * no grounding mode, no on-screen report. Both shells now call
 * {@link buildConsultRequest}, and each supplies only what genuinely differs:
 * the question text and pins it resolved, the model, the window and grounding
 * choice, its own on-screen report, whether the turn drives the view and
 * whether writes are armed.
 *
 * PURE: no React, no store, no frame import. Everything time-dependent comes
 * in on the input (`nowMs`), so the same input always yields the same bytes.
 *
 * KEY ORDER IS PART OF THE CONTRACT. The body is serialised with
 * `JSON.stringify`, and `Consult.requestBody.test.tsx` holds the desktop's
 * bodies byte-for-byte against fixtures recorded from the inline code before
 * the extraction. Optional keys (`on_screen`, `present_case`, `allow_writes`)
 * are present ONLY when set, so a plain turn's body is the one it always was.
 */
import type { ConsultModel } from '@/lib/api'
import { roundsForTurn } from '@/lib/caseModel'
import { windowForRequest, type GroundingMode, type RequestWindow, type WindowChoiceId } from '@/lib/consultContract'
import type { PinnedRef } from '@/lib/consultContext'
import type { OnScreen } from '@/lib/onScreen'
import { DEFAULT_MAX_ROUNDS } from '@/state/consultSession'

export type ConsultTurnMode = 'chat' | 'deep'

/** One prior turn as the server reads it (`ConsultMessage`). */
export interface ConsultMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface ConsultRequestInput {
  /** The question as SENT — any pinned-context prefix already applied by the shell. */
  question: string
  /** Prior turns; extra fields on a transcript turn are dropped. */
  messages: readonly { role: 'user' | 'assistant'; content: string }[]
  sessionId: string | null
  requestId: string
  mode: ConsultTurnMode
  model: ConsultModel
  /** The window choice and, for `band`, the console time band's ends. */
  window: WindowChoiceId
  band?: { startMs: number; endMs: number }
  /** The turn's instant (a fixed span ends now). Defaults to `Date.now()`. */
  nowMs?: number
  groundingMode: GroundingMode
  pinnedContext: readonly PinnedRef[]
  /** The shell's on-screen report (`lib/onScreen` shape), or null outside a frame. */
  onScreen: OnScreen | null
  /** "Drive the view" — honoured on a chat turn only. */
  presentCase: boolean
  /** "Allow changes this turn" — honoured on a chat turn only. */
  allowWrites: boolean
  /** The raw scope predicate the user typed; blank sends null. */
  scopePredicate?: string | null
  /** The round cap the user chose (a case turn may raise the default; `lib/caseModel`). */
  maxToolRounds?: number
}

/** The body POSTed to `/consult` (`ConsultRequest` in `consult_api.py`). */
export interface ConsultRequestBody {
  question: string
  scope_predicate: string | null
  max_tool_rounds: number
  mode: ConsultTurnMode
  model: ConsultModel
  request_id: string
  messages: ConsultMessage[]
  session_id: string | null
  pinned_context: PinnedRef[]
  on_screen?: OnScreen
  present_case?: true
  allow_writes?: true
  window: RequestWindow
  grounding_mode: GroundingMode
}

/** Is this turn sent as a case? Only a chat turn can drive the view. */
export function turnPresentsCase(input: Pick<ConsultRequestInput, 'presentCase' | 'mode'>): boolean {
  return input.presentCase && input.mode === 'chat'
}

/** Are writes armed on this turn? Only a chat turn can carry them. */
export function turnArmsWrites(input: Pick<ConsultRequestInput, 'allowWrites' | 'mode'>): boolean {
  return input.allowWrites && input.mode === 'chat'
}

export function buildConsultRequest(input: ConsultRequestInput): ConsultRequestBody {
  const asCase = turnPresentsCase(input)
  const armWrites = turnArmsWrites(input)
  const now = input.nowMs ?? Date.now()
  const band = input.band ?? { startMs: NaN, endMs: NaN }
  return {
    question: input.question,
    scope_predicate: (input.scopePredicate ?? '').trim() || null,
    max_tool_rounds: roundsForTurn(input.maxToolRounds ?? DEFAULT_MAX_ROUNDS, DEFAULT_MAX_ROUNDS, asCase),
    mode: input.mode,
    model: input.model,
    request_id: input.requestId,
    messages: input.messages.map((t) => ({ role: t.role, content: t.content })),
    session_id: input.sessionId,
    pinned_context: [...input.pinnedContext],
    // U-6 — what is on screen (ids and places only; `lib/onScreen.ts`).
    ...(input.onScreen ? { on_screen: input.onScreen } : {}),
    // Sent ONLY when chosen, so a plain turn's request body is byte-identical
    // to what it was before cases existed.
    ...(asCase ? { present_case: true as const } : {}),
    ...(armWrites ? { allow_writes: true as const } : {}),
    // U-8 / Q-1 — the turn contract's inputs (`ConsultRequest.window` /
    // `.grounding_mode`).
    window: windowForRequest(input.window, band, now),
    grounding_mode: input.groundingMode,
  }
}

/** A request id: `crypto.randomUUID()` where the browser has it. */
export function newConsultRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `r-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}
