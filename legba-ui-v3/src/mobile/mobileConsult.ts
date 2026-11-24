/**
 * mobileConsult — what a phone Consult turn carries, beside the shared engine.
 *
 * The request body is `lib/consultCore` `buildConsultRequest` and the turn is
 * `state/useConsultTurn` — the desktop's two (M-1). This module is only what
 * the PHONE knows (M-3g):
 *
 *   * the pin — what "Ask about this" pinned, as the desktop's own pins
 *     (`Selection`s), sent through the desktop's builders (`questionWithPins`
 *     for the `[Pinned …]` prefix, `pinnedContext` for the structured
 *     `pinned_context`). Nothing here hand-builds either;
 *   * the on-screen report — the shell's, through its frame port
 *     (`describeOnScreen()`: while Consult is the open tab, the screen the
 *     person came from, `chat: {placement: 'tab'}`), with a small truthful
 *     fallback when no port is registered (a bare test, a crash in the shell);
 *   * the window — the shell's window chips (`useWorldState`), not a fixed
 *     week: 24 h, 7 d or 30 d, sent as the matching fixed span. A window the
 *     question states still wins on the server (the contract says so);
 *   * the route and the arming — the free self-hosted plane always
 *     (`model: 'core'`), writes never armed; the drive mode is the desktop's
 *     (`state/driveMode`), so a turn is a case only when the reader turned it on.
 */
import { questionWithPins, pinnedContext } from '@/lib/consultContext'
import type { ConsultModel } from '@/lib/api'
import type { GroundingMode, WindowChoiceId } from '@/lib/consultContract'
import { describeOnScreen, type OnScreen } from '@/lib/onScreen'
import type { Selection } from '@/state/selection'
import type { ConsultTurnSend } from '@/state/useConsultTurn'
import { phoneWindowOf, type PhoneWindowId } from '@/mobile/screens/changed/changedModel'

/** The Consult store slice the phone owns (the desktop's tiles have their own). */
export const MOBILE_PANEL_ID = 'mobile'

/**
 * The route the phone asks on: the free self-hosted plane, always (plan §2.1;
 * the standing rule that an accidental press never bills a paid plane). The
 * desktop's persisted pick (`legba_consult_model`) is deliberately NOT read.
 */
export const PHONE_CONSULT_MODEL: ConsultModel = 'core'

/** Mirrors `ConsultRequest.question`'s `max_length`; the box leaves the pins room. */
export const QUESTION_MAX = 8192
export const QUESTION_BOX_MAX = 6000

/** The window chip → the request's fixed span (`lib/consultContract` choices). */
const CHOICE_OF: Record<PhoneWindowId, WindowChoiceId> = { '24h': '24h', '7d': '168h', '30d': '720h' }

const WORDS_OF: Record<PhoneWindowId, string> = {
  '24h': 'the last 24 hours',
  '7d': 'the last 7 days',
  '30d': 'the last 30 days',
}

/** The request window for the shell's chip. */
export function phoneWindowChoice(spanMs: number): WindowChoiceId {
  return CHOICE_OF[phoneWindowOf(spanMs).id]
}

/** The chip's window in plain words ("the last 7 days"). */
export function phoneWindowWords(spanMs: number): string {
  return WORDS_OF[phoneWindowOf(spanMs).id]
}

/** What "Ask about this" pinned: the desktop's pins, and the line the screen shows. */
export interface PhoneAsking {
  pins: Selection[]
  label: string
}

/**
 * The on-screen report when no frame port is registered — minimal and true:
 * the point is the first pin, the window is the chip's, Consult is a tab.
 * Ids and places only, never content (the shape and budget of `lib/onScreen`).
 */
export function phoneOnScreen(asking: PhoneAsking | null, spanMs: number, nowMs: number = Date.now()): OnScreen {
  const iso = (ms: number) => new Date(ms).toISOString().slice(0, 16) + 'Z'
  const w = phoneWindowOf(spanMs)
  const pin = asking?.pins[0] ?? null
  const label = pin?.label?.trim() ? pin.label.trim().slice(0, 80) : null
  return {
    v: 1,
    page: 'phone',
    side: 'reader',
    point: pin ? { kind: pin.kind, id: pin.id.slice(0, 200), label: label && label !== pin.id ? label : null } : null,
    window: { span: w.id, from: iso(nowMs - w.ms), to: iso(nowMs) },
    views: [],
    chat: { placement: 'tab', collapsed: false },
  }
}

export interface PhoneTurnArgs {
  /** The question the person typed (trimmed). */
  question: string
  asking: PhoneAsking | null
  maxRounds: number
  /** The shell's window (`useWorldState.spanMs`). */
  spanMs: number
  groundingMode: GroundingMode
  /** The drive mode (`state/driveMode`): on sends the turn as a case. */
  presentCase: boolean
  nowMs?: number
}

/**
 * The `request` half of a phone turn for `useConsultTurn().send` — everything
 * but the request id, session, history, mode and model, which the hook adds.
 */
export function phoneTurnRequest(args: PhoneTurnArgs): ConsultTurnSend['request'] {
  const now = args.nowMs ?? Date.now()
  const pins = args.asking?.pins ?? []
  const question = questionWithPins(args.question, pins)
  return {
    question: question.length > QUESTION_MAX ? args.question.slice(0, QUESTION_MAX) : question,
    // Left null deliberately: `scope_predicate` is a backend expression
    // dialect this surface cannot validate; the pin names the subject instead.
    scopePredicate: null,
    maxToolRounds: args.maxRounds,
    pinnedContext: pinnedContext(pins),
    onScreen: describeOnScreen() ?? phoneOnScreen(args.asking, args.spanMs, now),
    presentCase: args.presentCase,
    // never armed from a phone (plan §2.1)
    allowWrites: false,
    window: phoneWindowChoice(args.spanMs),
    nowMs: now,
    groundingMode: args.groundingMode,
  }
}
