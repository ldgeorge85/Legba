/**
 * mobileConsult — the Consult, with the report pinned to it.
 *
 * In the v2 Crossroads the Consult pane sat at the centre of the workspace and
 * carried the current selection as its context ("Viewing: Iran (entity)"). That
 * is the piece the workstation lost and the operator asked for back: on this
 * surface the Consult is a sheet you can pull up from ANY screen, and whatever
 * report you are reading is already in it.
 *
 * ── HOW THE CONTEXT ACTUALLY REACHES THE MODEL ────────────────────────────
 * The workstation sends context on two channels: a text prefix on `question`,
 * and a structured `pinned_context` array. Only the FIRST of those reaches the
 * model TODAY. `ConsultRequest` in `src/legba/data/registry/consult_api.py`
 * declares `question`, `scope_predicate`, `max_tool_rounds`, `mode`, `model`,
 * `messages`, `request_id` and `session_id` — and nothing else. It sets no
 * `model_config`, so Pydantic v2's default `extra='ignore'` silently drops
 * `pinned_context` on arrival.
 *
 * So this surface sends BOTH, and neither is decorative:
 *   * the TEXT PREFIX, because it is the channel that demonstrably works
 *     against the server running right now; and
 *   * the STRUCTURED ARRAY, in the `{kind, id, title, text}` shape the
 *     registry is growing a real field for (kinds `report | finding | entity |
 *     target | signal`, rendered server-side as a PINNED CONTEXT block and
 *     forwarded on the deep path). It is inert until that lands and costs
 *     nothing meanwhile, and the two channels carry the same substance so the
 *     answer cannot change depending on which one the server reads.
 *
 * ── WHY THE BUDGET MATTERS ────────────────────────────────────────────────
 * `question` is `Field(min_length=1, max_length=8192)`. A pinned context is
 * prepended to text the operator typed, so an ungoverned context block turns a
 * long report into a 422 on a question the operator can see nothing wrong
 * with. {@link buildPinnedContext} is therefore budgeted and truncates the
 * BLUF rather than the operator's own words, and {@link composeQuestion}
 * guarantees the result fits.
 */

import {
  apiPostWithStatus,
  getConsultRun,
  type ConsultModel,
  type ConsultRunStatus,
} from '@/lib/api'
import type { ChatTurn } from '@/state/consultSession'

/** Mirrors `ConsultRequest.question`'s `max_length` — the hard server ceiling. */
export const QUESTION_MAX = 8192

/**
 * The most a pinned context may occupy, leaving the operator ~6 kB of question.
 * A report's identity + lead is worth about a paragraph; anything longer is the
 * report arguing with the operator for room.
 */
export const CONTEXT_BUDGET = 2000

/** What the sheet knows about the current selection. */
export interface PinnedReport {
  id: string
  title: string
  tier: string
  target: string
  /** The read's lead thread — the sentence that says what today is about. */
  bluf: string
  producedAt: string
}

export interface PinnedEntity {
  id: string
  name: string
}

function clip(text: string, max: number): string {
  const t = text.trim().replace(/\s+/g, ' ')
  if (t.length <= max) return t
  // Cut on a word boundary so the model never sees a severed token, and mark
  // the cut so it knows the sentence was truncated rather than ending there.
  const cut = t.slice(0, max - 1)
  const lastSpace = cut.lastIndexOf(' ')
  return `${(lastSpace > max * 0.6 ? cut.slice(0, lastSpace) : cut).trimEnd()}…`
}

/**
 * The context block prepended to the operator's question.
 *
 * Shaped as a labelled preamble rather than prose so the model can tell the
 * pinned record from the question about it, and carrying the report's ID so
 * the answer can cite the row it was asked about.
 */
export function buildPinnedContext(
  report: PinnedReport | null,
  entity: PinnedEntity | null,
  budget = CONTEXT_BUDGET,
): string {
  if (!report) return ''
  const lines = [
    '[Pinned report]',
    `id: ${report.id}`,
    `tier: ${report.tier}`,
    `target: ${report.target}`,
    `produced_at: ${report.producedAt}`,
    `title: ${clip(report.title, 240)}`,
  ]
  if (entity) lines.push(`focused on: ${entity.name} (target id=${entity.id})`)
  const head = lines.join('\n')
  // The BLUF gets whatever the budget has left after the identity lines, which
  // are the part that must never be dropped — an answer about the wrong report
  // is worse than an answer with a shortened lead.
  const remaining = budget - head.length - '\nlead: '.length
  if (report.bluf && remaining > 80) {
    return `${head}\nlead: ${clip(report.bluf, remaining)}`
  }
  return head
}

/**
 * Prefix the question with its context, never exceeding the server's ceiling.
 *
 * When the two together would overflow, the CONTEXT is what shrinks: the
 * operator's own words are the one thing this function will not edit.
 */
export function composeQuestion(context: string, question: string): string {
  const q = question.trim()
  if (!context) return q.slice(0, QUESTION_MAX)
  const joined = `${context}\n\n${q}`
  if (joined.length <= QUESTION_MAX) return joined
  const room = QUESTION_MAX - q.length - 2
  if (room < 40) return q.slice(0, QUESTION_MAX)
  return `${clip(context, room)}\n\n${q}`
}

// ── the request ──────────────────────────────────────────────────────────────

export interface ConsultCitedRefOut {
  kind: string
  id: string
  description?: string | null
}

