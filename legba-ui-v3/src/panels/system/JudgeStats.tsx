/**
 * Judge Stats (`system.judge_stats`) — WHO SERVED THE JUDGE, and did it matter.
 *
 * GLASS-3's one new API, given its reader. `served_by` — the upstream provider a
 * router actually dispatched a judge call to — has been written onto every LLM
 * receipt since 2026-08-16 and read by nothing, while a provider change was
 * measured to flip 13.6% of verdicts. That is an unannounced upstream input to
 * the faithfulness numbers the whole product is graded on, and it was observable
 * only by hand-decoding a JSONB array. This panel is where it becomes a fact an
 * operator can act on.
 *
 * WHAT IT RENDERS, in the order the question is actually asked:
 *   1. THE DRIFT READOUT — do the two highest-volume providers agree? This is the
 *      only thing most visits are for, so it leads. It reports a delta ONLY when
 *      both sides clear `MIN_COMPARABLE_N`; below that it says how far short it
 *      is, because "not enough data yet" and "no drift" are opposite findings and
 *      a panel that renders them the same way is worse than no panel.
 *   2. The provider table — the verdict mix, adjudicated share, faithfulness mean
 *      and call stats per provider, every one of them printed with its n.
 *   3. The three attribution-failure buckets, kept BELOW the real providers and
 *      visibly apart, each carrying the server's own explanation of what it means.
 *   4. The judge-pipeline stamps in the window, so a pooled mean is never read as
 *      one grader's when it is two.
 *   5. The day grid — when a provider entered or left.
 *
 * HONESTY RULES this panel holds to:
 *   * `measured: false` is a FAILED READ, not a quiet judge. The server degrades
 *     to an all-defaults payload at HTTP 200 rather than 500ing a polling panel,
 *     so this field is the only thing separating the two and it is rendered
 *     loudly.
 *   * No statistic is printed without its n (`formatMeasure` cannot do it).
 *   * A null mean renders as "unmeasured", never as 0.000 — an `unassessable`
 *     verdict carries no score, and a fabricated zero would drag every average
 *     that reads it.
 *   * The sentinel buckets are never summed into a provider or into the provider
 *     count. `(no receipt)` is usually the LARGEST bucket and that is correct:
 *     deterministic and unsampled verdicts never called an LLM and so cannot have
 *     a serving provider.
 *   * Sentinel meanings come from the payload's `sentinels` map, not from a
 *     glossary copied into this file that could drift from the server's.
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, HelpCircle, TrendingUp } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import { cn } from '@/lib/cn'
import { fetchJudgeStats } from '@/lib/api'
import type { JudgeStatsProvider, JudgeStatsResponse } from '@/lib/api'
import {
  UNMEASURED,
  cellsByDay,
  driftReadout,
  formatMeasure,
  pipelineCaveat,
  realProviders,
  sentinelMeaning,
  sentinelRows,
  statusMix,
  summaryLine,
} from '@/lib/judgeStats'
import type { PanelProps } from '@/types'

/** Server-side bounds are [1, 90] and it 400s outside them rather than clamping. */
const WINDOWS = [7, 14, 30, 90] as const

const STATUS_TONE: Record<string, string> = {
  llm: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40',
  deterministic: 'bg-amber-500/15 text-amber-300 border-amber-500/40',
  unsampled: 'bg-surf-1 text-ink-3 border-line',
  '(unknown)': 'bg-rose-500/15 text-rose-300 border-rose-500/40',
}

