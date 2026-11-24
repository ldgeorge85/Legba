/**
 * Silences — the Watch surface's first panel region (U-9): the sources and
 * units that are quiet, each with WHY in plain words, sorted onto the one line
 * the reader needs: we LOOKED and there was nothing, or we COULD NOT look (and
 * the cases that cannot be told apart yet, said as such).
 *
 * Sources come from `GET /v3/system/source-firing` — the same query the health
 * line at the foot of every page already holds (`SOURCE_FIRING_KEY`), so this
 * region adds no request; the class is the route's `quiet_class` (the liveness
 * watchdog's own truth table, U-9). Units come from the alert log's open unit
 * episodes (the stage's read, shared by key).
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getSystemSourceFiring } from '@/lib/api'
import { SLOW_STALE_MS, SOURCE_FIRING_KEY } from '@/lib/queryKeys'
import { PROFILE_UI } from '@/lib/profileUi'
import { cn } from '@/lib/cn'
import { firingCensus, QUIET_SIDE_LABEL, sourceSilences, unitSilences, type QuietSide, type SilenceRow } from './model'
import { useAlertLog } from './AlertLog'
import { useWatch } from './state'
import type { FiringRow } from './types'
import { Chip, ChipRow, Figure, Notice, SubjectLink, ViewHead } from './parts'

const SIDE_RULE: Record<QuietSide, string> = {
  looked: 'border-accent-ok',
  unknown: 'border-accent-warning',
  could_not_look: 'border-accent-critical',
}

const SIDES: readonly QuietSide[] = ['could_not_look', 'unknown', 'looked']

function Row({ row }: { row: SilenceRow }) {
  return (
    <li className={cn('border-b border-l-2 border-b-line-subtle py-1.5 pl-3', SIDE_RULE[row.def.side])} data-testid="watch-silence-row" data-side={row.def.side} data-class={row.quietClass}>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="text-body text-ink-1">
          <SubjectLink subject={row.ref} />
        </span>
        <span className="text-label text-ink-3">{row.noun}</span>
      </div>
      <div className="text-body text-ink-2">
        <span className="text-ink-1">{row.def.title}</span> · <span className="text-label text-ink-3">{row.since}</span>
      </div>
      <div className="text-label text-ink-3">{row.def.detail}</div>
    </li>
  )
}

export default function Silences() {
  const firing = useQuery({ queryKey: SOURCE_FIRING_KEY, queryFn: getSystemSourceFiring, staleTime: SLOW_STALE_MS })
  const { q: log } = useAlertLog()
  const side = useWatch((s) => s.silenceSide)
  const rows = (firing.data ?? null) as FiringRow[] | null
  const sources = useMemo(() => sourceSilences(rows, Date.now()), [rows])
  const units = useMemo(() => unitSilences(log.data, Date.now()), [log.data])
  const census = rows ? firingCensus(rows) : null
  const all = [...sources, ...units]
  const shown = side === 'any' ? all : all.filter((r) => r.def.side === side)
  const unitWord = (units.length === 1 ? PROFILE_UI.unit_noun.singular : PROFILE_UI.unit_noun.plural).toLowerCase()

  return (
    <section className="flex h-full min-h-0 flex-col gap-2 overflow-auto bg-surf-1 px-5 py-4" data-testid="watch-silences" data-read={rows ? '1' : '0'}>
      <ViewHead
        title="Silences"
        testId="watch-silences-head"
        line={
          <>
            <Figure value={census?.quiet} testId="watch-silences-quiet" /> of <Figure value={census?.active} /> active sources quiet for 48 hours ·{' '}
            <Figure value={census?.paused} /> paused on purpose
            {log.data?.measured ? (
              <>
                {' '}
                · <Figure value={units.length} /> {unitWord} quiet
              </>
            ) : null}
          </>
        }
      />
      <ChipRow label="Show">
        <Chip on={side === 'any'} onClick={() => useWatch.setState({ silenceSide: 'any' })} testId="watch-silence-side-any">
          All
        </Chip>
        {SIDES.map((s) => (
          <Chip key={s} on={side === s} onClick={() => useWatch.setState({ silenceSide: s })} testId={`watch-silence-side-${s}`}>
            {QUIET_SIDE_LABEL[s]} {rows ? <span className="font-mono text-ink-3">{all.filter((r) => r.def.side === s).length}</span> : null}
          </Chip>
        ))}
      </ChipRow>
      {firing.isPending ? (
        <Notice title="Reading which sources answered…" testId="watch-silences-reading" />
      ) : firing.isError || !rows ? (
        <Notice title="Which sources answered could not be read." testId="watch-silences-error">
          Nothing here is a measurement; it is asked again within a minute.
        </Notice>
      ) : all.length === 0 ? (
        <Notice title="Every active source produced something in the last 48 hours." testId="watch-silences-none">
          {census ? `${census.active} active sources were read; none is quiet.` : null}
        </Notice>
      ) : shown.length === 0 ? (
        <Notice title={`Nothing under “${QUIET_SIDE_LABEL[side as QuietSide]}”.`} testId="watch-silences-filtered-out" />
      ) : (
        <div className="flex flex-col gap-3" data-testid="watch-silence-list">
          {SIDES.filter((s) => side === 'any' || s === side).map((s) => {
            const group = shown.filter((r) => r.def.side === s)
            if (group.length === 0) return null
            return (
              <div key={s}>
                <h3 className="text-label uppercase tracking-[0.06em] text-ink-3">
                  {QUIET_SIDE_LABEL[s]} · {group.length}
                </h3>
                <ul>
                  {group.map((r) => (
                    <Row key={r.id} row={r} />
                  ))}
                </ul>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
