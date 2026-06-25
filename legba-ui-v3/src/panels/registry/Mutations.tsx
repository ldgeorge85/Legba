/**
 * O5. Proposed-Mutations Queue (`registry.mutations`).
 *
 * legba_ui_panels_v2.md §3.4 O5 — the operator's review gate for
 * machine-proposed changes to the registry. Three mutation classes were
 * specced:
 *
 *   1. prompt-module promotions  — optimizer (GEPA) candidates that, if
 *      approved, flip an analyst descriptor's `method.prompt_module` to the
 *      higher-scoring module. This is the one class with a *live* backend:
 *        GET  /api/v1/v3/optimizer/candidates?state=pending|rejected|all
 *        POST /api/v1/v3/optimizer/candidates/{id}/review
 *             { action: 'promote'|'reject', reviewer, note? }
 *      Promote mints a new content-hash version of the parent analyst +
 *      writes a signed audit row (L-176 / RUNBOOK §11).
 *   2. proposed graph edges      — no backend feed under the source-first
 *   3. proposed entity merges      pivot (Nexus relationship/merge proposals
 *                                  aren't emitted as a reviewable queue yet),
 *                                  so those tabs declare the gap honestly
 *                                  rather than read a phantom endpoint.
 *
 * O5 is the *gate* view (decide + audit). The richer optimizer dashboard
 * (fitness-progression scatter, prompt-module diff) lives in `system.optimizer`
 * / `system.optimizer.diff`; the "view diff" link below hands off to it via
 * the shared `legba:open-optimizer-diff` window event.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, apiPost } from '@/lib/api'
import { getToken, tryDecodeClaims } from '@/auth/jwt'
import type { PanelProps } from '@/types'

type Tab = 'prompt_modules' | 'edges' | 'merges'

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
  temporal_workflow_id: string | null
  produced_at: string
}

interface ReviewResult {
  candidate_id: string
  action: 'promote' | 'reject'
  analyst_id: string
  new_descriptor_version: string | null
  promotion_gate: 'promoted' | 'rejected'
}

/** Resolve the reviewer principal for the audit row (falls back in dev). */
function reviewerPrincipal(): string {
  return tryDecodeClaims(getToken())?.sub ?? 'operator'
}

