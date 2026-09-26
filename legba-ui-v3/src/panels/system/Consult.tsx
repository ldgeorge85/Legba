/**
 * O-Consult / system.consult — daily-driver consult panel (Piece 1 rework).
 *
 * Wired to the `consult_on_demand` analyst kind (L-178) via the registry
 * proxy at `POST /api/v1/consult` (see
 * `src/legba/data/registry/consult_api.py`).
 *
 * This panel is a multi-turn CHAT surface:
 *   - `mode:'chat'` (default) → the actor answers without writing a finding;
 *     the answer comes back in the envelope (no DB row). History threads
 *     under a server-side session and is re-sent as `messages[]` on each turn.
 *   - Live thinking → before POSTing, the panel mints a `request_id`, opens an
 *     `EventSource` on `/api/v1/consult/stream/<id>?token=...`, and renders each
 *     ReAct step as it streams; the stream closes on the terminal `final` frame.
 *
 * Deep, durable analysis (a long-running task that writes a finding) lives in
 * its OWN `Deep Consult` panel (`system.deep_consult`) — this panel is the
 * lightweight chat surface only.
 *
 * The conversation is NOT component state (GLASS-4)
 * =================================================
 *
 * Transcript, session id, pins, the in-flight turn and its live step ticker all
 * live in `state/consultSession.ts`, keyed by `registration.id`. This panel is
 * a view over that slice.
 *
 * That is a correctness requirement, not tidiness. Dockview destroys a panel's
 * React tree on `api.clear()` — which every layout preset and both Investigate
 * grids call — so with the state held locally, picking a preset silently
 * discarded an in-flight turn along with the whole conversation above it. With
 * the state in the store the unmount costs only the DOM: the `fetch` in `send`
 * is never aborted and its continuation writes through the store, so the answer
 * still lands and is waiting when the panel reopens.
 *
 * Three things follow from that split, and each is load-bearing:
 *
 *   1. **The EventSource is closed on unmount.** It is the one resource that
 *      genuinely cannot outlive the component, so `send` registers it on a ref
 *      and an unmount cleanup closes it. The turn keeps running; only the live
 *      ticker stops, and the steps collected so far stay on the pending turn.
 *   2. **On mount the panel reconciles against the server** via the existing
 *      `loadConsultSession`. Server truth wins for completed turns; the local
 *      pending turn survives only while the server has not recorded its answer
 *      (see `reconcileWithServer`).
 *   3. **A pending turn orphaned by a RELOAD is polled back.** Its `fetch` died
 *      with the old page, so nothing will ever resolve it locally — the panel
 *      re-reads the session until the answer appears, then stops waiting and
 *      says so rather than showing "Consulting…" forever.
 *
 * Step 2 and 3 rest on the registry finishing a turn whose client has gone
 * away. That is proven, not assumed — see
 * `tests/data_pkg/test_consult_disconnect_persistence.py`.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { PanelChrome } from '@/components/PanelChrome'
import {
  apiPostWithStatus,
  ApiError,
  getConsultRun,
  listConsultSessions,
  loadConsultSession,
  loadConsultModel,
  saveConsultModel,
  stopConsultRun,
  synthesizeConsultRun,
  CONSULT_MODEL_OPTIONS,
  type ConsultModel,
  type ConsultRunStatus,
  type ConsultSessionSummary,
  type ConsultSynthesisBody,
  type ConsultUsage,
} from '@/lib/api'
import { RecordLink } from '@/components/inspector/RecordLink'
import { Pin, X } from 'lucide-react'
import { selectionKindOf, useSelection } from '@/state/selection'
import { useScope } from '@/state/scope'
import {
  isScopePin,
  pinnedContext,
  questionWithPins,
  scopePin,
} from '@/lib/consultContext'
import {
  consultActions,
  isDetached,
  useConsultPanel,
  CONSULT_PAGE_LOAD_ID,
  type ChatTurn,
  type ConsultCitedRef,
  type ConsultToolCall,
  type StepFrame,
} from '@/state/consultSession'
import type { ProvenanceCensus } from '@/state/consultSession'
import type { PanelProps } from '@/types'

interface ConsultResponse {
  answer: string
  finding_id: string | null
  derived_from: string[]
  tool_calls: ConsultToolCall[]
  cited_refs: ConsultCitedRef[]
  receipt_hash?: string | null
  uncertainty?: number | null
  unanswered_aspects?: string[]
  session_id?: string | null
  // F1: which LLM plane answered ("opus"/"core"), echoed by the server.
  model?: string | null
  /**
   * `'accepted'` (with HTTP 202) when the run outlived the request and `answer`
   * is therefore empty — the answer arrives on the step stream's terminal
   * frame instead. `'complete'` is the fast path, byte-identical to the
   * response shape that existed before the run was detached.
   */
  status?: 'complete' | 'accepted'
  request_id?: string | null
  /**
   * Did the FINAL SYNTHESIS finish? Orthogonal to `status`, which is about the
   * request. `'partial'`/`'none'` is the 2026-09-16 shape — an expensive run
   * whose answer was cut — and both are recoverable from persisted evidence.
   */
  synthesis_status?: 'complete' | 'partial' | 'none'
  resynthesizable?: boolean
  usage?: ConsultUsage | null
  /** Set only by the synthesize endpoint — see {@link ConsultSynthesisBody}. */
  replay_fidelity?: 'exact' | 'rebuilt' | 'unavailable'
  replay_note?: string | null
  /** 7g-2 — what this answer rests on, counted server-side. */
  provenance_census?: ProvenanceCensus | null
}

/**
 * The census line beside the answer header — the count behind "mostly model
 * knowledge". Renders ONLY what the server measured: a class whose count the
 * server could not measure is `null` and is omitted, never printed as 0, and
 * a census with nothing measurable at all renders nothing.
 *
 * REFS and SENTENCES are different units and the line says so on each, so the
 * two halves can never be read as one total.
 */
function ProvenanceCensusLine({ census }: { census: ProvenanceCensus }) {
  const classes: Array<[string, number | null | undefined]> = [
    ['live', census.live],
    ['history', census.history],
    ['web', census.web_retrieval],
    ['seed', census.seed],
  ]
  const shown = classes.filter(([, n]) => typeof n === 'number')
  const mk = census.model_knowledge
  if (!shown.length && typeof mk !== 'number') return null
  return (
    <span
      className="font-mono text-[10px] text-slate-500"
      data-testid="consult-provenance-census"
      title={census.basis ?? undefined}
    >
      {shown.length > 0 ? (
        <>cited: {shown.map(([label, n]) => `${n} ${label}`).join(' · ')}</>
      ) : (
        <>cited: not measured</>
      )}
      {typeof mk === 'number' && (
        <>
          {' '}
          · model knowledge: {mk} sentence{mk === 1 ? '' : 's'}
        </>
      )}
    </span>
  )
}

/**
 * The synthesize endpoint's body, widened to the shape the settle path takes.
 *
 * The fields the settle path treats as required are optional on the wire, and
 * defaulting them HERE (once) keeps every downstream reader from having to
 * decide what an absent `cited_refs` means.
 */
