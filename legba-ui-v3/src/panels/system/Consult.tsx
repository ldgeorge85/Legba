/**
 * O-Consult / system.consult — daily-driver consult panel (Piece 1 rework).
 *
 * Wired to the `consult_on_demand` analyst kind (L-178) via the registry
 * proxy at `POST /api/v1/consult` (see
 * `src/legba/data/registry/consult_api.py`).
 *
 * This panel is a multi-turn CHAT surface:
 *   - `mode:'chat'` (default) → the actor answers without writing a finding;
 *     the answer comes back in the envelope (no DB row). History is held
 *     client-side here and re-sent as `messages[]` on each turn.
 *   - Live thinking → before POSTing, the panel mints a `request_id`, opens an
 *     `EventSource` on `/api/v1/consult/stream/<id>?token=...`, and renders each
 *     ReAct step as it streams; the stream closes on the terminal `final` frame.
 *
 * Deep, durable analysis (a long-running task that writes a finding) lives in
 * its OWN `Deep Consult` panel (`system.deep_consult`) — this panel is the
 * lightweight chat surface only.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { PanelChrome } from '@/components/PanelChrome'
import {
  apiPost,
  ApiError,
  listConsultSessions,
  loadConsultSession,
  type ConsultSessionSummary,
} from '@/lib/api'
import { RecordLink } from '@/components/inspector/RecordLink'
import { Pin, X } from 'lucide-react'
import { selectionKindOf, useSelection, type Selection } from '@/state/selection'
import type { PanelProps } from '@/types'

interface ConsultToolCall {
  tool: string
  args: Record<string, unknown>
  result: unknown
}

interface ConsultCitedRef {
  kind: string
  id: string
  description?: string | null
}

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
}

/** One streamed ReAct step frame off the SSE relay. */
interface StepFrame {
  type: string
  phase?: string
  kind?: string
  tool?: string
  round?: number
  [k: string]: unknown
}

/** One turn of the client-held transcript. */
interface ChatTurn {
  role: 'user' | 'assistant'
  content: string
  steps?: StepFrame[]
  toolCalls?: ConsultToolCall[]
  citedRefs?: ConsultCitedRef[]
  uncertainty?: number | null
  unansweredAspects?: string[]
  findingId?: string | null
  deep?: boolean
}

