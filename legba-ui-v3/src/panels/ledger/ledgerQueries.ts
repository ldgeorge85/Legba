/**
 * The Ledger page's reads (U-11) — one query key per resource, shared with
 * every other reader of the same route (the globe's forecast layer reads
 * `ledger-forecasts`; the old Scorecard view read `ledger-scorecard`).
 *
 * A 404 is an ANSWER here (the route is not served by this installation),
 * returned as `null`, never retried and never an error; anything else that
 * fails is the query's error. Light reads only: no route is polled faster
 * than once a minute, and the anchors are read once per page view.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiGet, apiPostWithStatus, ApiError } from '@/lib/api'
import { withAt } from '@/lib/asOf'
import { LEDGER_ROUTE, type LedgerForecastsResponse } from '@/lib/ledgerModel'
import { SCORECARD_ROUTE, type ScorecardResponse } from '@/lib/scorecardModel'
import {
  ANCHORS_ROUTE,
  EVAL_RUNS_ROUTE,
  QUEUE_ROUTE,
  RECORDS_ROUTE,
  SEAL_PROOF_ROUTE,
  decisionBody,
  decisionPath,
  type AnchorsResponse,
  type Decision,
  type DecisionResult,
  type EvalRunsResponse,
  type QueueResponse,
  type ResolutionRecordsResponse,
  type SealProofResponse,
} from '@/lib/ledgerProof'

async function orNull<T>(path: string): Promise<T | null> {
  try {
    return await apiGet<T>(path)
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null
    throw e
  }
}

const noRetry404 = (n: number, e: unknown) => !(e instanceof ApiError && e.status === 404) && n < 2

/**
 * The ledger now, or — with an instant (`lib/asOf.ts`) — the ledger as it
 * stood then (`?at=`), under the key the time band's past ticks read too.
 * A past ledger does not change, so it is not polled.
 */
export function useLedgerForecasts(atMs: number | null = null) {
  return useQuery<LedgerForecastsResponse>({
    queryKey: atMs === null ? ['ledger-forecasts'] : ['ledger-forecasts', 'at', atMs],
    queryFn: () => apiGet<LedgerForecastsResponse>(withAt(LEDGER_ROUTE, atMs)),
    refetchInterval: atMs === null ? 60_000 : false,
    staleTime: atMs === null ? undefined : Infinity,
    retry: noRetry404,
  })
}

export function useLedgerScorecard() {
  return useQuery<ScorecardResponse | null>({
    queryKey: ['ledger-scorecard'],
    queryFn: () => orNull<ScorecardResponse>(SCORECARD_ROUTE),
    refetchInterval: 60_000,
  })
}

export function useReceiptAnchors() {
  return useQuery<AnchorsResponse | null>({
    queryKey: ['receipt-anchors', 60],
    queryFn: () => orNull<AnchorsResponse>(ANCHORS_ROUTE),
    staleTime: 10 * 60_000,
  })
}

export function useEvalRuns() {
  return useQuery<EvalRunsResponse | null>({
    queryKey: ['eval-runs'],
    queryFn: () => orNull<EvalRunsResponse>(EVAL_RUNS_ROUTE),
    staleTime: 10 * 60_000,
  })
}

/** F-1: what waits on THIS person (null: the registry does not serve the route). */
export function useResolutionQueue() {
  return useQuery<QueueResponse | null>({
    queryKey: ['ledger-resolution-queue'],
    queryFn: () => orNull<QueueResponse>(QUEUE_ROUTE),
    refetchInterval: 60_000,
    retry: (n, e) => !(e instanceof ApiError && (e.status === 403 || e.status === 404)) && n < 2,
  })
}

/** F-1: a seal's inclusion proof, read only when the reader opens the seal. */
export function useSealProof(questionId: string | null, enabled: boolean) {
  return useQuery<SealProofResponse | null>({
    queryKey: ['ledger-seal-proof', questionId],
    queryFn: () => orNull<SealProofResponse>(`${SEAL_PROOF_ROUTE}?question_id=${encodeURIComponent(questionId ?? '')}`),
    enabled: enabled && !!questionId,
    staleTime: 10 * 60_000,
    retry: noRetry404,
  })
}

/**
 * F-1: send one decision. A refusal comes back as the route's own words; on
 * success every read the decision changes is re-read (the queue, the
 * scorecard — the fifth confirmation opens it — the forecasts, the record).
 */
export function useDecide() {
  const qc = useQueryClient()
  return useMutation<DecisionResult, ApiError, { recordId: string; questionId: string; decision: Decision }>({
    mutationFn: async ({ recordId, decision }) => (await apiPostWithStatus<DecisionResult>(decisionPath(recordId, decision.verb), decisionBody(decision))).body,
    onSuccess: (_r, { questionId }) => {
      for (const key of [['ledger-resolution-queue'], ['ledger-scorecard'], ['ledger-forecasts'], ['ledger-resolution-records', questionId]]) {
        void qc.invalidateQueries({ queryKey: key })
      }
    },
  })
}

export function useResolutionRecords(questionId: string | null) {
  return useQuery<ResolutionRecordsResponse | null>({
    queryKey: ['ledger-resolution-records', questionId],
    queryFn: () => orNull<ResolutionRecordsResponse>(`${RECORDS_ROUTE}?question_id=${encodeURIComponent(questionId ?? '')}`),
    enabled: !!questionId,
    staleTime: 60_000,
  })
}
