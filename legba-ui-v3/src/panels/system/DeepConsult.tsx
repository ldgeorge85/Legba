/**
 * Deep Consult (`system.deep_consult`) — the on-demand DEEP analysis surface
 * (anchor §5 PIECE 4).
 *
 * Unlike the chat Consult panel (which answers in the envelope, no durable
 * row), Deep Consult submits a DETACHED staged Dapr Workflow
 * (plan → acquire → analyze → synthesize) that runs minutes → hours and
 * produces a lineage-walkable FINDING (+ optional facts/hypotheses).
 *
 *   - Submit → `POST /api/v1/deep_consult` returns a `task_id` IMMEDIATELY
 *     (the actor schedules the workflow and returns; never the 180s block).
 *   - Poll → `GET /api/v1/deep_consult/{task_id}` on an interval until the
 *     status flips to `completed` (the synthesize stage wrote the finding) or
 *     `failed`.
 *   - Result → the synthesized answer + a lineage link to the produced
 *     `finding_id` (walkable via the lineage route).
 *
 * Backed by `src/legba/data/registry/deep_consult_api.py` via
 * `lib/api.submitDeepConsult` / `getDeepConsultStatus`.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { PanelChrome } from '@/components/PanelChrome'
import {
  submitDeepConsult,
  getDeepConsultStatus,
  listConsultSessions,
  loadConsultSession,
  ApiError,
  type DeepConsultStatus,
  type ConsultSessionSummary,
} from '@/lib/api'
import type { PanelProps } from '@/types'

const POLL_INTERVAL_MS = 3000

function formatApiError(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body
    if (typeof body === 'string') return body
    if (body && typeof body === 'object' && 'detail' in body) {
      const d = (body as { detail: unknown }).detail
      return typeof d === 'string' ? d : JSON.stringify(d)
    }
    return `API error ${err.status}`
  }
  return err instanceof Error ? err.message : String(err)
}

export default function DeepConsultPanel({ registration }: PanelProps) {
  const [question, setQuestion] = useState('')
  const [scope, setScope] = useState('')
  const [emitFacts, setEmitFacts] = useState(true)
  const [emitHypotheses, setEmitHypotheses] = useState(true)

  const [statusResp, setStatusResp] = useState<DeepConsultStatus | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Task history (0038 audit trail) — prior deep-consult submissions.
  const [tasks, setTasks] = useState<ConsultSessionSummary[]>([])
  const [historyError, setHistoryError] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }

  useEffect(() => stopPolling, [])

  // Load the deep-consult task-history list (mode=deep sessions).
  const loadHistory = useCallback(async () => {
    setHistoryError(null)
    try {
      const rows = await listConsultSessions({ mode: 'deep', limit: 50 })
      setTasks(rows)
    } catch (err) {
      setHistoryError(formatApiError(err))
    }
  }, [])

  // Refresh history on mount.
  useEffect(() => {
    void loadHistory()
  }, [loadHistory])

  const submit = async () => {
    const trimmed = question.trim()
    if (!trimmed || submitting) return
    setError(null)
    setSubmitting(true)
    setStatusResp(null)
    stopPolling()
    try {
      const resp = await submitDeepConsult({
        question: trimmed,
        scope_predicate: scope.trim() || null,
        emit_facts: emitFacts,
        emit_hypotheses: emitHypotheses,
      })
      setStatusResp({
        task_id: resp.task_id,
        status: 'running',
        cited_refs: [],
        fact_ids: [],
        hypothesis_ids: [],
      })
      // The new task shows up in history immediately (submit persisted it).
      void loadHistory()
      // Begin polling.
      pollRef.current = setInterval(() => void poll(resp.task_id), POLL_INTERVAL_MS)
    } catch (err) {
      setError(formatApiError(err))
    } finally {
      setSubmitting(false)
    }
  }

  const poll = async (id: string) => {
    try {
      const s = await getDeepConsultStatus(id)
      setStatusResp(s)
      if (s.status === 'completed' || s.status === 'failed') {
        stopPolling()
        // Reflect the terminal turn-count / completion in the history list.
        void loadHistory()
      }
    } catch (err) {
      // Transient poll failure — keep polling; surface the last error.
      setError(formatApiError(err))
    }
  }

  // Re-open a prior task from the history list: show its current status. If
  // still running, resume polling; otherwise show the terminal result.
  const openTask = async (taskId: string) => {
    if (submitting) return
    setError(null)
    stopPolling()
    try {
      const s = await getDeepConsultStatus(taskId)
      setStatusResp(s)
      if (s.status !== 'completed' && s.status !== 'failed') {
        pollRef.current = setInterval(() => void poll(taskId), POLL_INTERVAL_MS)
      }
    } catch (err) {
      // Fall back to the persisted session turns when the live status read
      // fails (e.g. an old task the finding-row lookup can't resolve).
      try {
        const session = tasks.find((t) => t.task_id === taskId)
        if (session) {
          const detail = await loadConsultSession(session.id)
          const answerTurn = detail.turns.find((t) => t.role === 'assistant')
          setStatusResp({
            task_id: taskId,
            status: answerTurn ? 'completed' : 'running',
            answer: answerTurn?.content ?? null,
            finding_id: answerTurn?.finding_id ?? null,
            cited_refs: [],
            fact_ids: [],
            hypothesis_ids: [],
          })
        } else {
          setError(formatApiError(err))
        }
      } catch (inner) {
        setError(formatApiError(inner))
      }
    }
  }

  const status = statusResp?.status
  const isTerminal = status === 'completed' || status === 'failed'

  return (
    <PanelChrome registration={registration}>
      <div className="flex flex-col gap-3 p-3">
        <label className="text-sm font-medium" htmlFor="dc-question">
          Question
        </label>
        <textarea
          id="dc-question"
          className="min-h-[88px] rounded border border-neutral-700 bg-neutral-900 p-2 text-sm"
          placeholder="Ask a deep analytical question over the substrate…"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          disabled={submitting}
        />
        <input
          className="rounded border border-neutral-700 bg-neutral-900 p-2 text-sm"
          placeholder="Scope predicate (optional)"
          value={scope}
          onChange={(e) => setScope(e.target.value)}
          disabled={submitting}
        />
        <div className="flex items-center gap-4 text-sm">
          <label className="flex items-center gap-1">
            <input
              type="checkbox"
              checked={emitFacts}
              onChange={(e) => setEmitFacts(e.target.checked)}
            />
            Emit facts
          </label>
          <label className="flex items-center gap-1">
            <input
              type="checkbox"
              checked={emitHypotheses}
              onChange={(e) => setEmitHypotheses(e.target.checked)}
            />
            Emit hypotheses
          </label>
          <button
            type="button"
            className="ml-auto rounded bg-indigo-600 px-3 py-1.5 text-sm font-medium disabled:opacity-50"
            onClick={() => void submit()}
            disabled={submitting || !question.trim()}
          >
            {submitting ? 'Submitting…' : 'Submit Deep Consult'}
          </button>
        </div>

        {error && (
          <div className="rounded border border-red-700 bg-red-950 p-2 text-sm text-red-300">
            {error}
          </div>
        )}

        {statusResp && (
          <div className="rounded border border-neutral-700 bg-neutral-900 p-3 text-sm">
            <div className="flex items-center gap-2">
              <span className="font-mono text-xs text-neutral-400">
                {statusResp.task_id}
              </span>
              <span
                className={
                  'rounded px-2 py-0.5 text-xs ' +
                  (status === 'completed'
                    ? 'bg-green-800 text-green-200'
                    : status === 'failed'
                      ? 'bg-red-800 text-red-200'
                      : 'bg-amber-800 text-amber-100')
                }
              >
                {status}
              </span>
              {!isTerminal && (
                <span className="text-xs text-neutral-500">polling…</span>
              )}
            </div>

            {status === 'completed' && statusResp.answer && (
              <div className="prose prose-invert prose-sm mt-3 max-w-none">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {statusResp.answer}
                </ReactMarkdown>
              </div>
            )}

            {status === 'failed' && (
              <div className="mt-2 text-red-300">
                {statusResp.detail ?? 'workflow failed'}
              </div>
            )}

            {status === 'completed' && statusResp.finding_id && (
              <div className="mt-3 flex flex-col gap-1 text-xs text-neutral-400">
                <span>
                  Finding:{' '}
                  <a
                    className="text-indigo-400 underline"
                    href={`#/lineage/${statusResp.finding_id}`}
                  >
                    {statusResp.finding_id}
                  </a>
                </span>
                {statusResp.uncertainty != null && (
                  <span>uncertainty: {statusResp.uncertainty.toFixed(2)}</span>
                )}
                {statusResp.cited_refs.length > 0 && (
                  <span>lineage refs: {statusResp.cited_refs.length}</span>
                )}
                {statusResp.fact_ids.length > 0 && (
                  <span>facts: {statusResp.fact_ids.length}</span>
                )}
                {statusResp.hypothesis_ids.length > 0 && (
                  <span>hypotheses: {statusResp.hypothesis_ids.length}</span>
                )}
              </div>
            )}
          </div>
        )}

        {/* Task history (0038 audit trail) — prior deep-consult submissions. */}
        <div className="mt-2 border-t border-neutral-800 pt-2" data-testid="deep-consult-history">
          <div className="flex items-center justify-between mb-1">
            <span className="text-[11px] uppercase tracking-wide text-neutral-500">
              Task history
            </span>
            <button
              type="button"
              onClick={() => void loadHistory()}
              className="text-[10px] text-neutral-500 hover:text-neutral-300"
              title="refresh task history"
              data-testid="deep-consult-history-refresh"
            >
              ↻
            </button>
          </div>
          {historyError && (
            <div className="text-[10px] text-red-400 mb-1">{historyError}</div>
          )}
          {tasks.length === 0 && !historyError && (
            <div className="text-[11px] text-neutral-600 py-1">
              No deep-consult tasks yet.
            </div>
          )}
          <ul className="space-y-1" data-testid="deep-consult-history-list">
            {tasks.map((t) => (
              <li key={t.id}>
                <button
                  type="button"
                  onClick={() => t.task_id && void openTask(t.task_id)}
                  disabled={submitting || !t.task_id}
                  className={
                    'w-full text-left rounded px-2 py-1 text-xs hover:bg-neutral-800 disabled:opacity-50 ' +
                    (t.task_id && t.task_id === statusResp?.task_id
                      ? 'bg-neutral-800 text-neutral-100'
                      : 'text-neutral-300')
                  }
                  title={t.title}
                  data-testid="deep-consult-history-item"
                >
                  <div className="truncate">{t.title || '(untitled)'}</div>
                  <div className="font-mono text-[10px] text-neutral-500 truncate">
                    {t.task_id ?? t.id}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </PanelChrome>
  )
}
