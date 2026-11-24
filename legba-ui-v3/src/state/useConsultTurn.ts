/**
 * useConsultTurn — ONE Consult turn's lifecycle, for every shell (M-1).
 *
 * Moved out of `panels/system/Consult.tsx` on 2026-11-14 so the phone stops
 * running its own copy (it used to poll instead of streaming, had no STOP and
 * never saw the turn contract). What lives here is everything between the
 * Send press and a settled answer:
 *
 *   * SEND — build the body (`lib/consultCore` `buildConsultRequest`), start
 *     the turn in the store, subscribe to `/consult/stream/{request_id}` BEFORE
 *     the POST, then POST. A 200 settles from its body; a **202** leaves the
 *     stream open and the answer lands on the registry's terminal frame.
 *   * SETTLE — one path for every arrival (the POST's 200, the terminal frame,
 *     the run-status poll, a reconnect, a salvage), which lifts the contract
 *     (`parseTurnContract`), the case record, the writes, the usage receipt
 *     and the verification onto the settled turn.
 *   * DETACH AND RECOVER — a turn orphaned by a reload is polled back from the
 *     run, then the session; a POST that failed outright is recovered from the
 *     run status before it is failed.
 *   * STOP — `stopConsultRun` (or the case player's stop) then
 *     `synthesizeConsultRun` on the shell's model, then settle.
 *   * RESYNTHESISE — a settled turn whose synthesis was cut, replaced in place.
 *
 * The conversation itself is NOT here: it is `state/consultSession`, keyed by
 * the panel id, so it outlives any component (GLASS-4). This hook owns only
 * what cannot outlive the component — the EventSource (closed on unmount) and
 * the stream's liveness clock — plus the two salvage phases.
 *
 * What each shell supplies: the model, and per turn the question text and
 * pins it resolved, its on-screen report, the window and grounding choice,
 * whether the turn drives the view and whether writes are armed.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  apiPostWithStatus,
  ApiError,
  getConsultRun,
  loadConsultSession,
  stopConsultRun,
  synthesizeConsultRun,
  type ConsultModel,
  type ConsultRunStatus,
  type ConsultSynthesisBody,
  type ConsultUncertaintyFloor,
  type ConsultUsage,
  type ConsultWatchMint,
} from '@/lib/api'
import {
  consultActions,
  isDetached,
  useConsultPanel,
  CONSULT_PAGE_LOAD_ID,
  stripFinalFrame,
  type ChatTurn,
  type ConsultCitedRef,
  type ConsultToolCall,
  type ProvenanceCensus,
  type StepFrame,
} from '@/state/consultSession'
import { parseKeepalive, quietRound, type StreamLiveness } from '@/lib/consultLiveness'
import { parseCaseRecord } from '@/lib/caseModel'
import { casePlayer } from '@/state/casePlayer'
import { parseUsageReceipt, parseVerification } from '@/lib/consultTurnStamp'
import type { ConsultWriteEntry } from '@/lib/consultWrites'
import { parseTurnContract } from '@/lib/consultContract'
import type { TurnPin } from '@/lib/consultContext'
import {
  buildConsultRequest,
  newConsultRequestId,
  turnPresentsCase,
  type ConsultRequestInput,
  type ConsultTurnMode,
} from '@/lib/consultCore'

export interface ConsultResponse {
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
  /** 2026-09-28 §B.3 — see {@link ChatTurn.holdReason}. */
  hold_reason?: string | null
  /** §B.4 — see {@link ChatTurn.uncertaintyFloor}. */
  uncertainty_floor_applied?: ConsultUncertaintyFloor | null
  watch_candidates?: string[] | null
  watch_minted?: ConsultWatchMint[] | null
  /** C-1b — the case this turn presented (`case/1`); null = none. */
  case?: unknown
  /** C-2b-1 — the writes of an ARMED turn; null = not armed. */
  writes?: ConsultWriteEntry[] | null
  /** X2 / C1 — the `consult_verification/1` stamp; see `lib/consultTurnStamp`. */
  verification?: unknown
  /** Q-1 — the turn contract (`consult_contract/1`); see `lib/consultContract`. */
  contract?: unknown
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

export const CONSULT_PATH = '/consult'

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
export const REATTACH_POLL_MS = 3000
export const REATTACH_DEADLINE_MS = 540_000

