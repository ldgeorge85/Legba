/**
 * S4. Optimizer Candidates Queue (`system.optimizer`).
 *
 * Reads `GET /api/v1/v3/optimizer/candidates?state=pending|rejected|all` —
 * surfaces `analyst_outputs` rows with `kind='prompt_module_candidate'`. The
 * candidate `state` is derived from `promotion_gate` and is only ever
 * `'pending'` or `'rejected'` (a `'promoted'` candidate becomes a new analyst
 * descriptor version, so it leaves the queue — see L-176).
 *
 * Promote/reject mutation (the optimizer runs on Dapr Workflow):
 *   POST /api/v1/v3/optimizer/candidates/{id}/review
 *     { action: 'promote' | 'reject', reviewer: string, note?: string }
 *   → { candidate_id, action, analyst_id, new_descriptor_version, promotion_gate }
 *
 * Promotion flips the analyst descriptor's active prompt-module path (minting a
 * new content-hash version); the audit log captures the operator action (per
 * L-176 + RUNBOOK §11).
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import {
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, apiPost } from '@/lib/api'
import type { PanelProps } from '@/types'

// The backend derives candidate `state` from `promotion_gate`; a promoted
// candidate becomes a new descriptor version and leaves the queue, so only
// 'pending' and 'rejected' are ever returned (L-176).
const STATE_COLOR: Record<'pending' | 'rejected', string> = {
  pending: '#fbbf24', // amber-400
  rejected: '#fb7185', // rose-400
}

interface OptimizerCandidate {
  id: string
  analyst_id: string
  analyst_version: string
  parent_prompt_module_path: string
  eval_score: number
  eval_score_delta: number
  training_set_size: number
  gepa_generation: number
  promotion_gate: string
  state: 'pending' | 'rejected'
  /** Dapr Workflow instance id (backend field name retained from migration). */
  temporal_workflow_id: string | null
  produced_at: string
  /**
   * Real method the GEPA workflow took: dspy_gepa | naive_best_of_n |
   * noop_empty_training | skipped_validation | unknown. A non-`dspy_gepa`
   * value means a fallback path ran (e.g. a worker-less deploy silently runs
   * the naive instruction search), which the UI flags so operators don't
   * assume every candidate came from the full GEPA loop.
   */
  method: string
}

interface OptimizerReviewResult {
  candidate_id: string
  action: 'promote' | 'reject'
  analyst_id: string
  new_descriptor_version: string | null
  promotion_gate: 'promoted' | 'rejected'
}

type StateFilter = 'pending' | 'rejected' | 'all'

/**
 * Cross-panel deep-link contract for the Prompt-Module Diff panel.
 *
 * `dispatchEvent` alone is fire-and-forget: if the diff panel is NOT already
 * mounted (the common case — an operator clicks "view diff" before that panel
 * has ever been opened) the event lands with no listener and the request is
 * silently lost, so the panel opens empty. We therefore ALSO stash the
 * requested candidate on `window` as a durable, replayable "pending" slot,
 * which `OptimizerDiff` drains on mount — the first open then loads the right
 * candidate even though it missed the live event. Both sides share this event
 * name + window key (kept in sync by string literal; the central panel-open
 * bridge that actually materialises the panel is the integrator's — see the
 * central-changes note).
 */
const OPEN_DIFF_EVENT = 'legba:open-optimizer-diff'
const PENDING_DIFF_KEY = '__legbaPendingOptimizerDiff'

/**
 * Fire the cross-panel deep-link AND record it as the pending request so the
 * diff panel can pick it up on first mount (see {@link OPEN_DIFF_EVENT}).
 */
function requestOptimizerDiff(candidateId: string) {
  ;(window as unknown as Record<string, string>)[PENDING_DIFF_KEY] = candidateId
  window.dispatchEvent(
    new CustomEvent(OPEN_DIFF_EVENT, { detail: { candidate_id: candidateId } }),
  )
}

/**
 * One-line change summary for a candidate row: what promoting it would do to
 * the analyst's live prompt module, framed by the eval delta and the GEPA
 * method that produced it — so an operator can read the row's intent without
 * opening the diff.
 */
function changeSummary(c: OptimizerCandidate): string {
  const dir =
    c.eval_score_delta > 0
      ? 'improves'
      : c.eval_score_delta < 0
        ? 'regresses'
        : 'leaves unchanged'
  const pct = `${Math.abs(c.eval_score_delta * 100).toFixed(1)}%`
  const via =
    c.method === 'dspy_gepa'
      ? `GEPA gen ${c.gepa_generation}`
      : `fallback (${c.method})`
  return `Rewrites ${c.analyst_id}'s prompt module — ${dir} eval by ${pct} (${via}, ${c.training_set_size} traces).`
}