export default function JudgeStatsPanel({ registration }: PanelProps) {
  const [days, setDays] = useState<number>(14)

  const { data, isLoading, error, refetch } = useQuery<JudgeStatsResponse>({
    queryKey: ['judge-stats', days],
    queryFn: () => fetchJudgeStats({ days }),
    refetchInterval: 120_000,
  })

  const drift = useMemo(
    () => (data ? driftReadout(data) : null),
    [data],
  )
  const dayRows = useMemo(() => (data ? cellsByDay(data.cells) : []), [data])

  const real = data ? realProviders(data.providers) : []
  const sentinels = data ? sentinelRows(data.providers) : []
  const caveat = data ? pipelineCaveat(data) : null

  return (
    <PanelChrome
      registration={registration}
      subtitle={data ? summaryLine(data) : 'reading the judge ledger…'}
      onRefresh={() => refetch()}
      actions={
        <div className="flex items-center gap-1" data-testid="judge-stats-windows">
          {WINDOWS.map((w) => (
            <button
              key={w}
              type="button"
              onClick={() => setDays(w)}
              data-testid={`judge-stats-window-${w}`}
              className={cn(
                'rounded border px-2 py-0.5 text-label',
                days === w
                  ? 'border-line-strong bg-surf-3 text-ink-1'
                  : 'border-line text-ink-3 hover:text-ink-1',
              )}
            >
              {w}d
            </button>
          ))}
        </div>
      }
    >
      {isLoading && (
        <div className="text-body text-ink-3" data-testid="judge-stats-loading">
          reading the judge ledger…
        </div>
      )}

      {error != null && (
        <div
          className="rounded border border-rose-500/40 bg-rose-500/10 p-2 text-body text-rose-300"
          data-testid="judge-stats-error"
        >
          Could not read judge stats — {String((error as Error).message ?? error)}
        </div>
      )}

      {data && !data.measured && (
        <div
          className="mb-2 flex items-start gap-1.5 rounded border border-rose-500/40 bg-rose-500/10 p-2 text-body text-rose-300"
          data-testid="judge-stats-unmeasured"
        >
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>
            The judge ledger could not be read. Everything below is empty because
            the query failed — this is <strong>not</strong> a report that the judge
            was idle.
          </span>
        </div>
      )}

      {data && data.measured && (
        <div className="space-y-3">
          {drift && <DriftCard drift={drift} />}

          {caveat && (
            <div
              className="flex items-start gap-1.5 rounded border border-amber-500/40 bg-amber-500/10 p-2 text-body text-amber-300"
              data-testid="judge-stats-pipeline-caveat"
            >
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              <span>{caveat}</span>
            </div>
          )}

          <Section label={`Serving providers (${real.length})`}>
            {real.length === 0 ? (
              <p className="text-body text-ink-3" data-testid="judge-stats-no-providers">
                No verdict in this window could be attributed to a named serving
                provider. That is expected while the judge runs on a direct
                provider rather than a router — see the buckets below.
              </p>
            ) : (
              <ProviderTable
                rows={real}
                statuses={data.judge_statuses}
                sentinelMap={data.sentinels}
                testId="judge-stats-providers"
              />
            )}
          </Section>

          {sentinels.length > 0 && (
            <Section label="Unattributed — and exactly why">
              <p className="mb-1 text-label text-ink-3">
                These are not providers. Each is a distinct reason a verdict could
                not be traced to one, and they are never folded into the numbers
                above.
              </p>
              <ProviderTable
                rows={sentinels}
                statuses={data.judge_statuses}
                sentinelMap={data.sentinels}
                testId="judge-stats-sentinels"
              />
            </Section>
          )}

          <Section label={`Judge-pipeline stamps (${data.pipeline_versions.length})`}>
            <table className="w-full text-body" data-testid="judge-stats-versions">
              <thead>
                <tr className="text-label uppercase tracking-wider text-ink-3">
                  <th className="py-0.5 text-left font-normal">stamp</th>
                  <th className="py-0.5 text-right font-normal">verdicts</th>
                  <th className="py-0.5 text-right font-normal">faithfulness</th>
                  <th className="py-0.5 text-left font-normal">providers</th>
                </tr>
              </thead>
              <tbody>
                {data.pipeline_versions.map((v) => (
                  <tr
                    key={v.judge_pipeline_version}
                    className="border-t border-line"
                    data-testid={`judge-stats-version-${v.judge_pipeline_version}`}
                  >
                    <td className="py-0.5 font-mono text-ink-2">
                      {v.judge_pipeline_version}
                    </td>
                    <td className="py-0.5 text-right text-ink-2">{v.n}</td>
                    <td className="py-0.5 text-right text-ink-2">
                      {formatMeasure(v.faithfulness_mean, v.faithfulness_n)}
                    </td>
                    <td className="py-0.5 font-mono text-label text-ink-3">
                      {v.providers.join(' · ')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>

          {dayRows.length > 0 && (
            <Section label="By day — when a provider entered or left">
              <div className="space-y-0.5" data-testid="judge-stats-days">
                {dayRows.map((d) => (
                  <div
                    key={d.day}
                    className="flex items-baseline gap-2 text-label"
                    data-testid={`judge-stats-day-${d.day}`}
                  >
                    <span className="w-24 shrink-0 font-mono text-ink-3">{d.day}</span>
                    <span className="w-12 shrink-0 text-right text-ink-2">{d.total}</span>
                    <span className="min-w-0 flex-1 truncate text-ink-3">
                      {Object.entries(d.byProvider)
                        .sort((a, b) => b[1] - a[1])
                        .map(([label, n]) => `${label} ${n}`)
                        .join(' · ')}
                    </span>
                  </div>
                ))}
              </div>
            </Section>
          )}

          <div className="text-label text-ink-3" data-testid="judge-stats-footer">
            {data.totals.judge_calls} judge call
            {data.totals.judge_calls === 1 ? '' : 's'} in window
            {data.totals.judge_call_errors > 0 && (
              <> · <span className="text-rose-300">
                {data.totals.judge_call_errors} failed
              </span></>
            )}
            {data.generated_at && <> · read {data.generated_at}</>}
          </div>
        </div>
      )}
    </PanelChrome>
  )
}

function DriftCard({ drift }: { drift: ReturnType<typeof driftReadout> }) {
  const tone =
    drift.verdict === 'drift'
      ? 'border-rose-500/40 bg-rose-500/10 text-rose-300'
      : drift.verdict === 'steady'
        ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
        : 'border-line bg-surf-1 text-ink-2'
  return (
    <div
      className={cn('flex items-start gap-1.5 rounded border p-2 text-body', tone)}
      data-testid={`judge-stats-drift-${drift.verdict}`}
    >
      <TrendingUp className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
      <div className="min-w-0">
        <div className="text-label uppercase tracking-wider opacity-80">
          Provider drift
        </div>
        <div className="mt-0.5">{drift.summary}</div>
        {drift.adjudicatedDelta != null && (
          <div className="mt-0.5 text-label opacity-90">
            adjudicated share differs by {drift.adjudicatedDelta.toFixed(3)}
          </div>
        )}
      </div>
    </div>
  )
}

function ProviderTable({
  rows,
  statuses,
  sentinelMap,
  testId,
}: {
  rows: JudgeStatsProvider[]
  statuses: string[]
  sentinelMap: Record<string, string>
  testId: string
}) {
  return (
    <table className="w-full text-body" data-testid={testId}>
      <thead>
        <tr className="text-label uppercase tracking-wider text-ink-3">
          <th className="py-0.5 text-left font-normal">served by</th>
          <th className="py-0.5 text-left font-normal">verdict mix</th>
          <th className="py-0.5 text-right font-normal">adjudicated</th>
          <th className="py-0.5 text-right font-normal">faithfulness</th>
          <th className="py-0.5 text-right font-normal">calls</th>
          <th className="py-0.5 text-right font-normal">p95</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((p) => {
          const meaning = sentinelMeaning(p.served_by, sentinelMap)
          return (
            <tr
              key={p.served_by}
              className="border-t border-line align-top"
              data-testid={`judge-stats-provider-${p.served_by}`}
            >
              <td className="py-1 pr-2">
                <div className="flex items-center gap-1">
                  <span
                    className={cn(
                      'font-mono',
                      p.is_sentinel ? 'text-ink-3' : 'text-ink-1',
                    )}
                  >
                    {p.served_by}
                  </span>
                  {meaning && (
                    <HelpCircle
                      className="h-3 w-3 shrink-0 text-ink-3"
                      aria-hidden
                    />
                  )}
                </div>
                <div className="text-label text-ink-3">{p.n} verdicts</div>
                {meaning && (
                  <div
                    className="mt-0.5 max-w-md text-label text-ink-3"
                    data-testid={`judge-stats-meaning-${p.served_by}`}
                  >
                    {meaning}
                  </div>
                )}
              </td>
              <td className="py-1 pr-2">
                <div className="flex flex-wrap gap-1">
                  {statusMix(p, statuses)
                    .filter((s) => s.n > 0)
                    .map((s) => (
                      <span
                        key={s.status}
                        className={cn(
                          'rounded border px-1 py-0.5 text-label',
                          STATUS_TONE[s.status] ?? 'border-line bg-surf-2 text-ink-2',
                        )}
                        data-testid={`judge-stats-mix-${p.served_by}-${s.status}`}
                      >
                        {s.status} {s.n}
                      </span>
                    ))}
                </div>
              </td>
              <td
                className="py-1 text-right font-mono text-ink-2"
                data-testid={`judge-stats-adjudicated-${p.served_by}`}
              >
                {formatMeasure(p.adjudicated_share, p.adjudicated_n, {
                  percent: true,
                })}
              </td>
              <td
                className="py-1 text-right font-mono text-ink-2"
                data-testid={`judge-stats-faithfulness-${p.served_by}`}
              >
                {formatMeasure(p.faithfulness_mean, p.faithfulness_n)}
              </td>
              <td className="py-1 text-right font-mono text-ink-2">
                {p.judge_calls}
                {p.judge_call_errors > 0 && (
                  <span className="text-rose-300"> ({p.judge_call_errors} err)</span>
                )}
              </td>
              <td className="py-1 text-right font-mono text-ink-2">
                {p.latency_p95_ms == null ? UNMEASURED : `${p.latency_p95_ms}ms`}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-label uppercase tracking-wider text-ink-3">{label}</div>
      <div className="mt-0.5">{children}</div>
    </div>
  )
}
