/**
 * P2-5. Correctness Gold Set (`system.goldset`) — the weekly labeling worksheet.
 *
 * The correctness-vs-reference gold set only grows if labeling is cheap: this
 * panel shows the week's deterministic, server-pinned sample (~8 verified
 * findings, stratified per unit + faithfulness band) as a simple card list —
 * finding title → cited read (the CitedProse reading kit, same as the Findings
 * panels) → four verdict buttons + optional rationale → saved state. Verdicts
 * upsert via `POST /v3/eval/goldset/label` (the server snapshots what was
 * judged) and flow straight into the eval scoreboard's per-unit
 * `operator … (n=…)` badge segment — n grows with every label.
 *
 * Honest states: an exhausted week says "all labeled — next sample Monday";
 * a week with no verified candidates says so. All progress / verdict logic is
 * DOM-free in `@/lib/goldsetModel` (vitest-covered); this file is rendering +
 * wiring only. Personal-only; Operations (Operate) nav group.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { CheckCircle2 } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import CitedProse from '@/components/CitedProse'
import { InfoTip } from '@/components/InfoTip'
import { RecordLink } from '@/components/inspector/RecordLink'
import { extractCitations } from '@/lib/citationsModel'
import { relTime } from '@/lib/evalOps'
import { FAITHFULNESS_EXPLAIN } from '@/lib/verdictModel'
import {
  emptyStateMessage,
  fetchGoldsetWorksheet,
  postGoldsetLabel,
  VERDICT_OPTIONS,
  worksheetProgress,
  type GoldsetVerdict,
  type GoldsetWorksheet,
  type GoldsetWorksheetItem,
} from '@/lib/goldsetModel'
import type { PanelProps } from '@/types'

/** Verdict-tone → selected-button pill classes (the EvalScorecard palette). */
const TONE_SELECTED: Record<string, string> = {
  good: 'bg-emerald-900 text-emerald-200 border-emerald-700',
  warn: 'bg-amber-900 text-amber-200 border-amber-700',
  bad: 'bg-rose-900 text-rose-200 border-rose-700',
  muted: 'bg-slate-800 text-slate-300 border-slate-600',
}

function VerdictButtons({
  item,
  rationale,
  saving,
  onPick,
}: {
  item: GoldsetWorksheetItem
  rationale: string
  saving: boolean
  onPick: (verdict: GoldsetVerdict) => void
}) {
  const current = item.label?.label ?? null
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {VERDICT_OPTIONS.map((o) => {
        const selected = current === o.value
        return (
          <button
            key={o.value}
            type="button"
            disabled={saving}
            title={o.hint}
            onClick={() => onPick(o.value)}
            data-testid={`goldset-verdict-${item.finding_id}-${o.value}`}
            className={`rounded border px-2 py-0.5 text-[11px] disabled:opacity-50 ${
              selected
                ? TONE_SELECTED[o.tone]
                : 'border-slate-700 bg-surface-200 text-slate-300 hover:bg-surface-100'
            }`}
          >
            {o.label}
          </button>
        )
      })}
      {saving && <span className="text-[10px] text-slate-500">saving…</span>}
      {!saving && item.label && (
        <span
          className="inline-flex items-center gap-1 text-[10px] text-emerald-400"
          data-testid={`goldset-saved-${item.finding_id}`}
        >
          <CheckCircle2 className="h-3 w-3" aria-hidden />
          saved {relTime(item.label.labeled_at)}
          {item.label.labeled_by ? ` by ${item.label.labeled_by}` : ''}
        </span>
      )}
      {rationale !== (item.label?.rationale ?? '') && (
        <span className="text-[10px] text-slate-500">rationale unsaved — pick a verdict to save</span>
      )}
    </div>
  )
}