const CONSULT_PATH = '/consult'
const DEFAULT_ROUNDS = 10
const MAX_ROUNDS = 30

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
  const [question, setQuestion] = useState('')
  const [scope, setScope] = useState('')
  const [maxRounds, setMaxRounds] = useState(DEFAULT_ROUNDS)
  const [transcript, setTranscript] = useState<ChatTurn[]>([])
  const [liveSteps, setLiveSteps] = useState<StepFrame[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // The persisted audit-trail session (0038). Set on the first server reply;
  // re-sent on each subsequent turn so the whole conversation threads under one
  // session, and set directly when a prior session is loaded from history.
  const [sessionId, setSessionId] = useState<string | null>(null)
  // History sidebar: prior chat sessions, most-recently-active first.
  const [sessions, setSessions] = useState<ConsultSessionSummary[]>([])
  const [historyOpen, setHistoryOpen] = useState(false)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const esRef = useRef<EventSource | null>(null)
  // Mirror of liveSteps readable synchronously inside the async submit closure
  // (state reads there are stale). Kept in lockstep via pushLiveStep / reset.
  const liveStepsRef = useRef<StepFrame[]>([])
  // Auto-scroll the conversation to the newest turn / live step.
  const scrollRef = useRef<HTMLDivElement | null>(null)

  // Pin-to-context (#90): the operator pins records from the shared selection
  // into a sticky set; every pin is injected into each turn's context (see
  // `send`). Pins persist as the operator navigates and accumulate.
  const selection = useSelection((s) => s.selection)
  const [pins, setPins] = useState<Selection[]>([])
  const pinSelection = () => {
    if (!selection) return
    setPins((prev) =>
      prev.some((p) => p.kind === selection.kind && p.id === selection.id)
        ? prev
        : [...prev, selection],
    )
  }
  const removePin = (kind: string, id: string) =>
    setPins((prev) => prev.filter((p) => !(p.kind === kind && p.id === id)))
  const selectionPinned =
    !!selection && pins.some((p) => p.kind === selection.kind && p.id === selection.id)

  const pushLiveStep = (frame: StepFrame) => {
    liveStepsRef.current = [...liveStepsRef.current, frame]
    setLiveSteps(liveStepsRef.current)
  }

  const resetLiveSteps = () => {
    liveStepsRef.current = []
    setLiveSteps([])
  }

  const closeStream = () => {
    if (esRef.current) {
      esRef.current.close()
      esRef.current = null
    }
  }

  // Keep the conversation pinned to the bottom as turns / steps arrive.
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [transcript, liveSteps, loading])

  const send = async (mode: 'chat' | 'deep') => {
    const trimmed = question.trim()
    if (!trimmed || loading) return

    setError(null)
    setLoading(true)
    resetLiveSteps()

    // Snapshot the transcript-so-far for the server (prior turns) BEFORE we
    // optimistically append the new user turn locally.
    const priorMessages = transcript.map((t) => ({
      role: t.role,
      content: t.content,
    }))
    setTranscript((prev) => [...prev, { role: 'user', content: trimmed }])
    setQuestion('')

    // Subscribe to the step stream BEFORE POSTing (subscribe-before-publish).
    const requestId = crypto.randomUUID()
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
          pushLiveStep(frame)
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
        request_id: requestId,
        messages: priorMessages,
        session_id: sessionId,
        pinned_context: pins.map((p) => ({ kind: p.kind, id: p.id, label: p.label ?? null })),
      })
      // Thread the audit-trail session — the server opens one on the first turn
      // and echoes its id; pass it back on the next turn so the conversation
      // stays under one session.
      if (resp.session_id) setSessionId(resp.session_id)
      setTranscript((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: resp.answer,
          steps: liveStepsRef.current,
          toolCalls: resp.tool_calls,
          citedRefs: resp.cited_refs,
          uncertainty: resp.uncertainty,
          unansweredAspects: resp.unanswered_aspects,
          findingId: resp.finding_id,
          deep: mode === 'deep',
        },
      ])
    } catch (err) {
      setError(formatApiError(err))
    } finally {
      closeStream()
      resetLiveSteps()
      setLoading(false)
    }
  }

  const resetChat = () => {
    closeStream()
    setTranscript([])
    resetLiveSteps()
    setError(null)
    setLoading(false)
    setPins([])
    // Start a fresh conversation — the next turn opens a new session.
    setSessionId(null)
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
    if (loading) return
    setError(null)
    closeStream()
    resetLiveSteps()
    try {
      const detail = await loadConsultSession(id)
      const turns: ChatTurn[] = detail.turns.map((t) => ({
        role: t.role,
        content: t.content,
        steps: (t.steps as StepFrame[]) ?? [],
        toolCalls: (t.tool_calls as ConsultToolCall[]) ?? [],
        citedRefs: (t.cited_refs as ConsultCitedRef[]) ?? [],
        findingId: t.finding_id ?? null,
        deep: Boolean(t.finding_id),
      }))
      setTranscript(turns)
      setSessionId(detail.id)
      setHistoryOpen(false)
    } catch (err) {
      setError(formatApiError(err))
    }
  }

  // Refresh the history list when the sidebar opens.
  useEffect(() => {
    if (historyOpen) void loadHistory()
  }, [historyOpen, loadHistory])

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
                    disabled={loading}
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
            disabled={loading}
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
          {transcript.length === 0 && !loading && (
            <div className="text-label text-ink-3 py-6 text-center">
              Ask the substrate anything — answers cite the records they used.
            </div>
          )}

          {transcript.length > 0 && (
            <div className="space-y-3" data-testid="consult-transcript">
              {transcript.map((turn, i) =>
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

          {/* Live thinking (in-flight turn) */}
          {loading && (
            <div
              className="bg-surface-200 rounded p-2 border border-slate-700"
              data-testid="consult-live-steps"
            >
              <div className="text-[11px] text-slate-400 mb-1">
                Thinking… ({liveSteps.length} steps)
              </div>
              <ul className="text-[10px] font-mono space-y-0.5 text-slate-400 max-h-40 overflow-y-auto">
                {liveSteps.map((s, i) => (
                  <li key={i}>{stepLabel(s)}</li>
                ))}
              </ul>
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
                    onClick={() => removePin(p.kind, p.id)}
                    className="text-slate-400 hover:text-slate-100"
                    title="unpin"
                    aria-label="unpin"
                  >
                    <X className="h-2.5 w-2.5" />
                  </button>
                </span>
              ))}
              <button
                onClick={() => setPins([])}
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
          <div className="flex items-end gap-2">
            <textarea
              className="flex-1 bg-surface-200 border border-slate-700 rounded p-2 text-sm resize-none"
              rows={2}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
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
              disabled={loading || !question.trim()}
              className="px-4 py-2 text-sm bg-accent-info/30 hover:bg-accent-info/50 disabled:opacity-50 rounded shrink-0"
              data-testid="consult-submit"
            >
              {loading ? 'Consulting…' : 'Send'}
            </button>
          </div>
          <details className="text-xs text-slate-400">
            <summary className="cursor-pointer select-none">Options</summary>
            <div className="mt-2 space-y-2">
              <input
                className="w-full bg-surface-200 border border-slate-700 rounded p-2 text-xs font-mono"
                value={scope}
                onChange={(e) => setScope(e.target.value)}
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
                    setMaxRounds(Math.max(1, Math.min(MAX_ROUNDS, Number(e.target.value) || 1)))
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
