/**
 * S4b / UI-5 (Tier E). Optimizer Prompt-Module Diff (`system.optimizer.diff`).
 *
 * Candidate-vs-current prompt-module review. Before an operator promotes a
 * GEPA candidate from the Optimizer Candidates queue, they need to see *what
 * actually changed* in the prompt module — not just the eval delta.
 *
 * Reads `GET /api/v1/v3/optimizer/candidates/{id}/diff` →
 *   { candidate_id, analyst_id, current_module_path, candidate_module_path,
 *     current_text, candidate_text, eval_score, eval_score_delta }
 *
 * The `/diff` route IS wired (v3_api.py, snapshot-based — no dspy import). It
 * builds `current_text` from the candidate's `parent_prompt_module_text`
 * snapshot (the baseline its delta was measured against), preferring the
 * analyst's live promoted prompt when one exists. A 404 (unknown / non-existent
 * candidate id) still renders a clean note rather than a raw error; other
 * failures surface as errors. Candidates emitted before the snapshot field
 * existed return an empty `current_text` (the candidate side still renders).
 *
 * Candidate selection:
 *   - the `open_panel` that opened this drill (the Optimizer Candidates queue
 *     "diff" button), whose `candidate_id` param reaches the tile through
 *     `state/panelParams` — on first open and while mounted alike (UI-B1/shell
 *     retired the DOM event and the `window` park), OR
 *   - `data_query.candidate_id` on the registration, OR
 *   - a manual candidate-id input (operator escape hatch).
 *
 * Line-level LCS diff lives in `@/lib/evalOps::diffLines` (unit-tested).
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, ApiError } from '@/lib/api'
import { diffLines, diffStat, type PromptModuleDiff } from '@/lib/evalOps'
import type { PanelProps } from '@/types'
import { usePanelOpenParams } from '@/state/panelParams'

export default function OptimizerDiffPanel({ registration }: PanelProps) {
  // Initial candidate: the registration's deep-link; then whatever the
  // `open_panel` that opened (or re-focused) this drill carried.
  const [candidateId, setCandidateId] = useState(
    () => (registration.data_query?.candidate_id as string | undefined) ?? '',
  )
  const [draft, setDraft] = useState(candidateId)
  usePanelOpenParams('system.optimizer.diff', (params) => {
    const id = typeof params.candidate_id === 'string' ? params.candidate_id : ''
    if (id) {
      setCandidateId(id)
      setDraft(id)
    }
  })

  const enabled = candidateId.trim().length > 0

  const { data, isLoading, error, refetch } = useQuery<PromptModuleDiff>({
    enabled,
    queryKey: ['optimizer-diff', candidateId],
    queryFn: () =>
      apiGet<PromptModuleDiff>(
        `/v3/optimizer/candidates/${encodeURIComponent(candidateId)}/diff`,
      ),
  })

  const lines = useMemo(
    () => (data ? diffLines(data.current_text, data.candidate_text) : []),
    [data],
  )
  const stat = useMemo(() => diffStat(lines), [lines])

  const delta = data?.eval_score_delta ?? 0
  const deltaColor =
    delta > 0 ? 'text-emerald-400' : delta < 0 ? 'text-rose-400' : 'text-slate-400'

  return (
    <PanelChrome
      registration={registration}
      subtitle={
        data
          ? `${data.analyst_id} · +${stat.added}/-${stat.deleted}`
          : 'select a candidate'
      }
      onRefresh={() => refetch()}
    >
      <form
        className="flex items-center gap-2 mb-2 text-xs"
        onSubmit={(e) => {
          e.preventDefault()
          setCandidateId(draft.trim())
        }}
      >
        <input
          className="flex-1 bg-surface-200 border border-slate-700 rounded p-1 px-2 font-mono"
          placeholder="candidate_id…"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          data-testid="optdiff-id-input"
        />
        <button
          type="submit"
          className="bg-surface-200 hover:bg-surface-300 border border-slate-700 rounded px-2 py-1"
          data-testid="optdiff-load"
        >
          load diff
        </button>
      </form>

      {!enabled && (
        <div className="text-slate-500 text-center py-4 text-sm" data-testid="optdiff-empty">
          no candidate selected — open from the Optimizer Candidates queue, or
          paste a candidate_id above
        </div>
      )}
      {isLoading && <div className="text-slate-500 text-sm">loading diff…</div>}
      {error instanceof ApiError && error.status === 404 ? (
        <div className="text-slate-400 text-sm py-4 text-center" data-testid="optdiff-pending">
          unknown candidate id — pick one from the Optimizer Candidates queue
          (promote/reject still works there directly)
        </div>
      ) : (
        error instanceof Error && (
          <div className="text-rose-400 text-sm">error: {error.message}</div>
        )
      )}

      {data && (
        <>
          <div className="bg-surface-100 border border-slate-800 rounded p-2 mb-2 text-[11px] space-y-1">
            <div className="flex items-center gap-3">
              <span className="text-slate-500">eval</span>
              <span className="font-mono text-slate-300">
                {(data.eval_score * 100).toFixed(1)}%
              </span>
              <span className={`font-mono ${deltaColor}`}>
                Δ {delta >= 0 ? '+' : ''}
                {(delta * 100).toFixed(1)}%
              </span>
              <span className="ml-auto text-emerald-400 font-mono">+{stat.added}</span>
              <span className="text-rose-400 font-mono">-{stat.deleted}</span>
            </div>
            <div className="text-slate-600 font-mono truncate">
              current: {data.current_module_path}
            </div>
            <div className="text-slate-600 font-mono truncate">
              candidate: {data.candidate_module_path}
            </div>
          </div>

          <div
            className="flex-1 overflow-auto bg-surface-50 border border-slate-800 rounded font-mono text-[11px] leading-relaxed"
            data-testid="optdiff-body"
          >
            {lines.map((l, i) => {
              const bg =
                l.op === 'add'
                  ? 'bg-emerald-950/60'
                  : l.op === 'del'
                    ? 'bg-rose-950/60'
                    : ''
              const sign = l.op === 'add' ? '+' : l.op === 'del' ? '-' : ' '
              const fg =
                l.op === 'add'
                  ? 'text-emerald-300'
                  : l.op === 'del'
                    ? 'text-rose-300'
                    : 'text-slate-400'
              return (
                <div
                  key={i}
                  className={`flex gap-2 px-2 ${bg}`}
                  data-testid={`optdiff-line-${l.op}`}
                >
                  <span className="w-8 shrink-0 text-right text-slate-700 select-none">
                    {l.oldNo ?? ''}
                  </span>
                  <span className="w-8 shrink-0 text-right text-slate-700 select-none">
                    {l.newNo ?? ''}
                  </span>
                  <span className={`shrink-0 select-none ${fg}`}>{sign}</span>
                  <span className={`whitespace-pre-wrap break-all ${fg}`}>{l.text}</span>
                </div>
              )
            })}
            {lines.length === 0 && (
              <div className="text-slate-600 p-2">modules are identical</div>
            )}
          </div>
        </>
      )}
    </PanelChrome>
  )
}