export function formatApiError(err: unknown): string {
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

/** What a shell hands {@link UseConsultTurn.send} for one turn. */
export interface ConsultTurnSend {
  /** The question as the user typed it (trimmed) — the user turn's text. */
  question: string
  mode: ConsultTurnMode
  /** UI-B6 — the ambient pin the question is asked under (omit: not recorded). */
  pinnedTo?: TurnPin | null
  /**
   * The rest of the request, as the shell resolved it at send time. The hook
   * fills in the request id, the session, the history, the mode and the model.
   */
  request: Omit<ConsultRequestInput, 'requestId' | 'sessionId' | 'messages' | 'mode' | 'model'>
}

export type ConsultTurnState = 'idle' | 'streaming' | 'detached' | 'stopping' | 'error'

export interface UseConsultTurn {
  send: (input: ConsultTurnSend) => Promise<void>
  /** STOP — end the spending, then answer from what was already bought. */
  stop: () => Promise<void>
  /** Re-synthesise a SETTLED turn whose answer was cut short, in place. */
  resynthesize: (index: number, turn: ChatTurn) => Promise<void>
  closeStream: () => void
  stopPhase: 'stopping' | 'synthesizing' | null
  resynthIndex: number | null
  /** The quiet-stretch line ("synthesising · 42s") while a keepalive vouches for it. */
  quiet: ReturnType<typeof quietRound> | null
  /** A turn whose POST this page load no longer owns (the reload case). */
  detached: boolean
  /** A turn is in flight and not stalled — the composer is locked. */
  busy: boolean
  state: ConsultTurnState
}

export function useConsultTurn(panelId: string, opts: { model: ConsultModel }): UseConsultTurn {
  const { model } = opts
  const panel = useConsultPanel(panelId)
  const { sessionId, pendingTurn, error } = panel

  const [stopPhase, setStopPhase] = useState<'stopping' | 'synthesizing' | null>(null)
  const [resynthIndex, setResynthIndex] = useState<number | null>(null)

  const esRef = useRef<EventSource | null>(null)
  // The live stream's clock — when it last delivered a frame, and whether a
  // KEEPALIVE has vouched for the silence since (`lib/consultLiveness`). Local,
  // not in the store: it describes this component's EventSource, which closes
  // on unmount, and a remount re-opens a stream with a fresh clock.
  const [liveness, setLiveness] = useState<StreamLiveness | null>(null)
  const [nowMs, setNowMs] = useState(() => Date.now())

  // A turn whose POST this page load no longer owns (the reload case) — it can
  // only be recovered from the server, so it is polled rather than awaited.
  const detached = isDetached(pendingTurn)
  // A stalled turn must not lock the composer: the operator gets the panel back.
  const busy = !!pendingTurn && !pendingTurn.stalled

  const closeStream = useCallback(() => {
    if (esRef.current) {
      esRef.current.close()
      esRef.current = null
    }
    setLiveness(null)
  }, [])

  // Tick the quiet-stretch counter once a second, but only while a keepalive
  // has vouched for the silence — an idle panel runs no timer.
  const vouched = liveness?.keepaliveAt ?? null
  useEffect(() => {
    if (vouched === null) return
    setNowMs(Date.now())
    const t = window.setInterval(() => setNowMs(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [vouched])

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
          holdReason: resp.hold_reason ?? null,
          uncertaintyFloor: resp.uncertainty_floor_applied ?? null,
          watchCandidates: resp.watch_candidates ?? null,
          watchMinted: resp.watch_minted ?? null,
          // C-1b — the case record rides the settled turn: Replay reads it.
          ...(parseCaseRecord(resp.case) ? { caseRecord: parseCaseRecord(resp.case) } : {}),
          // C-2b-1 — an armed turn's writes; the cards render from the ledger.
          ...(Array.isArray(resp.writes) ? { writes: resp.writes } : {}),
          // Q-1 — the turn contract, drawn under the answer.
          ...(parseTurnContract(resp.contract) ? { contract: parseTurnContract(resp.contract) } : {}),
          // X2 / C1 — the turn header's usage receipt + judged label.
          usageReceipt: parseUsageReceipt(resp.usage),
          ...(parseVerification(resp.verification) !== undefined
            ? { verification: parseVerification(resp.verification) }
            : {}),
        })
        // A live case reconciles against the record — the authority — and
        // resumes any step a dropped frame held back.
        if (casePlayer().player?.requestId === requestId) casePlayer().landRecord(resp.case)
        return
      }
      if (casePlayer().player?.requestId === requestId) casePlayer().landRecord(null)
      const watched = live.answerPreview ? stripFinalFrame(live.answerPreview).trim() : ''
      // No answer — render the partial rather than dropping the turn.
      store.completeTurn(panelId, requestId, {
        role: 'assistant',
        // The server's partial leads with the streamed answer when there was
        // one (run 9b902fec). Without it, the answer this panel watched stream
        // is still the operator's — it was paid for — so it is kept rather
        // than replaced with a line saying nothing was recorded.
        content:
          outcome.partialAnswer ||
          (watched
            ? `_This consult was cut mid-answer; below is what it had written, unfinished._\n\n${watched}`
            : '_This consult did not finish, and the server recorded no partial._'),
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
        // The registry's partial — the streamed answer first, when there was
        // one — rides the run status too, for a client that reconnected
        // after the terminal frame went out.
        partialAnswer: typeof run.partial_answer === 'string' ? run.partial_answer : null,
        error: `consult ${run.error_status ?? 502}: ${detail}`,
      })
    },
    [settleTurn],
  )

  /**
   * Open the step stream for `requestId` and render everything it sends.
   *
   * A turn can need a stream twice: once when it starts, and again after a
   * reload that left the run going. Both want identical behaviour, and a
   * second copy of this would be a second place for the terminal-frame
   * handling to drift.
   */
  const openStream = useCallback(
    (requestId: string, mode: 'chat' | 'deep') => {
      closeStream()
      const token = (() => {
        try {
          return localStorage.getItem('legba_token') ?? ''
        } catch {
          return ''
        }
      })()
      const url = `/api/v1${CONSULT_PATH}/stream/${requestId}?token=${encodeURIComponent(token)}`
      // C-1b — a CASE turn's stream belongs to the player, not this tile: the
      // case's own `arrange` can unmount this tile, and a tile-owned stream
      // would be cut by it. The handlers below only write through the stores,
      // so they keep working after an unmount.
      const player = casePlayer()
      if (player.player?.requestId === requestId && player.player.mode === 'live') {
        setLiveness({ requestId, lastFrameAt: Date.now(), keepaliveAt: null })
        player.openStream(url, {
          onStep: (frame) => consultActions().pushStep(panelId, requestId, frame as StepFrame),
          onTerminal: (frame) =>
            settleFromTerminal(requestId, mode, frame as unknown as TerminalFrame),
          onActorFinal: () =>
            consultActions().pushStep(panelId, requestId, {
              type: 'step',
              phase: 'reflect',
              kind: 'awaiting_answer',
            }),
          onKeepalive: (data) => {
            if (!parseKeepalive(data)) return
            setLiveness((prev) =>
              prev && prev.requestId === requestId ? { ...prev, keepaliveAt: Date.now() } : prev,
            )
          },
          onFrame: () =>
            setLiveness((prev) =>
              prev && prev.requestId === requestId
                ? { ...prev, lastFrameAt: Date.now(), keepaliveAt: null }
                : prev,
            ),
          onError: () => setLiveness(null),
        })
        return
      }
      try {
        const es = new EventSource(url)
        esRef.current = es
        setLiveness({ requestId, lastFrameAt: Date.now(), keepaliveAt: null })
        // The relay's NAMED keepalive (a comment line would never reach
        // script). It proves the run is alive through a quiet stretch — the
        // long final synthesis that used to look like a hang. Guarded so a
        // client without `addEventListener` still gets its step frames.
        if (typeof es.addEventListener === 'function') {
          es.addEventListener('keepalive', (e: Event) => {
            if (!parseKeepalive((e as MessageEvent).data)) return
            setLiveness((prev) =>
              prev && prev.requestId === requestId ? { ...prev, keepaliveAt: Date.now() } : prev,
            )
          })
        }
        es.onmessage = (e: MessageEvent) => {
          let frame: (StepFrame & TerminalFrame) | null = null
          try {
            frame = JSON.parse(e.data) as StepFrame & TerminalFrame
          } catch {
            return
          }
          if (!frame) return
          // Any frame ends the quiet stretch; the next keepalive starts a new one.
          setLiveness((prev) =>
            prev && prev.requestId === requestId
              ? { ...prev, lastFrameAt: Date.now(), keepaliveAt: null }
              : prev,
          )
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

  const send = async ({ question: trimmed, mode, pinnedTo, request }: ConsultTurnSend) => {
    const store = consultActions()
    const slice = store.panel(panelId)
    if (!trimmed || (slice.pendingTurn && !slice.pendingTurn.stalled)) return

    // Snapshot the transcript-so-far for the server (prior turns) BEFORE the
    // store optimistically appends the new user turn.
    const priorMessages = slice.transcript.map((t) => ({ role: t.role, content: t.content }))
    const requestId = newConsultRequestId()

    // Stamp the turn with THIS page load: while the stamp matches, the reattach
    // poll leaves the turn alone, because the promise below will answer it.
    store.startTurn(panelId, {
      requestId,
      question: trimmed,
      mode,
      startedAt: Date.now(),
      pageLoadId: CONSULT_PAGE_LOAD_ID,
      steps: [],
      // UI-B6 — what the answer is asked under; its census line prints it.
      ...(pinnedTo !== undefined ? { pinnedTo } : {}),
    })

    // D1 — the case toggle applies to THIS turn only.
    const asCase = turnPresentsCase({ presentCase: request.presentCase, mode })
    // A case turn's player is armed BEFORE the stream opens, so `openStream`
    // hands the stream to the player rather than to this tile.
    if (asCase) casePlayer().beginLive(requestId, panelId)

    // Subscribe to the step stream BEFORE POSTing (subscribe-before-publish).
    openStream(requestId, mode)

    const body = buildConsultRequest({
      ...request,
      requestId,
      sessionId: slice.sessionId,
      messages: priorMessages,
      mode,
      model,
    })

    try {
      const { status, body: resp } = await apiPostWithStatus<ConsultResponse>(CONSULT_PATH, body)
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
      // A case that never became a run (a 409 `case_unavailable`, say) moved
      // nothing: drop its player rather than leave an empty rail standing.
      const cp = casePlayer()
      if (asCase && cp.player?.requestId === requestId && cp.player.steps.length === 0) {
        cp.abandon(requestId)
      }
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
  const stop = async () => {
    const pending = consultActions().panel(panelId).pendingTurn
    if (!pending || stopPhase) return
    const { requestId, mode } = pending
    let stopNote: string | null = null
    setStopPhase('stopping')
    try {
      try {
        // A case turn stops through the player: the latch holds FIRST, and
        // the route is told the last step the wall actually ran.
        if (casePlayer().player?.requestId === requestId) {
          await casePlayer().stop()
          const failed = casePlayer().stopError
          if (failed) stopNote = failed
        } else {
          await stopConsultRun(requestId)
        }
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
  const resynthesize = async (index: number, turn: ChatTurn) => {
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
        // 2026-09-28 §B.3 — the FRESH census, never the stale one. This is a
        // NEW answer over the same evidence: keeping the original run's counts
        // beside it (which is what spreading `...turn` did, because the
        // recovery carried no census at all) prints a number describing prose
        // that no longer exists. `?? null` and not `?? turn.provenanceCensus`
        // for exactly that reason — a recovery whose census could not be
        // measured shows none, rather than borrowing one.
        provenanceCensus: body.provenance_census ?? null,
        holdReason: body.hold_reason ?? null,
        // Same rule as the census: the recovery's OWN floor and watch list.
        uncertaintyFloor: body.uncertainty_floor_applied ?? null,
        watchCandidates: body.watch_candidates ?? null,
        watchMinted: body.watch_minted ?? null,
        // X2 / C1 — the recovery's OWN usage and judged label, never the
        // replaced turn's (same rule as the census above).
        usageReceipt: parseUsageReceipt(body.usage),
        verification: parseVerification(body.verification),
        // Q-1 — the recovery's OWN contract, never the replaced turn's.
        contract: parseTurnContract(body.contract),
        // A recovery is a live answer with a live response; the reload
        // marker of the turn it replaced no longer describes it.
        responseRecorded: undefined,
      })
    } catch (err) {
      consultActions().setError(panelId, formatApiError(err))
    } finally {
      setResynthIndex(null)
    }
  }

  // The last round's quiet-stretch line ("synthesising · 42s"), shown only
  // while a keepalive vouches for the silence and only for THIS turn's stream.
  const quiet =
    pendingTurn && !pendingTurn.stalled && liveness?.requestId === pendingTurn.requestId
      ? quietRound(liveness, pendingTurn.steps, pendingTurn.roundCap, nowMs)
      : null

  const state: ConsultTurnState = stopPhase
    ? 'stopping'
    : pendingTurn && !pendingTurn.stalled
      ? detached
        ? 'detached'
        : 'streaming'
      : error
        ? 'error'
        : 'idle'

  return {
    send,
    stop,
    resynthesize,
    closeStream,
    stopPhase,
    resynthIndex,
    quiet,
    detached,
    busy,
    state,
  }
}