/** Mirrors the workstation's `ConsultResponse` (panels/system/Consult.tsx). */
export interface MobileConsultResponse {
  answer: string
  finding_id: string | null
  derived_from?: string[]
  tool_calls?: unknown[]
  cited_refs?: ConsultCitedRefOut[]
  uncertainty?: number | null
  unanswered_aspects?: string[]
  session_id?: string | null
  model?: string | null
  /**
   * `'accepted'` when the run outlived the POST (HTTP 202) and `answer` is
   * therefore empty — see {@link sendConsult}, which resolves it before any
   * caller sees it.
   */
  status?: 'complete' | 'accepted'
  request_id?: string | null
}

/**
 * One structured pin, in the shape the incoming server-side field will take.
 *
 * `kind` is drawn from the bounded set the registry work is adding
 * (`report | finding | entity | target | signal`); `text` carries the same
 * substance the prefix does, so a server that starts reading this field renders
 * an equivalent PINNED CONTEXT block without this client changing.
 */
export interface StructuredPin {
  kind: 'report' | 'finding' | 'entity' | 'target' | 'signal'
  id: string
  title: string
  text: string
}

/**
 * The structured half of the pin, sent alongside the text prefix.
 *
 * Today's registry drops this field (Pydantic `extra='ignore'`), so it is inert
 * and harmless; the moment the server grows the field the phone surface starts
 * sending a first-class pin with no redeploy of this code. Both channels carry
 * the SAME content deliberately — a server that reads one and a server that
 * reads the other must produce the same answer.
 */
export function buildStructuredPins(
  report: PinnedReport | null,
  entity: PinnedEntity | null,
): StructuredPin[] {
  const pins: StructuredPin[] = []
  if (report) {
    pins.push({
      kind: 'report',
      id: report.id,
      title: report.title,
      text: buildPinnedContext(report, null),
    })
  }
  if (entity) {
    pins.push({
      kind: 'target',
      id: entity.id,
      title: entity.name,
      text: `The reader is focused on ${entity.name} (target id=${entity.id}) within this report.`,
    })
  }
  return pins
}

export interface SendConsultArgs {
  question: string
  report: PinnedReport | null
  entity: PinnedEntity | null
  transcript: ChatTurn[]
  sessionId: string | null
  model: ConsultModel
  maxRounds: number
  requestId: string
}

/**
 * Build the exact body the workstation sends, plus the pinned preamble.
 *
 * Split out from {@link sendConsult} so a test can assert the pinned context
 * lands in the payload without a network round trip — the one behaviour this
 * whole module exists to guarantee.
 */
export function buildConsultBody(args: SendConsultArgs): Record<string, unknown> {
  const context = buildPinnedContext(args.report, args.entity)
  return {
    question: composeQuestion(context, args.question),
    // Left null deliberately: `scope_predicate` is a backend expression
    // dialect (`target_id == "..."`) that this surface has no way to validate,
    // and a malformed predicate would narrow the answer silently. The target
    // is named in the pinned context instead, where it is unambiguous.
    scope_predicate: null,
    max_tool_rounds: args.maxRounds,
    mode: 'chat',
    model: args.model,
    request_id: args.requestId,
    messages: args.transcript.map((t) => ({ role: t.role, content: t.content })),
    session_id: args.sessionId,
    pinned_context: buildStructuredPins(args.report, args.entity),
  }
}

/**
 * How long the mobile sheet waits on a detached run, and how often it asks.
 *
 * Mobile has no SSE stream (see `ConsultSheet`), so where the workstation
 * panel is told the answer, this surface has to go and look. The deadline
 * matches the analyst's total wall-clock budget plus delivery slack: past it
 * the loop has emitted its own degraded final, so nothing more is coming.
 */
const RUN_POLL_MS = 2000
const RUN_DEADLINE_MS = 540_000

/**
 * POST one chat turn and return its answer.
 *
 * The front door answers 200 with the answer, or **202** when the run outlived
 * the request — the shape that stopped a long consult being lost with the
 * caller's timeout (see `consult_runs.py`). On a 202 there is nothing to
 * render yet, so this polls the run to completion and returns the same body
 * either way: the caller sees one contract, not two.
 *
 * A run that fails still returns, carrying the PARTIAL the server persisted,
 * because this sheet must never show a blank turn.
 *
 * Throws `ApiError` on a non-2xx, like every other call.
 */
export async function sendConsult(args: SendConsultArgs): Promise<MobileConsultResponse> {
  const { status, body } = await apiPostWithStatus<MobileConsultResponse>(
    '/consult',
    buildConsultBody(args),
  )
  if (status !== 202 && body.status !== 'accepted') return body
  return awaitConsultRun(args.requestId, body)
}

/** Poll a detached run to completion; exported so a test can drive it directly. */
export async function awaitConsultRun(
  requestId: string,
  accepted: MobileConsultResponse,
  { pollMs = RUN_POLL_MS, deadlineMs = RUN_DEADLINE_MS } = {},
): Promise<MobileConsultResponse> {
  const started = Date.now()
  while (Date.now() - started < deadlineMs) {
    await new Promise((resolve) => setTimeout(resolve, pollMs))
    let run: ConsultRunStatus
    try {
      run = await getConsultRun(requestId)
    } catch {
      // Aged out of the registry's memory, or unreachable. The durable record
      // is the session's turns, which the sheet reconciles on its next open.
      break
    }
    if (run.status === 'running') continue
    if (run.status === 'complete' && run.response) {
      return { ...accepted, ...(run.response as MobileConsultResponse) }
    }
    const detail =
      typeof run.error_detail === 'string'
        ? run.error_detail
        : JSON.stringify(run.error_detail ?? 'the run failed')
    return {
      ...accepted,
      answer: `_This consult did not finish._\n\nReason: ${detail}`,
    }
  }
  return {
    ...accepted,
    answer:
      '_This consult is still running._\n\nReopen this conversation shortly — ' +
      'the answer is recorded on the session whether or not this sheet is open.',
  }
}
