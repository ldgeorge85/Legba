/**
 * Read Scoreboard (`system.read_scoreboard`) — THE WAGER'S SCOREBOARD.
 *
 * Every other panel on the ops deck measures the engine. This one measures
 * the OPERATOR, and it is the only panel in the workstation that does.
 *
 * PREMISE_REASON_TO_EXIST §2.1 falsified the reading half of the glass-tower
 * ruling ("a self-driving organ the operator reads") off Caddy access logs,
 * and §5 Option 1 pre-commits the hypothesis — "given a worthy morning read,
 * the operator will read it" — to be graded at day 90 by logged numbers. This
 * panel is where those numbers become legible while the wager is still
 * running, rather than a surprise at the end of it.
 *
 * HONESTY RULES, the same ones the rest of the deck holds to:
 *
 *   * AN EMPTY LOG IS A FINDING, NOT AN ERROR. Zero reads renders as an
 *     explicit "nothing read in this window" — the answer the premise review
 *     predicts — never as a spinner, a dash, or a blank tile. A scoreboard
 *     that looks broken when the score is zero would let the most important
 *     result be mistaken for a bug.
 *
 *   * THE HEADLINE IS A RATIO WITH ITS DENOMINATOR PRINTED. "Morning read
 *     opened on 12 of 30 days" is the wager's actual metric; "12 reads" is
 *     not, because a single day of twelve refreshes and twelve separate
 *     mornings are opposite findings.
 *
 *   * DRILLS ARE SHOWN SEPARATELY FROM OPENS. §2.2's claim is specifically
 *     that the *trust* operations — lineage walks, citation drills — are
 *     never performed. Folding them into a total "reads" number would hide
 *     exactly the thing under test, so they get their own row.
 *
 *   * THE PANEL DOES NOT COUNT ITSELF. Opening it fires a `panel_open` like
 *     any other panel, which is honest — but the tile explains that reads
 *     include panel opens, so nobody reads a self-inflated number as
 *     evidence of reading the product.
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BookOpen, Eye, Footprints } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import { fetchReadRollup } from '@/lib/api'
import type { ReadRollupResponse } from '@/lib/api'
import { DRILL_KINDS, kindTotals, readSummaryLine, recentDays } from '@/lib/readScoreboard'
import type { PanelProps } from '@/types'

/** Server bounds are [1, 365] and it 422s outside them rather than clamping. */
const WINDOWS = [7, 30, 90] as const

