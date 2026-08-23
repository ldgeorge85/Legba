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
  apiPost,
  ApiError,
  listConsultSessions,
  loadConsultSession,
  loadConsultModel,
  saveConsultModel,
  CONSULT_MODEL_OPTIONS,
  type ConsultModel,
  type ConsultSessionSummary,
} from '@/lib/api'
import { RecordLink } from '@/components/inspector/RecordLink'
import { Pin, X } from 'lucide-react'
import { selectionKindOf, useSelection } from '@/state/selection'
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
}

const CONSULT_PATH = '/consult'
const MAX_ROUNDS = 30

/**
 * How often a turn orphaned by a reload re-reads its session looking for the
 * answer, and how long it keeps looking.
 *
 * The deadline mirrors `DAPR_INVOKE_TIMEOUT_SECONDS` in `consult_api.py`: past
 * it the registry has itself given up on the actor, so an answer is no longer
 * coming and continuing to poll would only misrepresent the turn as live.
 */
const REATTACH_POLL_MS = 3000
const REATTACH_DEADLINE_MS = 300_000

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

/** Compact one-line label for a streamed step. */
function stepLabel(s: StepFrame): string {
  const parts: string[] = []
  if (s.phase) parts.push(String(s.phase))
  if (s.kind) parts.push(String(s.kind))
  if (s.tool) parts.push(`tool=${String(s.tool)}`)
  if (typeof s.round === 'number') parts.push(`round=${s.round}`)
  return parts.join(' · ') || 'step'
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
  const selection = useSelection((s) => s.selection)
  const pinSelection = () => {
    if (!selection) return
    consultActions().addPin(panelId, selection)
  }
  const selectionPinned =
    !!selection && pins.some((p) => p.kind === selection.kind && p.id === selection.id)

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
  useEffect(() => {
    if (!pendingTurn || pendingTurn.stalled || !detached) return
    const { requestId, startedAt } = pendingTurn

    // No session id means the reload beat the FIRST response back: the server
    // minted the session, we never learned its id, and `consult_sessions`
    // carries no `request_id` to find it by. The turn is genuinely
    // unrecoverable automatically — say so instead of spinning. The run itself
    // is not lost: it is in the History sidebar under its own question.
    if (!sessionId || Date.now() - startedAt > REATTACH_DEADLINE_MS) {
      consultActions().markStalled(panelId, requestId)
      return
    }

    let cancelled = false
    const timer = window.setInterval(() => {
      if (Date.now() - startedAt > REATTACH_DEADLINE_MS) {
        consultActions().markStalled(panelId, requestId)
        return
      }
      const rev = consultActions().panel(panelId).rev
      void loadConsultSession(sessionId)
        .then((detail) => {
          if (!cancelled) consultActions().reconcile(panelId, detail, rev)
        })
        .catch(() => {
          // A poll that can't reach the registry just tries again next tick.
        })
    }, REATTACH_POLL_MS)

    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [panelId, pendingTurn, detached, sessionId])

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
    closeStream()
    try {
      const token = localStorage.getItem('legba_token') ?? ''
      const es = new EventSource(
        `/api/v1${CONSULT_PATH}/stream/${requestId}?token=${encodeURIComponent(token)}`,
      )
      esRef.current = es
      es.onmessage = (e: MessageEvent) => {
        let frame: StepFrame | null = null
        try {
          frame = JSON.parse(e.data) as StepFrame
        } catch {
          return
        }
        if (!frame) return
        if (frame.type === 'final') {
          closeStream()
          return
        }
        if (frame.type === 'step') {
          // Addressed by request id, so a frame arriving after the turn settled
          // (or belonging to a superseded turn) is dropped, not misfiled.
          consultActions().pushStep(panelId, requestId, frame)
        }
      }
      es.onerror = () => {
        // Best-effort live view — a stream error just stops the live ticker;
        // the authoritative answer still arrives on the POST response.
        closeStream()
      }
    } catch {
      // EventSource unsupported / blocked — degrade to no live steps.
      esRef.current = null
    }

    // Inject pinned records two ways: a `[Pinned …]` context prefix on the
    // question (works against today's text-only backend) AND a structured
    // `pinned_context` field a backend can hydrate full record bodies from.
    const pinnedPrefix = pins
      .map((p) => `[Pinned ${p.kind}: "${p.label ?? p.id}" (id=${p.id})]`)
      .join('\n')
    const questionWithPins = pinnedPrefix ? `${pinnedPrefix}\n\n${trimmed}` : trimmed

    try {
      const resp = await apiPost<ConsultResponse>(CONSULT_PATH, {
        question: questionWithPins,
        scope_predicate: scope.trim() || null,
        max_tool_rounds: maxRounds,
        mode,
        model,
        request_id: requestId,
        messages: priorMessages,
        session_id: sessionId,
        pinned_context: pins.map((p) => ({ kind: p.kind, id: p.id, label: p.label ?? null })),
      })
      // Thread the audit-trail session — the server opens one on the first turn
      // and echoes its id; pass it back on the next turn so the conversation
      // stays under one session.
      if (resp.session_id) consultActions().setSessionId(panelId, resp.session_id)
      // Read the live steps back off the store rather than a local ref: this
      // continuation may be running long after the component that started it
      // was unmounted, and the store is the only thing that still has them.
      const steps = consultActions().panel(panelId).pendingTurn?.steps ?? []
      consultActions().completeTurn(panelId, requestId, {
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
      })
    } catch (err) {
      consultActions().failTurn(panelId, requestId, formatApiError(err))
    } finally {
      closeStream()
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
    if (detached) return 'Reattached after a reload — waiting for the server…'
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
              <div className="flex items-center gap-2 mb-1">
                <div
                  className={
                    'text-[11px] ' +
                    (pendingTurn.stalled ? 'text-amber-300' : 'text-slate-400')
                  }
                  data-testid="consult-pending-label"
                >
                  {pendingLabel}
                </div>
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
