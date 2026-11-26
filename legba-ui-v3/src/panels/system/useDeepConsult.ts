/**
 * useDeepConsult — the deep (detached) side of Consult, as a hook.
 *
 * Unlike a chat turn, which answers in the response, a deep question starts a
 * long-running task: `POST /api/v1/deep_consult` returns a task id at once, the
 * console polls `GET /api/v1/deep_consult/{task_id}` until it is completed or
 * failed, and the answer is a stored finding (plus optional facts and
 * hypotheses) that can be walked back to its sources.
 *
 * The hook owns the task, its polling and the list of past tasks; the Consult
 * composer owns the question box and calls `submit`. A past task can be
 * reopened (and polled again if it is still running).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getDeepConsultStatus,
  listConsultSessions,
  loadConsultSession,
  submitDeepConsult,
  type ConsultModel,
  type ConsultSessionSummary,
  type DeepConsultStatus,
} from '@/lib/api'
import type { Selection } from '@/state/selection'
import { questionWithPins } from '@/lib/consultContext'
import { formatApiError } from '@/state/useConsultTurn'

const POLL_INTERVAL_MS = 3000

export interface DeepSubmit {
  question: string
  scope: string
  emitFacts: boolean
  emitHypotheses: boolean
}

export function useDeepConsult(pins: readonly Selection[], model: ConsultModel, active: boolean) {
  const [statusResp, setStatusResp] = useState<DeepConsultStatus | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [tasks, setTasks] = useState<ConsultSessionSummary[]>([])
  const [historyError, setHistoryError] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])
  useEffect(() => stopPolling, [stopPolling])

  const loadHistory = useCallback(async () => {
    setHistoryError(null)
    try {
      setTasks(await listConsultSessions({ mode: 'deep', limit: 50 }))
    } catch (err) {
      setHistoryError(formatApiError(err))
    }
  }, [])

  // The past-task list is read only once deep mode is first used.
  useEffect(() => {
    if (active) void loadHistory()
  }, [active, loadHistory])

  const poll = useCallback(
    async (id: string) => {
      try {
        const s = await getDeepConsultStatus(id)
        setStatusResp(s)
        if (s.status === 'completed' || s.status === 'failed') {
          stopPolling()
          void loadHistory()
        }
      } catch (err) {
        // A poll can fail transiently: keep polling and show the last error.
        setError(formatApiError(err))
      }
    },
    [loadHistory, stopPolling],
  )

  const submit = useCallback(
    async (req: DeepSubmit): Promise<boolean> => {
      const trimmed = req.question.trim()
      if (!trimmed || submitting) return false
      setError(null)
      setSubmitting(true)
      setStatusResp(null)
      stopPolling()
      try {
        const resp = await submitDeepConsult({
          question: questionWithPins(trimmed, pins as Selection[]),
          scope_predicate: req.scope.trim() || null,
          emit_facts: req.emitFacts,
          emit_hypotheses: req.emitHypotheses,
          model,
        })
        setStatusResp({ task_id: resp.task_id, status: 'running', cited_refs: [], fact_ids: [], hypothesis_ids: [] })
        void loadHistory()
        pollRef.current = setInterval(() => void poll(resp.task_id), POLL_INTERVAL_MS)
        return true
      } catch (err) {
        setError(formatApiError(err))
        return false
      } finally {
        setSubmitting(false)
      }
    },
    [submitting, pins, model, loadHistory, poll, stopPolling],
  )

  // Reopen a past task: show its status, and resume polling while it runs.
  const openTask = useCallback(
    async (taskId: string) => {
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
        // The live status can fail for an old task; fall back to its stored turns.
        try {
          const session = tasks.find((t) => t.task_id === taskId)
          if (!session) {
            setError(formatApiError(err))
            return
          }
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
        } catch (inner) {
          setError(formatApiError(inner))
        }
      }
    },
    [submitting, tasks, poll, stopPolling],
  )

  return { statusResp, submitting, error, tasks, historyError, loadHistory, submit, openTask }
}