export default function GoldsetPanel({ registration }: PanelProps) {
  const qc = useQueryClient()
  // Per-finding rationale drafts (seeded from the saved label on first edit).
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [savingId, setSavingId] = useState<string | null>(null)

  const { data, isLoading, error, refetch } = useQuery<GoldsetWorksheet>({
    queryKey: ['goldset-worksheet'],
    queryFn: fetchGoldsetWorksheet,
    refetchInterval: 300_000, // the sample is week-pinned; no need to poll hot
  })

  const label = useMutation({
    mutationFn: (vars: { finding_id: string; label: GoldsetVerdict; rationale?: string | null }) =>
      postGoldsetLabel(vars),
    onMutate: (vars) => setSavingId(vars.finding_id),
    onSettled: () => {
      setSavingId(null)
      qc.invalidateQueries({ queryKey: ['goldset-worksheet'] })
    },
  })

  const progress = worksheetProgress(data)
  const empty = emptyStateMessage(data)

  const rationaleOf = (item: GoldsetWorksheetItem): string =>
    drafts[item.finding_id] ?? item.label?.rationale ?? ''

  return (
    <PanelChrome
      registration={registration}
      subtitle={
        data
          ? `week ${data.week} · ${progress.labeled}/${progress.total} labeled`
          : 'weekly correctness labeling'
      }
      onRefresh={() => refetch()}
    >
      {isLoading && <div className="text-sm text-slate-500">loading worksheet…</div>}
      {error instanceof Error && (
        <div className="text-sm text-rose-400">error: {error.message}</div>
      )}
      {label.error instanceof Error && (
        <div className="text-xs text-rose-400" data-testid="goldset-save-error">
          save failed: {label.error.message}
        </div>
      )}

      {/* The honest exhausted / no-candidates state. */}
      {empty && (
        <div
          className="rounded border border-slate-800 bg-surface-100 p-4 text-center text-sm text-slate-400"
          data-testid="goldset-empty"
        >
          {empty}
        </div>
      )}

      <div className="flex-1 space-y-2 overflow-auto" data-testid="goldset-list">
        {(data?.items ?? []).map((item) => (
          <div
            key={item.finding_id}
            className="rounded border border-slate-800 bg-surface-100 p-2.5 space-y-2"
            data-testid={`goldset-card-${item.finding_id}`}
          >
            {/* Header: unit + target + faithfulness + honest supersession flag. */}
            <div className="flex flex-wrap items-baseline gap-2 text-[10px]">
              <span className="rounded bg-surf-3 px-1 font-mono text-accent-info">{item.unit}</span>
              {item.target_id && (
                <RecordLink
                  kind="target"
                  id={item.target_id}
                  label={item.target_id}
                  origin="goldset"
                  className="text-[10px]"
                />
              )}
              {item.faithfulness !== null && (
                <InfoTip
                  text={`${FAITHFULNESS_EXPLAIN} This read: ${item.faithfulness.toFixed(2)}.`}
                  className="font-mono text-slate-500"
                  testId={`goldset-faith-${item.finding_id}`}
                >
                  faith {item.faithfulness.toFixed(2)}
                </InfoTip>
              )}
              {item.superseded && (
                <span
                  className="rounded bg-amber-950/40 px-1 text-amber-300"
                  title="superseded after this week's sample was drawn — judge the read as sampled, or mark unresolvable"
                >
                  superseded
                </span>
              )}
              <span className="ml-auto text-slate-600">{relTime(item.produced_at)}</span>
            </div>

            {/* Title + the cited read (the shared reading kit). */}
            <div className="text-[13px] font-medium leading-snug text-slate-200">
              {item.title}
            </div>
            <CitedProse text={item.body} citations={extractCitations(item.data ?? undefined)} />

            {/* Optional rationale + the four verdict buttons. */}
            <textarea
              value={rationaleOf(item)}
              onChange={(e) =>
                setDrafts((d) => ({ ...d, [item.finding_id]: e.target.value }))
              }
              placeholder="optional rationale (why this verdict)"
              rows={1}
              className="w-full resize-y rounded border border-slate-800 bg-surface-200 px-2 py-1 text-[11px] text-slate-300 placeholder:text-slate-600"
              data-testid={`goldset-rationale-${item.finding_id}`}
            />
            <VerdictButtons
              item={item}
              rationale={rationaleOf(item)}
              saving={savingId === item.finding_id}
              onPick={(verdict) =>
                label.mutate({
                  finding_id: item.finding_id,
                  label: verdict,
                  rationale: rationaleOf(item) || null,
                })
              }
            />
          </div>
        ))}
      </div>
    </PanelChrome>
  )
}