function asConsultResponse(body: ConsultSynthesisBody): ConsultResponse {
  return {
    ...body,
    answer: body.answer ?? '',
    finding_id: body.finding_id ?? null,
    derived_from: body.derived_from ?? [],
    tool_calls: (body.tool_calls as ConsultToolCall[]) ?? [],
    cited_refs: (body.cited_refs as ConsultCitedRef[]) ?? [],
  }
}

const CONSULT_PATH = '/consult'
const MAX_ROUNDS = 30

/**
 * The registry's terminal frame — the one that carries the ANSWER.
 *
 * The actor publishes its own `final` frame before its method returns, but it
 * carries only `output_id`/`mode`; the answer used to come back on the POST
 * response, which is precisely what the 2026-09-16 504 destroyed. The registry
 * now publishes a second terminal frame once it has persisted the turn, tagged
 * `final_source: 'registry'`, and that is what settles a turn here.
 *
 * On a failure it carries `partial_answer` + `steps` instead of a response:
 * the panel renders those, because a turn that says what broke and what it had
 * gathered is worth more than an error code.
 */
interface TerminalFrame {
  type: 'final'
  final_source?: string
  status?: 'complete' | 'error'
  session_id?: string | null
  response?: ConsultResponse
  error_status?: number
  error_detail?: unknown
  partial_answer?: string
  steps?: StepFrame[]
}

/**
 * How often a turn with no live stream re-reads the server, and how long it
 * keeps looking.
 *
 * The deadline is the analyst's total wall-clock budget
 * (`LEGBA_CONSULT_BUDGET_SECONDS`, 480s) plus slack for the detached run's own
 * delivery — past that the loop has itself emitted a degraded final, so an
 * answer is no longer coming and continuing to poll would misrepresent the
 * turn as live.
 */
const REATTACH_POLL_MS = 3000
const REATTACH_DEADLINE_MS = 540_000

function formatApiError(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body
    if (typeof body === 'string') return `consult ${err.status}: ${body}`
    if (body && typeof body === 'object' && 'detail' in body) {
      const detail = (body as { detail: unknown }).detail
      if (typeof detail === 'string') return `consult ${err.status}: ${detail}`
      return `consult ${err.status}: ${JSON.stringify(detail)}`
    }
    return `consult ${err.status}: ${err.message}`
  }
  if (err instanceof Error) return err.message
  return String(err)
}

/**
 * Why a step that isn't a plain tool call happened, in the operator's words.
 *
 * A round the planner wasted, or a budget the loop hit, is the most useful
 * thing in a trace and used to render as a bare kind name — `unparseable` with
 * no hint of what the model actually said. These are the steps someone reads
 * when a consult disappoints them, so they carry their reason.
 */
function stepReason(s: StepFrame): string | null {
  const kind = String(s.kind ?? '')
  if (kind === 'unparseable') {
    const raw = typeof s.raw === 'string' ? s.raw.replace(/\s+/g, ' ').trim() : ''
    return raw
      ? `the planner's reply matched neither reply shape — "${raw.slice(0, 120)}${raw.length > 120 ? '…' : ''}"`
      : "the planner's reply matched neither reply shape"
  }
  if (kind === 'missing_tool_or_final') {
    return 'the planner emitted JSON with neither a tool call nor an answer'
  }
  if (kind === 'native_text_tool_fallback') {
    return 'the planner answered in the old JSON protocol; ran it as a tool round'
  }
  if (kind === 'wall_budget_reached') {
    return `stopped drilling after ${s.elapsed_s ?? '?'}s — synthesising from what it has`
  }
  if (kind === 'total_budget_reserve_reached') {
    return `time budget nearly spent (${s.remaining_s ?? '?'}s left) — synthesising now`
  }
  if (kind === 'round_deadline_exceeded') {
    return `this round passed its ${s.deadline_s ?? '?'}s deadline`
  }
  if (kind === 'degraded_final') {
    return `answered from partial evidence: ${String(s.reason ?? 'out of budget')}`
  }
  if (kind === 'partial_final') {
    // The answer was CUT, and what there is of it is being delivered. Say how
    // much survived — the difference between "487 chars" and "4,800 chars" is
    // the difference between re-running and reading.
    return `synthesis cut at ${s.chars ?? '?'} chars (${String(
      s.reason ?? 'deadline',
    )}) — delivering the partial`
  }
  if (kind === 'spend_ceiling_reached') {
    return `stopped drilling to stay under the spend ceiling: ${String(
      s.reason ?? 'ceiling reached',
    )}`
  }
  if (kind === 'compaction') {
    return `compacted ${s.bodies_compacted ?? '?'} tool bodies (${
      typeof s.chars_saved === 'number' ? s.chars_saved.toLocaleString() : '?'
    } chars saved)`
  }
  if (kind === 'llm_error' || kind === 'forced_final_error') {
    return 'the model call failed'
  }
  return null
}

/**
 * The live spend readout: what this run has burned, against what it may burn.
 *
 * Both halves always carry their ceiling when there is one. A token count on
 * its own is trivia; `78,400 / 150,000` is a decision. The INPUT tokens are the
 * numerator because `max_input_tokens` is the cap that actually bites — output
 * and call count ride the tooltip rather than muddying the line.
 */
function formatUsage(u: ConsultUsage): string {
  const tokens =
    u.max_input_tokens > 0
      ? `${u.input_tokens.toLocaleString()} / ${u.max_input_tokens.toLocaleString()} tok`
      : `${u.input_tokens.toLocaleString()} tok`
  const cost =
    u.max_cost_usd > 0
      ? `$${u.est_cost_usd.toFixed(2)} / $${u.max_cost_usd.toFixed(2)}`
      : `$${u.est_cost_usd.toFixed(2)}`
  return `${tokens} · ${cost}`
}

/** How far into the tighter of the two ceilings this run already is (0–1+). */
function usageFraction(u: ConsultUsage): number {
  const byTokens = u.max_input_tokens > 0 ? u.input_tokens / u.max_input_tokens : 0
  const byCost = u.max_cost_usd > 0 ? u.est_cost_usd / u.max_cost_usd : 0
  return Math.max(byTokens, byCost)
}

/**
 * Escalate the meter as it closes on a ceiling — the same rose/amber ramp the
 * Budget panel uses, so "this is about to cost you" reads the same everywhere.
 */
function usageTone(u: ConsultUsage): string {
  const pct = usageFraction(u)
  if (pct >= 0.9) return 'text-rose-400'
  if (pct >= 0.75) return 'text-amber-400'
  return 'text-slate-400'
}

/**
 * A cap the operator did not choose is the one worth shouting about.
 *
 * The incident's root cause was exactly this: the run executed under a default
 * of 10 rounds while nobody was looking at the number. `'request'` is the quiet
 * case; a silently substituted default is not.
 */
function roundCapTone(source?: string | null): string {
  if (source === 'default_malformed_request') return 'text-rose-400'
  if (source === 'default') return 'text-amber-400'
  return 'text-slate-400'
}