export default function ReadScoreboardPanel({ registration }: PanelProps) {
  const [days, setDays] = useState<number>(30)

  const { data, isLoading, error, refetch } = useQuery<ReadRollupResponse>({
    queryKey: ['read-rollup', days],
    queryFn: () => fetchReadRollup({ days }),
    refetchInterval: 120_000,
  })

  const byKind = useMemo(() => (data ? kindTotals(data.days) : []), [data])
  const grid = useMemo(() => (data ? recentDays(data.days, 14) : []), [data])
  const drills = byKind
    .filter((k) => DRILL_KINDS.includes(k.kind))
    .reduce((sum, k) => sum + k.events, 0)

  return (
    <PanelChrome
      registration={registration}
      subtitle={data ? readSummaryLine(data) : 'reading the read log…'}
      onRefresh={() => refetch()}
      actions={
        <div className="flex items-center gap-1" data-testid="read-scoreboard-windows">
          {WINDOWS.map((w) => (
            <button
              key={w}
              type="button"
              onClick={() => setDays(w)}
              aria-pressed={days === w}
              className={
                days === w
                  ? 'rounded border border-line bg-surf-1 px-1.5 py-0.5 text-label text-ink-1'
                  : 'rounded border border-transparent px-1.5 py-0.5 text-label text-ink-3 hover:text-ink-1'
              }
            >
              {w}d
            </button>
          ))}
        </div>
      }
    >
      {isLoading && (
        <div className="p-density text-body text-ink-3" data-testid="read-scoreboard-loading">
          reading the read log…
        </div>
      )}

      {error != null && (
        <div className="p-density text-body text-ink-2" data-testid="read-scoreboard-error">
          Could not read the read log — {String((error as Error).message ?? error)}
        </div>
      )}

      {data && (
        <div className="flex flex-col gap-3 p-density">
          {/* The three tiles the wager actually turns on. */}
          <div className="grid grid-cols-3 gap-2" data-testid="read-scoreboard-tiles">
            <Tile
              icon={<Eye className="h-3.5 w-3.5" aria-hidden />}
              label="reads today"
              value={data.totals.reads_today}
              note={`${data.totals.reads_this_week} this week`}
              testId="tile-reads"
            />
            <Tile
              icon={<BookOpen className="h-3.5 w-3.5" aria-hidden />}
              label="morning reads"
              value={data.totals.brief_reads_today}
              note={`opened on ${data.totals.brief_read_days} of ${data.totals.window_days} days`}
              testId="tile-brief"
            />
            <Tile
              icon={<Footprints className="h-3.5 w-3.5" aria-hidden />}
              label="drills"
              value={drills}
              note="lineage walks + citation drills"
              testId="tile-drills"
            />
          </div>

          {/* An empty log is the premise review's PREDICTION, so say it plainly. */}
          {data.days.length === 0 ? (
            <div
              className="rounded border border-line bg-surf-3 p-density text-body text-ink-2"
              data-testid="read-scoreboard-empty"
            >
              Nothing read in the last {data.totals.window_days} days. This is a
              measurement, not a failure to load — the read log is reachable and
              it is empty.
            </div>
          ) : (
            <>
              <table className="w-full text-body" data-testid="read-scoreboard-kinds">
                <thead>
                  <tr className="text-label uppercase tracking-wider text-ink-3">
                    <th className="py-1 text-left font-normal">event</th>
                    <th className="py-1 text-right font-normal">events</th>
                    <th className="py-1 text-right font-normal">days seen</th>
                  </tr>
                </thead>
                <tbody>
                  {byKind.map((k) => (
                    <tr key={k.kind} className="border-t border-line" data-testid="read-kind-row">
                      <td className="py-1 text-ink-2">{k.kind}</td>
                      <td className="py-1 text-right tabular-nums text-ink-1">{k.events}</td>
                      <td className="py-1 text-right tabular-nums text-ink-3">{k.days}</td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <div>
                <div className="mb-1 text-label uppercase tracking-wider text-ink-3">
                  last 14 days
                </div>
                <div className="flex items-end gap-0.5" data-testid="read-scoreboard-grid">
                  {grid.map((d) => (
                    <div
                      key={d.day}
                      title={`${d.day} — ${d.events} event(s)${d.briefRead ? ', morning read opened' : ''}`}
                      data-testid="read-day-cell"
                      data-brief-read={d.briefRead ? 'true' : 'false'}
                      data-events={d.events}
                      // CHANNEL C (system state) — legitimate here and only
                      // here: this is an ops surface, and "the operator opened
                      // the morning read" is exactly the ok-condition the
                      // channel is reserved for. Confidence/severity would
                      // both be a channel misuse (§5.2).
                      className={
                        d.briefRead
                          ? 'h-6 flex-1 rounded-sm bg-accent-ok/80'
                          : d.events > 0
                            ? 'h-6 flex-1 rounded-sm bg-accent-ok/25'
                            : 'h-6 flex-1 rounded-sm border border-line bg-surf-3'
                      }
                    />
                  ))}
                </div>
                <div className="mt-1 text-label text-ink-3">
                  filled = the morning read was opened · faint = read something else ·
                  empty = nothing
                </div>
              </div>
            </>
          )}

          <div className="text-label text-ink-3">
            &ldquo;reads&rdquo; counts every instrumented open, including panel opens
            (this panel included). The wager&rsquo;s metric is the morning-read day
            count above it.
          </div>
        </div>
      )}
    </PanelChrome>
  )
}

function Tile({
  icon,
  label,
  value,
  note,
  testId,
}: {
  icon: React.ReactNode
  label: string
  value: number
  note: string
  testId: string
}) {
  return (
    <div className="rounded border border-line bg-surf-3 p-2" data-testid={testId}>
      <div className="flex items-center gap-1 text-label uppercase tracking-wider text-ink-3">
        {icon}
        {label}
      </div>
      <div className="mt-0.5 text-xl tabular-nums text-ink-1">{value}</div>
      <div className="text-label text-ink-3">{note}</div>
    </div>
  )
}
