/**
 * Scorecard — the sealed-ledger scorecard (ST1), a tab of `system.ledgers`.
 *
 * Reads `GET /api/v1/v3/ledger/scorecard`. Useful before any resolution
 * (sealed questions, each arm's number, spread, due dates, "0 resolved") and
 * never more confident than the ledger: below the minimum sample an arm reads
 * "n too small", calibration shows its reason, never a table of zeros.
 * Projections are `@/lib/scorecardModel` (unit-tested).
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, ApiError } from '@/lib/api'
import {
  NOT_DEPLOYED,
  SCORECARD_ROUTE,
  SEPARATION_LABEL,
  armRows,
  calibrationView,
  headerView,
  scoresWithheld,
  withheldLine,
  scorecardArmColumns,
  scorecardRows,
  scorecardState,
  type ScorecardResponse,
} from '@/lib/scorecardModel'
import type { PanelProps } from '@/types'

const STATUS_PILL: Record<string, string> = {
  open: 'bg-surf-3 text-ink-1',
  resolved: 'bg-sky-900 text-sky-200',
  void: 'bg-surf-2 text-ink-3',
  overdue: 'bg-amber-900 text-amber-200',
}

export default function ScorecardPanel({ registration }: PanelProps) {
  const { data, isLoading, error, refetch } = useQuery<ScorecardResponse | null>({
    queryKey: ['ledger-scorecard'],
    queryFn: async () => {
      try {
        return await apiGet<ScorecardResponse>(SCORECARD_ROUTE)
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null
        throw e
      }
    },
    refetchInterval: 60_000,
  })

  const state = scorecardState(data)
  const ready = state === 'ready' && data != null
  const header = useMemo(() => headerView(data), [data])
  const cols = useMemo(() => scorecardArmColumns(data), [data])
  const rows = useMemo(() => scorecardRows(data, new Date()), [data])
  const arms = useMemo(() => armRows(data), [data])
  const cal = useMemo(() => calibrationView(data), [data])
  const withheld = scoresWithheld(data)

  return (
    <PanelChrome registration={registration} subtitle={ready ? header.resolvedText : undefined} onRefresh={() => refetch()}>
      {isLoading && <div className="text-ink-3 text-xs">loading…</div>}
      {error instanceof Error && <div className="text-rose-400 text-sm">error: {error.message}</div>}
      {!isLoading && !error && state === 'not_deployed' && (
        <div className="text-ink-3 text-xs" data-testid="scorecard-not-deployed">
          {NOT_DEPLOYED}
        </div>
      )}
      {state === 'unavailable' && (
        <div className="text-amber-400 text-xs mb-2" data-testid="scorecard-unavailable">
          the forecast ledger could not be read — this is not the same as an empty ledger
        </div>
      )}

      {ready && data && (
        <div className="flex-1 overflow-auto text-xs space-y-3" data-testid="scorecard-body">
          <section data-testid="scorecard-header">
            <div className="flex items-baseline gap-3 flex-wrap">
              <span className="text-ink-1 text-sm font-semibold" data-testid="scorecard-n-resolved">
                {header.resolvedText}
              </span>
              {header.noClaimMarker && (
                <span className="rounded px-1 bg-amber-900 text-amber-200" data-testid="scorecard-no-claim">
                  {header.noClaimMarker}
                </span>
              )}
            </div>
            <div className="text-ink-2">{header.statement}</div>
          </section>

          <section className="text-ink-3" data-testid="scorecard-separation">
            <span className="text-label uppercase tracking-wide mr-2">{SEPARATION_LABEL}</span>
            {data.separation_note}
          </section>

          <section>
            <div className="text-label uppercase tracking-wide text-ink-3 mb-1">questions</div>
            {rows.length === 0 ? (
              <div className="text-ink-3 py-2" data-testid="scorecard-empty">
                no sealed questions on the ledger
              </div>
            ) : (
              <table className="w-full" data-testid="scorecard-questions">
                <thead className="text-ink-3 text-label uppercase tracking-wide border-b border-line">
                  <tr>
                    <th className="py-1 px-1 text-left">question</th>
                    <th className="py-1 px-1 text-left">status</th>
                    <th className="py-1 px-1 text-left">due</th>
                    {cols.map((k) => (
                      <th key={k} className="py-1 px-1 text-right">
                        {k}
                      </th>
                    ))}
                    <th className="py-1 px-1 text-right">spread</th>
                    <th className="py-1 px-1 text-left">outcome</th>
                    <th className="py-1 px-1 text-left">evidence</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id} className="border-b border-line align-top" data-testid={`scorecard-row-${r.slug}`}>
                      <td className="py-1 px-1 text-ink-1">
                        <div className="font-mono text-ink-3">
                          {r.slug}
                          {r.singleArm && (
                            <span className="ml-1 rounded px-1 bg-surf-3 text-ink-2" data-testid={`scorecard-single-${r.slug}`}>
                              single arm
                            </span>
                          )}
                        </div>
                        <div>{r.question}</div>
                      </td>
                      <td className="py-1 px-1">
                        <span className={`rounded px-1 ${STATUS_PILL[r.statusLabel] ?? STATUS_PILL.open}`}>{r.statusLabel}</span>
                      </td>
                      <td className="py-1 px-1 text-ink-1 whitespace-nowrap">
                        {r.dueAt}
                        {r.overdue && <span className="ml-1 text-amber-400">overdue</span>}
                      </td>
                      {r.cells.map((c) => (
                        <td
                          key={c.kind}
                          className={`py-1 px-1 text-right font-mono ${c.state === 'number' ? 'text-ink-1' : 'text-ink-3'}`}
                          title={c.title}
                          data-testid={`scorecard-cell-${r.slug}-${c.kind}`}
                        >
                          {c.text}
                          {c.brier != null && (
                            <div className="text-ink-3" data-testid={`scorecard-brier-${r.slug}-${c.kind}`}>
                              Brier {c.brier}
                            </div>
                          )}
                        </td>
                      ))}
                      <td className="py-1 px-1 text-right font-mono text-ink-2">{r.spread}</td>
                      <td className="py-1 px-1 text-ink-1" data-testid={`scorecard-outcome-${r.slug}`}>
                        {r.outcome}
                        {r.proposed && <span className="ml-1 text-ink-3">proposed</span>}
                        {r.needsJudgement && <span className="ml-1 text-amber-400">needs judgement</span>}
                      </td>
                      <td className="py-1 px-1 text-ink-2">
                        {r.evidence.count > 0 && <span>{r.evidence.count} </span>}
                        {r.evidence.urls.map((u) => (
                          <a key={u} href={u} target="_blank" rel="noreferrer" className="text-sky-300 underline mr-1">
                            link
                          </a>
                        ))}
                        {r.evidence.others.map((o) => (
                          <span key={o} className="font-mono text-ink-3 mr-1">
                            {o}
                          </span>
                        ))}
                        {r.evidence.count === 0 && <span className="text-ink-3">{'—'}</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <section data-testid="scorecard-arms">
            <div className="text-label uppercase tracking-wide text-ink-3 mb-1">arms</div>
            {withheld ? (
              <div className="text-ink-2" data-testid="scorecard-scores-withheld">
                {withheldLine(data)}
              </div>
            ) : (
            <table className="w-full">
              <thead className="text-ink-3 text-label uppercase tracking-wide border-b border-line">
                <tr>
                  <th className="py-1 px-1 text-left">arm</th>
                  <th className="py-1 px-1 text-right">n</th>
                  <th className="py-1 px-1 text-right">mean Brier</th>
                  <th className="py-1 px-1 text-right">baseline mean Brier</th>
                  <th className="py-1 px-1 text-right">skill vs climatology</th>
                </tr>
              </thead>
              <tbody>
                {arms.map((a) => (
                  <tr key={a.kind} className="border-b border-line" data-testid={`scorecard-arm-${a.kind}`}>
                    <td className="py-1 px-1 text-ink-1">{a.kind}</td>
                    <td className="py-1 px-1 text-right font-mono">{a.n}</td>
                    <td className="py-1 px-1 text-right font-mono text-ink-1">{a.mean}</td>
                    <td className="py-1 px-1 text-right font-mono text-ink-2">{a.baseline}</td>
                    <td className="py-1 px-1 text-right font-mono text-ink-2">{a.skill ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            )}
          </section>

          <section data-testid="scorecard-due-next">
            <div className="text-label uppercase tracking-wide text-ink-3 mb-1">due next</div>
            {(data.due_next ?? []).length === 0 ? (
              <div className="text-ink-3">nothing due</div>
            ) : (
              <ul>
                {(data.due_next ?? []).map((d) => (
                  <li key={d.slug} className="text-ink-1">
                    <span className="font-mono">{d.slug}</span> <span className="text-ink-2">{d.due_at.slice(0, 10)}</span>{' '}
                    <span className="text-ink-3">{d.resolver_kind}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section data-testid="scorecard-calibration">
            <div className="text-label uppercase tracking-wide text-ink-3 mb-1">calibration</div>
            {!cal.available ? (
              <div className="text-ink-3" data-testid="scorecard-calibration-reason">
                {cal.reason}
              </div>
            ) : (
              <table className="w-full">
                <thead className="text-ink-3 text-label uppercase tracking-wide border-b border-line">
                  <tr>
                    <th className="py-1 px-1 text-left">bin</th>
                    <th className="py-1 px-1 text-right">n</th>
                    <th className="py-1 px-1 text-right">mean p</th>
                    <th className="py-1 px-1 text-right">observed</th>
                  </tr>
                </thead>
                <tbody>
                  {cal.bins.map((b) => (
                    <tr key={b.range} className="border-b border-line">
                      <td className="py-1 px-1 font-mono">{b.range}</td>
                      <td className="py-1 px-1 text-right font-mono">{b.n}</td>
                      <td className="py-1 px-1 text-right font-mono">{b.meanP}</td>
                      <td className="py-1 px-1 text-right font-mono">{b.observed}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          {(data.void_questions ?? []).length > 0 && (
            <section data-testid="scorecard-void">
              <div className="text-label uppercase tracking-wide text-ink-3 mb-1">void questions</div>
              <ul>
                {(data.void_questions ?? []).map((v) => (
                  <li key={v.slug} className="text-ink-1">
                    <span className="font-mono">{v.slug}</span> <span className="text-ink-3">{v.reason}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}
    </PanelChrome>
  )
}