export default function RegistryMutationsPanel({ registration }: PanelProps) {
  const qc = useQueryClient()
  const [tab, setTab] = useState<Tab>('prompt_modules')
  const [pendingId, setPendingId] = useState<string | null>(null)

  const { data, isLoading, error, refetch } = useQuery<OptimizerCandidate[]>({
    queryKey: ['mutations-prompt-modules'],
    queryFn: () => apiGet<OptimizerCandidate[]>('/v3/optimizer/candidates?state=pending'),
    refetchInterval: 30_000,
  })

  const review = useMutation({
    mutationFn: ({ id, action, note }: { id: string; action: 'promote' | 'reject'; note?: string }) =>
      apiPost<ReviewResult>(`/v3/optimizer/candidates/${id}/review`, {
        action,
        reviewer: reviewerPrincipal(),
        note: note || undefined,
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['mutations-prompt-modules'] })
      // keep the optimizer panels in sync if they're open
      qc.invalidateQueries({ queryKey: ['optimizer-candidates'] })
      setPendingId(null)
      if (res.action === 'promote') {
        window.alert(
          `Promoted ${res.analyst_id} → ${res.new_descriptor_version?.slice(0, 16) ?? 'new version'}`,
        )
      }
    },
    onError: (err) => {
      setPendingId(null)
      window.alert(`Review failed: ${(err as Error).message}`)
    },
  })

  const candidates = data ?? []
  const counts = useMemo(
    () => ({ prompt_modules: candidates.length, edges: 0, merges: 0 }),
    [candidates.length],
  )

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${counts.prompt_modules} pending prompt-module promotion${counts.prompt_modules === 1 ? '' : 's'}`}
      onRefresh={() => refetch()}
    >
      {/* tab bar */}
      <div className="flex items-center gap-1 mb-2 text-xs border-b border-slate-800">
        {([
          ['prompt_modules', `prompt-modules (${counts.prompt_modules})`],
          ['edges', 'proposed edges'],
          ['merges', 'entity merges'],
        ] as Array<[Tab, string]>).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`px-2 py-1 rounded-t border-b-2 ${
              tab === key
                ? 'border-emerald-500 text-emerald-200'
                : 'border-transparent text-slate-400 hover:text-slate-200'
            }`}
            data-testid={`mutations-tab-${key}`}
          >
            {label}
          </button>
        ))}
        {isLoading && tab === 'prompt_modules' && <span className="text-slate-500 ml-2">loading…</span>}
      </div>

      {tab === 'prompt_modules' && (
        <>
          {error instanceof Error && (
            <div className="text-rose-400 text-sm">error: {error.message}</div>
          )}
          <div className="flex-1 overflow-auto space-y-2 text-xs" data-testid="mutations-prompt-modules">
            {candidates.length === 0 && !isLoading && (
              <div className="text-slate-500 text-center py-4">
                no pending promotions — the optimizer hasn't proposed a candidate or all are reviewed
              </div>
            )}
            {candidates.map((c) => {
              const deltaSign = c.eval_score_delta >= 0 ? '+' : ''
              const deltaColor =
                c.eval_score_delta > 0
                  ? 'text-emerald-400'
                  : c.eval_score_delta < 0
                    ? 'text-rose-400'
                    : 'text-slate-400'
              const busy = pendingId === c.id || review.isPending
              return (
                <div key={c.id} className="bg-surface-100 border border-slate-800 rounded p-2">
                  <div className="flex items-baseline gap-2 mb-1">
                    <span className="shrink-0 bg-amber-900 text-amber-200 rounded px-1 text-[10px]">
                      {c.promotion_gate}
                    </span>
                    <span className="text-slate-200 truncate flex-1">{c.analyst_id}</span>
                    <span className={`font-mono ${deltaColor}`}>
                      Δ {deltaSign}{(c.eval_score_delta * 100).toFixed(1)}%
                    </span>
                    <span className="text-slate-500 font-mono shrink-0">
                      eval {(c.eval_score * 100).toFixed(1)}%
                    </span>
                  </div>
                  <div className="text-slate-500 flex flex-wrap gap-3 text-[10px]">
                    <span>gen {c.gepa_generation}</span>
                    <span>{c.training_set_size} traces</span>
                    <span className="text-slate-600">{new Date(c.produced_at).toLocaleString()}</span>
                  </div>
                  <div className="text-slate-600 text-[10px] mt-1 font-mono truncate">
                    parent module: {c.parent_prompt_module_path}
                  </div>
                  <div className="mt-1">
                    <button
                      onClick={() =>
                        window.dispatchEvent(
                          new CustomEvent('legba:open-optimizer-diff', {
                            detail: { candidate_id: c.id },
                          }),
                        )
                      }
                      className="text-[10px] text-accent-info hover:text-blue-300 underline"
                      data-testid={`mutations-diff-${c.id}`}
                      title="View candidate vs current prompt-module diff"
                    >
                      view prompt-module diff →
                    </button>
                  </div>
                  <div className="flex items-center gap-2 mt-2">
                    <button
                      disabled={busy}
                      onClick={() => {
                        setPendingId(c.id)
                        review.mutate({ id: c.id, action: 'promote' })
                      }}
                      className="flex-1 bg-emerald-900 hover:bg-emerald-800 disabled:opacity-50 text-emerald-200 rounded p-1 text-xs"
                      data-testid={`mutations-promote-${c.id}`}
                    >
                      {busy ? '…' : 'Approve & promote'}
                    </button>
                    <button
                      disabled={busy}
                      onClick={() => {
                        const note = window.prompt('Rejection reason (optional)') ?? undefined
                        setPendingId(c.id)
                        review.mutate({ id: c.id, action: 'reject', note: note || undefined })
                      }}
                      className="flex-1 bg-rose-900 hover:bg-rose-800 disabled:opacity-50 text-rose-200 rounded p-1 text-xs"
                      data-testid={`mutations-reject-${c.id}`}
                    >
                      {busy ? '…' : 'Reject'}
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        </>
      )}

      {tab === 'edges' && (
        <NoFeed
          title="proposed graph edges"
          body="Nexus relationship proposals are written into the analyst-output substrate, not surfaced as a reviewable queue under the source-first pivot. When a proposed-edge feed lands (a /v3 endpoint emitting kind='proposed_edge'), this tab gains the side-by-side diff + approve/reject gate."
        />
      )}

      {tab === 'merges' && (
        <NoFeed
          title="proposed entity merges"
          body="Entity de-duplication runs as a deterministic analyst (alias/canonical) and does not collapse records into a human-gated merge queue. This tab activates if/when entity-merge proposals are emitted as a reviewable mutation class."
        />
      )}
    </PanelChrome>
  )
}

function NoFeed({ title, body }: { title: string; body: string }) {
  return (
    <div className="flex-1 overflow-auto text-xs">
      <div className="bg-accent-warning/10 border border-accent-warning/40 rounded p-3 text-slate-300 space-y-1">
        <div className="text-accent-warning font-semibold">{title} — no backend feed yet</div>
        <p>{body}</p>
        <p className="text-slate-500 text-[10px]">spec: legba_ui_panels_v2.md §3.4 O5</p>
      </div>
    </div>
  )
}