export default function OptimizerPanel({ registration }: PanelProps) {
  const qc = useQueryClient()
  const [stateFilter, setStateFilter] = useState<StateFilter>('pending')
  const [pendingAction, setPendingAction] = useState<string | null>(null)

  const { data, isLoading, error, refetch } = useQuery<OptimizerCandidate[]>({
    queryKey: ['optimizer-candidates', stateFilter],
    queryFn: () => apiGet<OptimizerCandidate[]>(`/v3/optimizer/candidates?state=${stateFilter}`),
    refetchInterval: 30_000,
  })

  // Reviewer principal stamped on the audit row. Falls back to a sentinel when
  // the JWT subject isn't decodable client-side.
  const reviewer = useMemo(() => {
    try {
      const tok = localStorage.getItem('legba_token')
      if (tok) {
        const claims = JSON.parse(atob(tok.split('.')[1])) as { sub?: string }
        if (claims.sub) return claims.sub
      }
    } catch {
      /* fall through */
    }
    return 'operator'
  }, [])

  const review = useMutation<
    OptimizerReviewResult,
    Error,
    { id: string; action: 'promote' | 'reject'; note?: string }
  >({
    mutationFn: ({ id, action, note }) =>
      apiPost<OptimizerReviewResult>(`/v3/optimizer/candidates/${id}/review`, {
        action,
        reviewer,
        note,
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['optimizer-candidates'] })
      setPendingAction(null)
      if (res.action === 'promote' && res.new_descriptor_version) {
        window.alert(
          `Promoted — ${res.analyst_id} now on prompt module @${res.new_descriptor_version.slice(0, 8)}`,
        )
      }
    },
    onError: (err) => {
      setPendingAction(null)
      window.alert(`Review failed: ${err.message}`)
    },
  })

  const rows = data ?? []

  /** Group candidates by state for the fitness-progression scatter. */
  const groupedByState = useMemo(() => {
    const out: Record<'pending' | 'rejected', Array<{
      gen: number
      score: number
      id: string
      analyst_id: string
    }>> = {
      pending: [],
      rejected: [],
    }
    for (const c of rows) {
      if (!(c.state in out)) continue
      out[c.state].push({
        gen: c.gepa_generation,
        score: c.eval_score,
        id: c.id,
        analyst_id: c.analyst_id,
      })
    }
    return out
  }, [rows])

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${rows.length} candidates (${stateFilter})`}
      onRefresh={() => refetch()}
    >
      <div className="flex items-center gap-2 mb-2 text-xs">
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={stateFilter}
          onChange={(e) => setStateFilter(e.target.value as StateFilter)}
        >
          <option value="pending">pending</option>
          <option value="rejected">rejected</option>
          <option value="all">all</option>
        </select>
        {isLoading && <span className="text-slate-500">loading…</span>}
      </div>

      {error instanceof Error && (
        <div className="text-rose-400 text-sm">error: {error.message}</div>
      )}

      {/* Fitness progression scatter (Pass 4) */}
      {rows.length > 0 && (
        <div className="bg-surface-100 border border-slate-800 rounded p-2 h-48 mb-2">
          <ResponsiveContainer width="100%" height="100%">
            <ScatterChart margin={{ top: 4, right: 8, bottom: 16, left: 0 }}>
              <CartesianGrid
                strokeDasharray="3 3"
                stroke="#334155"
                opacity={0.4}
              />
              <XAxis
                type="number"
                dataKey="gen"
                name="generation"
                stroke="#94a3b8"
                fontSize={10}
                label={{
                  value: 'gepa_generation',
                  position: 'insideBottom',
                  offset: -8,
                  fill: '#64748b',
                  fontSize: 10,
                }}
                allowDecimals={false}
              />
              <YAxis
                type="number"
                dataKey="score"
                name="eval_score"
                stroke="#94a3b8"
                fontSize={10}
                domain={[0, 1]}
                tickFormatter={(v: number) => `${(v * 100).toFixed(0)}%`}
                width={50}
              />
              <Tooltip
                cursor={{ strokeDasharray: '3 3', stroke: '#475569' }}
                contentStyle={{
                  background: '#1e293b',
                  border: '1px solid #334155',
                  borderRadius: 4,
                  fontSize: 11,
                }}
                labelStyle={{ color: '#cbd5e1' }}
                formatter={(value: unknown, name: string) => {
                  if (name === 'eval_score' && typeof value === 'number') {
                    return [`${(value * 100).toFixed(1)}%`, name]
                  }
                  return [String(value), name]
                }}
              />
              <Legend
                wrapperStyle={{ fontSize: 10, color: '#94a3b8' }}
                iconSize={8}
              />
              {(['pending', 'rejected'] as const).map((s) =>
                groupedByState[s].length > 0 ? (
                  <Scatter
                    key={s}
                    name={s}
                    data={groupedByState[s]}
                    fill={STATE_COLOR[s]}
                  />
                ) : null,
              )}
            </ScatterChart>
          </ResponsiveContainer>
        </div>
      )}

      <div className="flex-1 overflow-auto space-y-2 text-xs">
        {rows.length === 0 && !isLoading && (
          <div className="text-slate-500 text-center py-4">
            no {stateFilter} candidates — optimizer hasn't fired or all reviewed
          </div>
        )}
        {rows.map((c) => {
          const deltaSign = c.eval_score_delta >= 0 ? '+' : ''
          const deltaColor =
            c.eval_score_delta > 0
              ? 'text-emerald-400'
              : c.eval_score_delta < 0
                ? 'text-rose-400'
                : 'text-slate-400'
          const busy = pendingAction === c.id || review.isPending
          return (
            <div key={c.id} className="bg-surface-100 border border-slate-800 rounded p-2">
              <div className="flex items-baseline gap-2 mb-1">
                <span
                  className={`shrink-0 rounded px-1 text-[10px] ${
                    c.state === 'pending'
                      ? 'bg-amber-900 text-amber-200'
                      : 'bg-rose-900 text-rose-200'
                  }`}
                >
                  {c.state}
                </span>
                <span className="text-slate-200 truncate flex-1">{c.analyst_id}</span>
                <span className={`font-mono ${deltaColor}`}>
                  Δ {deltaSign}{(c.eval_score_delta * 100).toFixed(1)}%
                </span>
                <span className="text-slate-500 font-mono shrink-0">
                  eval {(c.eval_score * 100).toFixed(1)}%
                </span>
              </div>
              <div className="text-slate-500 mt-1 flex flex-wrap gap-3 items-center">
                <span>gen {c.gepa_generation}</span>
                <span>{c.training_set_size} traces</span>
                <span>gate: {c.promotion_gate}</span>
                <MethodFlag method={c.method} />
                <span className="text-slate-600">{new Date(c.produced_at).toLocaleString()}</span>
              </div>
              {/* What this candidate would actually change, in one line. */}
              <div className="text-slate-400 text-[11px] mt-1 leading-snug">
                {changeSummary(c)}
              </div>
              <div className="text-slate-600 text-[10px] mt-1 font-mono truncate">
                parent: {c.parent_prompt_module_path}
              </div>
              {c.temporal_workflow_id && (
                <div className="text-slate-600 text-[10px] font-mono truncate">
                  workflow: {c.temporal_workflow_id}
                </div>
              )}
              {c.state === 'rejected' && (
                <div className="text-rose-400/70 text-[10px] mt-1">rejected — left the queue</div>
              )}
              {/* UI-5: open the candidate-vs-current prompt-module diff. The
                  helper both fires the cross-panel event AND parks the request
                  on `window` so the diff panel loads this candidate even on its
                  first open (when no listener was mounted yet). */}
              <div className="mt-1">
                <button
                  onClick={() => requestOptimizerDiff(c.id)}
                  className="text-[10px] text-accent-info hover:text-blue-300 underline"
                  data-testid={`optimizer-diff-${c.id}`}
                  title="View what this candidate changes — opens the Prompt-Module Diff panel"
                >
                  view prompt-module diff →
                </button>
              </div>
              {c.state === 'pending' && (
                <div className="flex items-center gap-2 mt-2">
                  <button
                    disabled={busy}
                    data-testid={`optimizer-promote-${c.id}`}
                    onClick={() => {
                      if (
                        !window.confirm(
                          `Promote this candidate? ${c.analyst_id}'s active prompt module ` +
                            `will be replaced (new descriptor version + audit row).`,
                        )
                      )
                        return
                      setPendingAction(c.id)
                      review.mutate({ id: c.id, action: 'promote' })
                    }}
                    className="flex-1 bg-emerald-900 hover:bg-emerald-800 disabled:opacity-50 text-emerald-200 rounded p-1 text-xs"
                  >
                    {busy ? '…' : 'Promote'}
                  </button>
                  <button
                    disabled={busy}
                    data-testid={`optimizer-reject-${c.id}`}
                    onClick={() => {
                      const note = window.prompt('Rejection reason (optional)') ?? undefined
                      setPendingAction(c.id)
                      review.mutate({ id: c.id, action: 'reject', note: note || undefined })
                    }}
                    className="flex-1 bg-rose-900 hover:bg-rose-800 disabled:opacity-50 text-rose-200 rounded p-1 text-xs"
                  >
                    {busy ? '…' : 'Reject'}
                  </button>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </PanelChrome>
  )
}

/**
 * Method flag for one candidate. `dspy_gepa` (the full GEPA loop) is the
 * expected path and renders neutrally; any fallback (naive_best_of_n,
 * noop_empty_training, skipped_validation, unknown) renders amber so an
 * operator can tell at a glance the candidate did NOT come from the full loop
 * (e.g. a worker-less deploy silently runs the naive instruction search).
 */
function MethodFlag({ method }: { method: string }) {
  const isGepa = method === 'dspy_gepa'
  return (
    <span
      className={`rounded px-1 text-[10px] font-mono ${
        isGepa ? 'text-slate-500' : 'bg-amber-900 text-amber-200'
      }`}
      data-testid={`optimizer-method-${method}`}
      title={
        isGepa
          ? 'Full DSPy GEPA loop'
          : 'Fallback method — NOT the full GEPA loop (e.g. naive search on a worker-less deploy)'
      }
    >
      {isGepa ? 'method: dspy_gepa' : `⚠ ${method}`}
    </span>
  )
}