function roundCapTitle(cap: number, source?: string | null): string {
  const base = `this run drills at most ${cap} tool round(s)`
  if (source === 'default_malformed_request') {
    return `${base} — the requested cap was MALFORMED and the server's default was used instead`
  }
  if (source === 'default') return `${base} — the server's default; the request did not set one`
  if (source === 'request') return `${base} — as requested by this panel`
  return base
}

/** Steps that mean "the answer above is a stump, not an answer". */
const CUT_SYNTHESIS_KINDS = new Set(['degraded_final', 'partial_final'])

/**
 * Can this settled turn be re-synthesised from the evidence behind it?
 *
 * The server says so directly (`synthesis_status`), but turns persisted before
 * that field existed — including every turn in the conversation the incident
 * happened in — can only be recognised by the step that cut them off. Both are
 * honoured, and an explicit `resynthesizable: false` (the evidence has aged
 * out) overrides both: offering a button that can only 404 is worse than
 * offering nothing.
 */
function needsResynthesis(turn: ChatTurn): boolean {
  if (turn.role !== 'assistant') return false
  // Neither address to POST to — nothing to recover from. A turn re-seeded
  // from the session API has no RUN id but does have its own — see
  // `turnsFromServer`.
  if (!turn.requestId && !turn.turnId) return false
  if (turn.resynthesizable === false) return false
  if (turn.synthesisStatus === 'partial' || turn.synthesisStatus === 'none') return true
  if (turn.synthesisStatus === 'complete') return false
  return (turn.steps ?? []).some((s) => CUT_SYNTHESIS_KINDS.has(String(s.kind ?? '')))
}

/** Compact one-line label for a streamed step. */
function stepLabel(s: StepFrame): string {
  const parts: string[] = []
  if (s.phase) parts.push(String(s.phase))
  if (s.kind) parts.push(String(s.kind))
  if (s.tool) parts.push(`tool=${String(s.tool)}`)
  if (typeof s.round === 'number') parts.push(`round=${s.round}`)
  const base = parts.join(' · ') || 'step'
  const reason = stepReason(s)
  return reason ? `${base} — ${reason}` : base
}

export default function ConsultPanel({ registration }: PanelProps) {
  // The store key. `singleton:system.consult` for the ordinary panel — stable
  // across an `api.clear()`, so a re-opened panel rejoins its own conversation.
  const panelId = registration.id
  const panel = useConsultPanel(panelId)
  const { sessionId, transcript, pins, pendingTurn, draft, scope, maxRounds, error } = panel

  // F1 model picker — the LLM plane this chat runs on; persisted across opens
  // under its own key (it is an operator preference, not conversation state).
  const [model, setModel] = useState<ConsultModel>(() => loadConsultModel())
  // History sidebar: prior chat sessions. Deliberately component-local — view
  // chrome SHOULD reset with the panel; only the conversation is durable.
  const [sessions, setSessions] = useState<ConsultSessionSummary[]>([])
  const [historyOpen, setHistoryOpen] = useState(false)
  const [historyError, setHistoryError] = useState<string | null>(null)
  // The two salvage operations, each with its own in-flight state so the button
  // that is working says so and nothing else in the panel locks up.
  const [stopPhase, setStopPhase] = useState<'stopping' | 'synthesizing' | null>(null)
  const [resynthIndex, setResynthIndex] = useState<number | null>(null)

  const esRef = useRef<EventSource | null>(null)
  // Auto-scroll the conversation to the newest turn / live step.
  const scrollRef = useRef<HTMLDivElement | null>(null)

  // A turn whose POST this page load no longer owns (the reload case) — it can
  // only be recovered from the server, so it is polled rather than awaited.
  const detached = isDetached(pendingTurn)
  // A stalled turn must not lock the composer: the operator gets the panel back.
  const busy = !!pendingTurn && !pendingTurn.stalled

  // Pin-to-context (#90): the operator pins records from the shared selection
  // into a sticky set; every pin is injected into each turn's context (see
  // `send`). Pins live in the store, so they now survive a preset pick too.
  //
  // MANUAL pinning still pins FOCUS — deliberately. The operator holds three
  // findings pinned under one report scope; that is the v2 working posture.
  const selection = useSelection((s) => s.selection)
  const pinSelection = () => {
    if (!selection) return
    consultActions().addPin(panelId, selection)
  }
  const selectionPinned =
    !!selection && pins.some((p) => p.kind === selection.kind && p.id === selection.id)

  // AUTO-PIN THE SCOPE (design §5.2). One ambient pin, tagged
  // `origin:'scope'`, REPLACED on every scope change — never appended, or a
  // morning's navigation would leave a stack of stale scopes poisoning every
  // later turn. Clearing the scope removes it and pins nothing in its place.
  const wallScope = useScope((s) => s.scope)
  useEffect(() => {
    const store = consultActions()
    const current = store.panel(panelId).pins
    const existing = current.find(isScopePin)
    if (existing && (!wallScope || existing.id !== wallScope.id)) {
      store.removePin(panelId, existing.kind, existing.id)
    }
    if (wallScope && (!existing || existing.id !== wallScope.id)) {
      store.addPin(panelId, scopePin(wallScope))
    }
  }, [panelId, wallScope])

  const closeStream = useCallback(() => {
    if (esRef.current) {
      esRef.current.close()
      esRef.current = null
    }
  }, [])

  // The EventSource is the one piece of this panel that CANNOT outlive the
  // component — an unmounted panel has nothing to render steps into, and a
  // leaked SSE connection would hold a registry worker and keep relaying into
  // the void. Closing it does not touch the turn: the POST is still running and
  // the steps gathered so far stay on the pending turn in the store.
  useEffect(() => closeStream, [closeStream])

  /**
   * Land a turn from whatever the server finally said about it.
   *
   * ONE settlement path for four arrivals — the POST's 200, the stream's
   * terminal frame, the run-status poll, and a reconnect — because they carry
   * the same body and a turn that settled differently depending on which got
   * there first is a turn nobody can reason about.
   *
   * A failed run settles too, with its PARTIAL: what it gathered, which round
   * it died on, and why. The panel must never go blank and must never show a
   * bare status code, so the error also lands in the banner *next to* a
   * readable turn rather than in place of one.
   */
  const settleTurn = useCallback(
    (
      requestId: string,
      mode: 'chat' | 'deep',
      outcome: {
        response?: ConsultResponse | null
        partialAnswer?: string | null
        steps?: StepFrame[]
        error?: string | null
      },
    ) => {
      const store = consultActions()
      const live = store.panel(panelId).pendingTurn
      if (!live || live.requestId !== requestId) return
      const steps = outcome.steps?.length ? outcome.steps : live.steps
      const resp = outcome.response
      if (resp) {
        if (resp.session_id) store.setSessionId(panelId, resp.session_id)
        store.completeTurn(panelId, requestId, {
          role: 'assistant',
          content: resp.answer,
          steps,
          toolCalls: resp.tool_calls,
          citedRefs: resp.cited_refs,
          uncertainty: resp.uncertainty,
          unansweredAspects: resp.unanswered_aspects,
          findingId: resp.finding_id,
          deep: mode === 'deep',
          model: resp.model ?? model,
          // Everything the recovery path needs, kept ON the settled turn: the
          // run to address, whether its synthesis actually finished, and (for a
          // salvaged answer) how faithful the transcript behind it was.
          requestId,
          synthesisStatus: resp.synthesis_status ?? null,
          resynthesizable: resp.resynthesizable,
          replayFidelity: resp.replay_fidelity ?? null,
          replayNote: resp.replay_note ?? null,
          // 7g-2 — the census rides the SETTLED turn, because it describes
          // the answer and is read long after the run's spend line is gone.
          provenanceCensus: resp.provenance_census ?? null,
        })
        return
      }
      // No answer — render the partial rather than dropping the turn.
      store.completeTurn(panelId, requestId, {
        role: 'assistant',
        content:
          outcome.partialAnswer ||
          '_This consult did not finish, and the server recorded no partial._',
        steps,
        deep: mode === 'deep',
        model,
        requestId,
        // Nothing was synthesised at all — which is precisely the turn that
        // "Synthesize from evidence" exists to rescue.
        synthesisStatus: 'none',
      })
      if (outcome.error) store.setError(panelId, outcome.error)
    },
    [model, panelId],
  )

  /** Settle from the registry's terminal SSE frame. */
  const settleFromTerminal = useCallback(
    (requestId: string, mode: 'chat' | 'deep', frame: TerminalFrame) => {
      if (frame.status === 'error') {
        const detail =
          typeof frame.error_detail === 'string'
            ? frame.error_detail
            : JSON.stringify(frame.error_detail ?? 'the run failed')
        settleTurn(requestId, mode, {
          partialAnswer: frame.partial_answer,
          steps: frame.steps,
          error: `consult ${frame.error_status ?? 502}: ${detail}`,
        })
        return
      }
      settleTurn(requestId, mode, { response: frame.response })
    },
    [settleTurn],
  )

  /** Settle from a run-status read (reconnect, or a dead stream). */
  const settleFromRunStatus = useCallback(
    (requestId: string, mode: 'chat' | 'deep', run: ConsultRunStatus) => {
      const steps = (run.steps ?? []) as StepFrame[]
      if (run.status === 'complete') {
        settleTurn(requestId, mode, {
          response: run.response as ConsultResponse | null,
          steps,
        })
        return
      }
      const detail =
        typeof run.error_detail === 'string'
          ? run.error_detail
          : JSON.stringify(run.error_detail ?? 'the run failed')
      settleTurn(requestId, mode, {
        steps,
        partialAnswer: null,
        error: `consult ${run.error_status ?? 502}: ${detail}`,
      })
    },
    [settleTurn],
  )

  /**
   * Open the step stream for `requestId` and render everything it sends.
   *
   * Extracted from `send` because a turn can need a stream twice: once when it
   * starts, and again after a reload that left the run going. Both want
   * identical behaviour, and a second copy of this would be a second place for
   * the terminal-frame handling to drift.
   */
  const openStream = useCallback(
    (requestId: string, mode: 'chat' | 'deep') => {
      closeStream()
      try {
        const token = localStorage.getItem('legba_token') ?? ''
        const es = new EventSource(
          `/api/v1${CONSULT_PATH}/stream/${requestId}?token=${encodeURIComponent(token)}`,
        )
        esRef.current = es
        es.onmessage = (e: MessageEvent) => {
          let frame: (StepFrame & TerminalFrame) | null = null
          try {
            frame = JSON.parse(e.data) as StepFrame & TerminalFrame
          } catch {
            return
          }
          if (!frame) return
          if (frame.type === 'final') {
            if (frame.final_source === 'registry') {
              // The answer-bearing frame: this settles the turn and ends the
              // stream.
              settleFromTerminal(requestId, mode, frame)
              closeStream()
              return
            }
            // The ACTOR's bare final: the loop is done and the registry is
            // persisting. Keep the stream open and keep the operator informed
            // — this is the gap where the panel used to look frozen.
            consultActions().pushStep(panelId, requestId, {
              type: 'step',
              phase: 'reflect',
              kind: 'awaiting_answer',
            })
            return
          }
          if (frame.type === 'step') {
            // Addressed by request id, so a frame arriving after the turn
            // settled (or belonging to a superseded turn) is dropped, not
            // misfiled.
            consultActions().pushStep(panelId, requestId, frame)
          }
        }
        es.onerror = () => {
          // The live ticker is best-effort, but the ANSWER is not: a dead
          // stream falls back to the run-status poll below rather than
          // stranding the turn.
          closeStream()
        }
      } catch {
        // EventSource unsupported / blocked — degrade to no live steps. The
        // poll still lands the answer.
        esRef.current = null
      }
    },
    [closeStream, panelId, settleFromTerminal],
  )

  // Keep the conversation pinned to the bottom as turns / steps arrive.
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [transcript, pendingTurn])

  // ---------------------------------------------------------------------
  // Reconcile on mount — server truth for completed turns, local pending turn
  // only while the server has no answer for it.
  // ---------------------------------------------------------------------
  useEffect(() => {
    const store = consultActions()
    const slice = store.panel(panelId)
    if (!slice.sessionId) return
    const rev = slice.rev
    let cancelled = false
    void loadConsultSession(slice.sessionId)
      .then((detail) => {
        if (!cancelled) consultActions().reconcile(panelId, detail, rev)
      })
      .catch(() => {
        // Best-effort: an unreachable registry leaves the local slice exactly
        // as it was. Nothing is dropped on a failed reconcile.
      })
    return () => {
      cancelled = true
    }
  }, [panelId])

  // ---------------------------------------------------------------------
  // Reattach poll — a turn orphaned by a reload has no promise to resolve it.
  // ---------------------------------------------------------------------
  // Identify the pending turn by VALUE, not by object identity. The effect
  // below writes to the store (it seeds the steps a reload missed), which
  // replaces `pendingTurn` — so depending on the object itself would re-run the
  // effect on its own write and spin. These are stable across that write.
  const pendingRequestId = pendingTurn?.requestId ?? null
  const pendingStartedAt = pendingTurn?.startedAt ?? 0
  const pendingMode = pendingTurn?.mode ?? 'chat'
  const pendingStalled = pendingTurn?.stalled ?? false

  useEffect(() => {
    if (!pendingRequestId || pendingStalled || !detached) return
    const requestId = pendingRequestId
    const startedAt = pendingStartedAt
    const mode = pendingMode

    if (Date.now() - startedAt > REATTACH_DEADLINE_MS) {
      consultActions().markStalled(panelId, requestId)
      return
    }

    let cancelled = false
    let streamOpened = false
    // The run replays its whole accumulated trace on every read, so seeding is
    // a one-shot: after this the live stream is the source of new steps.
    let seeded = false

    /**
     * One poll tick: ask the RUN first, then the session.
     *
     * The run knows three things the session does not — whether it is still
     * alive, the steps it has taken so far, and (on a failure) why it stopped.
     * So a reload mid-run does not just wait: it re-attaches to the live
     * stream and seeds the steps it missed, and the operator sees the run
     * continue rather than a frozen "Consulting…".
     *
     * Falling back to the session matters just as much: once the run ages out
     * of the registry's memory, the persisted turn is the record, and it is
     * the only thing that survives a registry restart.
     */
    const tick = async () => {
      if (cancelled) return
      if (Date.now() - startedAt > REATTACH_DEADLINE_MS) {
        consultActions().markStalled(panelId, requestId)
        return
      }
      try {
        const run = await getConsultRun(requestId)
        if (cancelled) return
        if (run.status === 'running') {
          // Live. Seed whatever steps we missed ONCE, then re-attach so the
          // rest arrive as they happen.
          if (!seeded) {
            seeded = true
            for (const step of (run.steps ?? []) as StepFrame[]) {
              consultActions().pushStep(panelId, requestId, {
                ...step,
                type: 'step',
              })
            }
          }
          if (!streamOpened) {
            streamOpened = true
            openStream(requestId, mode)
          }
          return
        }
        if (run.status === 'complete' || run.status === 'error') {
          settleFromRunStatus(requestId, mode, run)
          return
        }
        // A 200 carrying something that is not a run (a proxy's error page, a
        // stub, a future shape we don't know) tells us NOTHING — and settling a
        // turn on it would invent an outcome. Fall through to the durable
        // record instead.
      } catch {
        // 404 (aged out / another registry worker) or unreachable — the
        // durable record is the session's turns.
      }
      if (!sessionId) {
        // The reload beat the FIRST response back: the server minted the
        // session, we never learned its id, and `consult_sessions` carries no
        // `request_id` to find it by. Not automatically recoverable — say so
        // instead of spinning. The run is not lost: it is in the History
        // sidebar under its own question.
        consultActions().markStalled(panelId, requestId)
        return
      }
      const rev = consultActions().panel(panelId).rev
      try {
        const detail = await loadConsultSession(sessionId)
        if (!cancelled) consultActions().reconcile(panelId, detail, rev)
      } catch {
        // A poll that can't reach the registry just tries again next tick.
      }
    }

    void tick()
    const timer = window.setInterval(() => void tick(), REATTACH_POLL_MS)

    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [
    panelId,
    pendingRequestId,
    pendingStartedAt,
    pendingMode,
    pendingStalled,
    detached,
    sessionId,
    openStream,
    settleFromRunStatus,
  ])


  /**
   * Last resort for a turn whose stream and POST both failed us.
   *
   * Asks the run-status endpoint first (it has the accumulated steps), then
   * the session's persisted turns. Returns whether the turn was settled —
   * `false` means there is genuinely nothing on the server yet and the caller
   * should show the error it has.
   */
  const recoverTurn = useCallback(
    async (requestId: string, mode: 'chat' | 'deep'): Promise<boolean> => {
      try {
        const run = await getConsultRun(requestId)
        if (run.status !== 'running') {
          settleFromRunStatus(requestId, mode, run)
          return true
        }
        return false
      } catch {
        // 404 or unreachable — fall through to the durable record.
      }
      const slice = consultActions().panel(panelId)
      if (!slice.sessionId) return false
      try {
        const detail = await loadConsultSession(slice.sessionId)
        consultActions().reconcile(panelId, detail, slice.rev)
        return !consultActions().panel(panelId).pendingTurn
      } catch {
        return false
      }
    },
    [panelId, settleFromRunStatus],
  )

  const send = async (mode: 'chat' | 'deep') => {
    const trimmed = draft.trim()
    if (!trimmed || busy) return

    const store = consultActions()
    // Snapshot the transcript-so-far for the server (prior turns) BEFORE the
    // store optimistically appends the new user turn.
    const priorMessages = transcript.map((t) => ({ role: t.role, content: t.content }))
    const requestId = crypto.randomUUID()

    // Stamp the turn with THIS page load: while the stamp matches, the reattach
    // poll leaves the turn alone, because the promise below will answer it.
    store.startTurn(panelId, {
      requestId,
      question: trimmed,
      mode,
      startedAt: Date.now(),
      pageLoadId: CONSULT_PAGE_LOAD_ID,
      steps: [],
    })

    // Subscribe to the step stream BEFORE POSTing (subscribe-before-publish).
    openStream(requestId, mode)

    // Inject pinned records two ways: a `[Pinned …]` context prefix on the
    // question (works against today's text-only backend) AND a structured
    // `pinned_context` field a backend can hydrate full record bodies from.
    // Both are built in `lib/consultContext` so Chat and Deep Consult cannot
    // drift apart on what the model is told.
    const question = questionWithPins(trimmed, pins)

    try {
      const { status, body: resp } = await apiPostWithStatus<ConsultResponse>(
        CONSULT_PATH,
        {
          question,
          scope_predicate: scope.trim() || null,
          max_tool_rounds: maxRounds,
          mode,
          model,
          request_id: requestId,
          messages: priorMessages,
          session_id: sessionId,
          pinned_context: pinnedContext(pins),
        },
      )
      // Thread the audit-trail session — the server opens one on the first turn
      // and echoes its id; pass it back on the next turn so the conversation
      // stays under one session.
      if (resp.session_id) consultActions().setSessionId(panelId, resp.session_id)

      // 202 — the run outlived the request. This is INVISIBLE to the operator:
      // the stream opened before the POST and is still rendering rounds, and
      // the answer arrives on its terminal frame. Leaving the stream open and
      // the turn pending is the whole behaviour.
      if (status === 202 || resp.status === 'accepted') {
        return
      }

      // 200 — the fast path, unchanged. Settle straight from the body.
      settleTurn(requestId, mode, { response: resp })
      closeStream()
    } catch (err) {
      // The POST failed outright (the request never became a run, or the run
      // failed inside the sync window). The server has still persisted a
      // partial turn, so pull it back rather than showing only a status line.
      consultActions().setError(panelId, formatApiError(err))
      const recovered = await recoverTurn(requestId, mode)
      if (!recovered) {
        consultActions().failTurn(panelId, requestId, formatApiError(err))
      }
      closeStream()
    }
  }


  /**
   * STOP — the escape hatch the 2026-09-16 run did not have.
   *
   * Two POSTs, in this order and for different reasons: `stop` ends the
   * spending, `synthesize` turns what has already been bought into an answer.
   * A failed stop does NOT abort the salvage — a run that finished on its own
   * (404) has the same persisted evidence as one we cancelled, and the operator
   * pressed this button to get an answer, not to get a status code.
   *
   * Deliberately NOT gated on `busy`: every other control locks while a turn is
   * in flight, and a brake that only works when the car is stopped is not a
   * brake.
   */
  const stopAndSynthesize = async () => {
    const pending = consultActions().panel(panelId).pendingTurn
    if (!pending || stopPhase) return
    const { requestId, mode } = pending
    let stopNote: string | null = null
    setStopPhase('stopping')
    try {
      try {
        await stopConsultRun(requestId)
      } catch (err) {
        stopNote = formatApiError(err)
      }
      setStopPhase('synthesizing')
      const body = await synthesizeConsultRun({ requestId }, model)
      closeStream()
      // The ordinary settle path — a salvaged answer is an answer, and a turn
      // that landed differently depending on how it ended would be a turn
      // nobody can reason about.
      settleTurn(requestId, mode, { response: asConsultResponse(body) })
    } catch (err) {
      // The turn stays PENDING on purpose: the run may yet deliver on its own,
      // and failing it here would discard evidence the operator just paid for.
      const detail = formatApiError(err)
      consultActions().setError(
        panelId,
        stopNote ? `${detail} (stop also failed: ${stopNote})` : detail,
      )
    } finally {
      setStopPhase(null)
    }
  }

  /**
   * Re-synthesise a SETTLED turn whose answer was cut short.
   *
   * Replaces the turn in place rather than appending: the stump and its repair
   * are one answer, and appending would also feed the truncated text back to
   * the model as conversation history on the next turn.
   */
  const resynthesizeTurn = async (index: number, turn: ChatTurn) => {
    if ((!turn.requestId && !turn.turnId) || resynthIndex !== null) return
    setResynthIndex(index)
    try {
      const body = await synthesizeConsultRun(
        { requestId: turn.requestId, turnId: turn.turnId },
        model,
      )
      const toolCalls = (body.tool_calls as ConsultToolCall[] | undefined) ?? []
      const citedRefs = (body.cited_refs as ConsultCitedRef[] | undefined) ?? []
      consultActions().replaceTurn(panelId, index, {
        ...turn,
        content: body.answer || turn.content,
        // The synthesis re-read the evidence; it did not re-drill. The original
        // trace is the record of how that evidence was gathered, so it stays.
        steps: turn.steps,
        toolCalls: toolCalls.length ? toolCalls : turn.toolCalls,
        citedRefs: citedRefs.length ? citedRefs : turn.citedRefs,
        uncertainty: body.uncertainty ?? turn.uncertainty,
        unansweredAspects: body.unanswered_aspects ?? turn.unansweredAspects,
        model: body.model ?? turn.model,
        // The recovery answer is written into its OWN turn server-side; keep
        // its id so a second recovery addresses the answer now on screen.
        turnId: body.turn_id ?? turn.turnId,
        synthesisStatus: body.synthesis_status ?? 'complete',
        resynthesizable: body.resynthesizable,
        replayFidelity: body.replay_fidelity ?? null,
        replayNote: body.replay_note ?? null,
      })
    } catch (err) {
      consultActions().setError(panelId, formatApiError(err))
    } finally {
      setResynthIndex(null)
    }
  }

  const resetChat = () => {
    closeStream()
    consultActions().reset(panelId)
  }

  // Load the prior-session list for the history sidebar.
  const loadHistory = useCallback(async () => {
    setHistoryError(null)
    try {
      const rows = await listConsultSessions({ mode: 'chat', limit: 50 })
      setSessions(rows)
    } catch (err) {
      setHistoryError(formatApiError(err))
    }
  }, [])

  // Open a prior session: re-seed the transcript from its persisted turns and
  // adopt its id so the next turn CONTINUES the conversation server-side.
  const openSession = async (id: string) => {
    if (busy) return
    closeStream()
    try {
      const detail = await loadConsultSession(id)
      consultActions().adoptSession(panelId, detail)
      setHistoryOpen(false)
    } catch (err) {
      consultActions().setError(panelId, formatApiError(err))
    }
  }

  // Refresh the history list when the sidebar opens.
  useEffect(() => {
    if (historyOpen) void loadHistory()
  }, [historyOpen, loadHistory])

  /** What the in-flight block says about itself — the three states differ. */
  const pendingLabel = useMemo(() => {
    if (!pendingTurn) return ''
    if (pendingTurn.stalled) {
      return sessionId
        ? 'Stopped waiting — the server never recorded an answer for this turn.'
        : 'Stopped waiting — this turn was interrupted before it was threaded to a session. Check History.'
    }
    if (detached) {
      // A reattached turn is no longer merely "waiting": the panel now seeds
      // the steps the reload missed and re-opens the live stream, so say what
      // is actually happening. Claiming to wait while rounds visibly arrive
      // below it is the kind of small lie that makes a panel feel broken.
      return pendingTurn.steps.length > 0
        ? `Reattached after a reload — thinking… (${pendingTurn.steps.length} steps)`
        : 'Reattached after a reload — waiting for the server…'
    }
    return `Thinking… (${pendingTurn.steps.length} steps)`
  }, [pendingTurn, detached, sessionId])

  return (
    <PanelChrome
      registration={registration}
      subtitle="consult_on_demand (chat · on-demand via dapr actor)"
      onRefresh={resetChat}
    >
      <div className="flex h-full min-h-0">
        {/* History sidebar — prior chat sessions (0038 audit trail). */}
        {historyOpen && (
          <div
            className="w-56 shrink-0 border-r border-slate-700 pr-2 mr-2 overflow-y-auto min-h-0"
            data-testid="consult-history"
          >
            <div className="flex items-center justify-between mb-2">
              <span className="text-[11px] uppercase tracking-wide text-slate-400">
                Prior chats
              </span>
              <button
                onClick={() => void loadHistory()}
                className="text-[10px] text-slate-400 hover:text-slate-200"
                title="refresh history"
                data-testid="consult-history-refresh"
              >
                ↻
              </button>
            </div>
            {historyError && (
              <div className="text-[10px] text-accent-critical mb-2">{historyError}</div>
            )}
            {sessions.length === 0 && !historyError && (
              <div className="text-[11px] text-slate-500 py-2">No prior chats yet.</div>
            )}
            <ul className="space-y-1" data-testid="consult-history-list">
              {sessions.map((s) => (
                <li key={s.id}>
                  <button
                    onClick={() => void openSession(s.id)}
                    disabled={busy}
                    className={
                      'w-full text-left rounded px-2 py-1 text-xs hover:bg-surface-200 disabled:opacity-50 ' +
                      (s.id === sessionId ? 'bg-surface-200 text-slate-100' : 'text-slate-300')
                    }
                    title={s.title}
                    data-testid="consult-history-item"
                  >
                    <div className="truncate">{s.title || '(untitled)'}</div>
                    <div className="text-[10px] text-slate-500">
                      {s.turn_count} turn{s.turn_count === 1 ? '' : 's'}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="flex flex-col h-full min-h-0 flex-1">
        {/* History toggle + new-chat bar */}
        <div className="flex items-center gap-2 mb-2 shrink-0">
          <button
            onClick={() => setHistoryOpen((v) => !v)}
            className="text-[11px] text-slate-400 hover:text-slate-200 rounded px-2 py-0.5 border border-slate-700"
            data-testid="consult-history-toggle"
          >
            {historyOpen ? '◀ Hide history' : '☰ History'}
          </button>
          <button
            onClick={resetChat}
            disabled={busy}
            className="text-[11px] text-slate-400 hover:text-slate-200 rounded px-2 py-0.5 border border-slate-700 disabled:opacity-50"
            data-testid="consult-new-chat"
          >
            + New chat
          </button>
          <button
            onClick={pinSelection}
            disabled={!selection || selectionPinned}
            className="ml-auto flex items-center gap-1 text-[11px] text-slate-400 hover:text-slate-200 rounded px-2 py-0.5 border border-slate-700 disabled:opacity-50"
            title={
              selection
                ? selectionPinned
                  ? 'already pinned to context'
                  : `pin ${selection.kind} to consult context`
                : 'select a record to pin it'
            }
            data-testid="consult-pin"
          >
            <Pin className="h-3 w-3" />
            {selectionPinned ? 'Pinned' : 'Pin selection'}
          </button>
        </div>
        {/* Conversation — scrolls; the composer below stays pinned. */}
        <div
          ref={scrollRef}
          className="flex-1 overflow-y-auto min-h-0 space-y-3 pr-1"
          data-testid="consult-scroll"
        >
          {transcript.length === 0 && !pendingTurn && (
            <div className="text-label text-ink-3 py-6 text-center">
              Ask the substrate anything — answers cite the records they used.
            </div>
          )}

          {transcript.length > 0 && (
            <div className="space-y-3" data-testid="consult-transcript">
              {transcript.map((turn: ChatTurn, i: number) =>
                turn.role === 'user' ? (
                  <div key={i} className="flex justify-end" data-testid="consult-turn-user">
                    <div className="bg-accent-info/20 rounded px-3 py-2 text-sm max-w-[85%] whitespace-pre-wrap">
                      {turn.content}
                    </div>
                  </div>
                ) : (
                  <div key={i} className="space-y-2" data-testid="consult-turn-assistant">
                    <div className="text-[11px] text-slate-400 flex items-center gap-2">
                      <span>Answer</span>
                      {turn.model && (
                        <span
                          className="font-mono text-slate-500"
                          data-testid="consult-answer-model"
                          title="the LLM plane that produced this answer"
                        >
                          via {turn.model}
                        </span>
                      )}
                      {typeof turn.uncertainty === 'number' && (
                        <span className="font-mono text-slate-500">
                          uncertainty={turn.uncertainty.toFixed(2)}
                        </span>
                      )}
                      {turn.provenanceCensus && (
                        <ProvenanceCensusLine census={turn.provenanceCensus} />
                      )}
                      {turn.deep && turn.findingId && (
                        <>
                          <span className="font-mono text-emerald-400">durable finding written</span>
                          <span data-testid="consult-finding-link">
                            <RecordLink
                              kind="finding"
                              id={turn.findingId}
                              label={`finding=${turn.findingId.slice(0, 8)}`}
                              origin="consult"
                              mono
                              title="inspect the produced finding"
                            />
                          </span>
                        </>
                      )}
                    </div>
                    <div
                      className="bg-surface-200 rounded p-2 text-sm prose prose-invert prose-sm max-w-none"
                      data-testid="consult-answer"
                    >
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{turn.content}</ReactMarkdown>
                    </div>
                    {/* A rebuilt transcript is NOT the run the operator paid
                        for. Saying so is the whole point — a recovery that
                        passes itself off as a clean re-run is a lie. */}
                    {turn.replayFidelity && turn.replayFidelity !== 'exact' && (
                      <div
                        className="rounded border border-amber-500/40 bg-amber-500/10 p-2 text-[11px] text-amber-300"
                        data-testid="consult-replay-note"
                      >
                        {turn.replayNote ||
                          (turn.replayFidelity === 'unavailable'
                            ? 'the original transcript could not be recovered — this synthesis read the persisted evidence alone'
                            : 'the transcript this synthesis read was rebuilt, not the original')}
                      </div>
                    )}
                    {needsResynthesis(turn) && (
                      <div className="flex items-center gap-2 flex-wrap">
                        <button
                          onClick={() => void resynthesizeTurn(i, turn)}
                          disabled={resynthIndex !== null}
                          className="text-[11px] rounded px-2 py-0.5 border border-amber-500/40 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20 disabled:opacity-50"
                          data-testid="consult-resynthesize"
                          title="re-run ONLY the final synthesis over the evidence this run already gathered — no new drilling, no new tool calls"
                        >
                          {resynthIndex === i ? 'Synthesizing…' : 'Synthesize from evidence'}
                        </button>
                        <span className="text-[10px] text-slate-500">
                          this answer was cut short — the evidence behind it is still on the
                          server
                        </span>
                      </div>
                    )}
                    {turn.unansweredAspects && turn.unansweredAspects.length > 0 && (
                      <div>
                        <div className="text-[11px] text-slate-400 mb-1">Unanswered aspects</div>
                        <ul className="text-xs space-y-1 list-disc list-inside text-amber-300">
                          {turn.unansweredAspects.map((u, j) => (
                            <li key={j}>{u}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {turn.steps && turn.steps.length > 0 && (
                      <details className="bg-surface-200 rounded p-2">
                        <summary className="text-[11px] text-slate-400 cursor-pointer">
                          Thinking ({turn.steps.length} steps)
                        </summary>
                        <ul className="text-[10px] font-mono mt-2 space-y-0.5 text-slate-400">
                          {turn.steps.map((s, j) => (
                            <li key={j}>{stepLabel(s)}</li>
                          ))}
                        </ul>
                      </details>
                    )}
                    {turn.toolCalls && turn.toolCalls.length > 0 && (
                      <details className="bg-surface-200 rounded p-2">
                        <summary className="text-[11px] text-slate-400 cursor-pointer">
                          Tool calls ({turn.toolCalls.length})
                        </summary>
                        <pre className="text-[10px] font-mono mt-2 overflow-x-auto">
                          {JSON.stringify(turn.toolCalls, null, 2)}
                        </pre>
                      </details>
                    )}
                    {turn.citedRefs && turn.citedRefs.length > 0 && (
                      <div>
                        <div className="text-[11px] text-slate-400 mb-1">
                          Cited substrate ({turn.citedRefs.length})
                        </div>
                        <ul className="text-xs space-y-1" data-testid="consult-cited">
                          {turn.citedRefs.map((ref, j) => (
                            <li key={j} className="font-mono">
                              <span className="text-slate-500">{ref.kind}:</span>{' '}
                              <span data-testid={`consult-cited-link-${j}`}>
                                <RecordLink
                                  kind={selectionKindOf(ref.kind)}
                                  id={ref.id}
                                  origin="consult"
                                  mono
                                  title={`inspect this ${ref.kind}`}
                                />
                              </span>
                              {ref.description && (
                                <span className="text-slate-400"> — {ref.description}</span>
                              )}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                ),
              )}
            </div>
          )}

          {/* The in-flight turn — survives an unmount, so this block is drawn
              from the store and can outlive the component that started it. */}
          {pendingTurn && (
            <div
              className="bg-surface-200 rounded p-2 border border-slate-700"
              data-testid="consult-live-steps"
            >
              <div className="flex items-center gap-2 mb-1 flex-wrap">
                <div
                  className={
                    'text-[11px] ' +
                    (pendingTurn.stalled ? 'text-amber-300' : 'text-slate-400')
                  }
                  data-testid="consult-pending-label"
                >
                  {pendingLabel}
                </div>
                {/* The cap this run is ACTUALLY under. An unnoticed default of
                    10 is what turned one question into a ~$10 bill. */}
                {typeof pendingTurn.roundCap === 'number' && (
                  <span
                    className={
                      'font-mono text-[10px] ' + roundCapTone(pendingTurn.roundsSource)
                    }
                    data-testid="consult-round-cap"
                    title={roundCapTitle(pendingTurn.roundCap, pendingTurn.roundsSource)}
                  >
                    {pendingTurn.roundCap} rounds
                  </span>
                )}
                {/* Live spend, against its ceilings, escalating as it closes. */}
                {pendingTurn.usage && (
                  <span
                    className={'font-mono text-[10px] ' + usageTone(pendingTurn.usage)}
                    data-testid="consult-usage"
                    title={`${pendingTurn.usage.calls} model call(s) · ${pendingTurn.usage.input_tokens.toLocaleString()} in / ${pendingTurn.usage.output_tokens.toLocaleString()} out tokens · estimated $${pendingTurn.usage.est_cost_usd.toFixed(
                      2,
                    )} so far`}
                  >
                    {formatUsage(pendingTurn.usage)}
                  </span>
                )}
                {pendingTurn.stalled && (
                  <button
                    onClick={() => consultActions().dismissPending(panelId)}
                    className="ml-auto text-[10px] text-slate-400 hover:text-slate-200 underline underline-offset-2"
                    data-testid="consult-pending-dismiss"
                  >
                    Dismiss
                  </button>
                )}
              </div>
              {pendingTurn.steps.length > 0 && (
                <ul className="text-[10px] font-mono space-y-0.5 text-slate-400 max-h-40 overflow-y-auto">
                  {pendingTurn.steps.map((s, i) => (
                    <li key={i}>{stepLabel(s)}</li>
                  ))}
                </ul>
              )}
              {/* The answer as it is written. This text used to be thrown away
                  when a synthesis ran out of budget; now the operator has read
                  most of it before the server decides whether it finished. It
                  is replaced by the settled answer the moment the turn lands. */}
              {pendingTurn.answerPreview && (
                <div className="mt-2">
                  <div className="text-[10px] uppercase tracking-wide text-slate-500 mb-1">
                    Answer forming
                  </div>
                  <div
                    className="bg-surface-200 rounded p-2 text-sm prose prose-invert prose-sm max-w-none"
                    data-testid="consult-answer-live"
                  >
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {pendingTurn.answerPreview}
                    </ReactMarkdown>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Composer — pinned chat bar at the bottom */}
        <div className="shrink-0 border-t border-slate-700 pt-2 mt-2 space-y-2">
          {/* Pinned context — injected into every turn until cleared (#90). */}
          {pins.length > 0 && (
            <div className="flex flex-wrap items-center gap-1" data-testid="consult-pins">
              <span className="text-[10px] uppercase tracking-wide text-slate-500 mr-1">
                Context
              </span>
              {pins.map((p) => (
                <span
                  key={`${p.kind}:${p.id}`}
                  className="flex items-center gap-1 rounded bg-accent-info/15 border border-accent-info/30 px-1.5 py-0.5 text-[10px] text-slate-200"
                  title={`${p.kind} ${p.id}`}
                  data-testid="consult-pin-chip"
                >
                  <span className="text-slate-400">{p.kind}:</span>
                  <span className="truncate max-w-[140px]">{p.label ?? p.id}</span>
                  <button
                    onClick={() => consultActions().removePin(panelId, p.kind, p.id)}
                    className="text-slate-400 hover:text-slate-100"
                    title="unpin"
                    aria-label="unpin"
                  >
                    <X className="h-2.5 w-2.5" />
                  </button>
                </span>
              ))}
              <button
                onClick={() => consultActions().clearPins(panelId)}
                className="text-[10px] text-slate-400 hover:text-slate-200 underline underline-offset-2 ml-1"
                data-testid="consult-pins-clear"
              >
                Clear
              </button>
            </div>
          )}
          {error && (
            <div
              className="bg-accent-critical/10 border border-accent-critical/40 rounded p-2 text-xs text-accent-critical whitespace-pre-wrap"
              data-testid="consult-error"
            >
              {error}
            </div>
          )}
          {/* F1 model picker — which LLM plane answers this chat. */}
          <div className="flex items-center gap-2 text-[11px] text-slate-400">
            <label htmlFor="consult-model" className="shrink-0">
              Model
            </label>
            <select
              id="consult-model"
              value={model}
              onChange={(e) => {
                const m = e.target.value as ConsultModel
                setModel(m)
                saveConsultModel(m)
              }}
              className="bg-surface-200 border border-slate-700 rounded px-1.5 py-0.5 text-[11px]"
              data-testid="consult-model"
            >
              {CONSULT_MODEL_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          <div className="flex items-end gap-2">
            <textarea
              className="flex-1 bg-surface-200 border border-slate-700 rounded p-2 text-sm resize-none"
              rows={2}
              value={draft}
              onChange={(e) => consultActions().setDraft(panelId, e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  void send('chat')
                }
              }}
              placeholder="Ask the substrate…  (Enter to send · Shift+Enter for newline)"
              data-testid="consult-question"
            />
            <button
              onClick={() => send('chat')}
              disabled={busy || !draft.trim()}
              className="px-4 py-2 text-sm bg-accent-info/30 hover:bg-accent-info/50 disabled:opacity-50 rounded shrink-0"
              data-testid="consult-submit"
            >
              {busy ? 'Consulting…' : 'Send'}
            </button>
            {/* Only while something is in flight, and never gated on `busy` —
                this is the control that ENDS the spending, so it must work
                exactly when everything else is locked. */}
            {pendingTurn && (
              <button
                onClick={() => void stopAndSynthesize()}
                disabled={stopPhase !== null}
                className="px-3 py-2 text-sm bg-accent-critical/20 hover:bg-accent-critical/40 border border-accent-critical/40 text-accent-critical disabled:opacity-50 rounded shrink-0"
                data-testid="consult-stop"
                title="stop drilling now and answer from the evidence gathered so far"
              >
                {stopPhase === 'stopping'
                  ? 'Stopping…'
                  : stopPhase === 'synthesizing'
                    ? 'Synthesizing…'
                    : 'Stop'}
              </button>
            )}
          </div>
          <details className="text-xs text-slate-400">
            <summary className="cursor-pointer select-none">Options</summary>
            <div className="mt-2 space-y-2">
              <input
                className="w-full bg-surface-200 border border-slate-700 rounded p-2 text-xs font-mono"
                value={scope}
                onChange={(e) => consultActions().setScope(panelId, e.target.value)}
                placeholder='scope predicate (optional) — target.id == "brazil"'
                data-testid="consult-scope"
              />
              <label className="flex items-center gap-2">
                max tool rounds:
                <input
                  type="number"
                  min={1}
                  max={MAX_ROUNDS}
                  value={maxRounds}
                  onChange={(e) =>
                    consultActions().setMaxRounds(
                      panelId,
                      Math.max(1, Math.min(MAX_ROUNDS, Number(e.target.value) || 1)),
                    )
                  }
                  className="w-16 bg-surface-200 border border-slate-700 rounded px-1 py-0.5 text-xs font-mono"
                  data-testid="consult-max-rounds"
                />
              </label>
              <p className="text-[11px] text-slate-500">
                Deep, durable analysis (a long-running task that writes a finding) lives in its own{' '}
                <span className="text-slate-400 font-medium">Deep Consult</span> panel.
              </p>
            </div>
          </details>
        </div>
        </div>
      </div>
    </PanelChrome>
  )
}
