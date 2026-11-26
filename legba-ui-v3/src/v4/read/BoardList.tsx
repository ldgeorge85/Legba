/**
 * BoardList — the Board's stage: what changed since the reader last looked,
 * ranked, eight rows and then "more". Changed, Checked, Gaps and Due are
 * filters over the one list, not chapters of a long page. A row names its
 * subject, says in one sentence what changed, when, and at most two plain
 * trust words; a click opens the row's record in the Record sheet, in place.
 *
 * The long-form daily read (`v4.morning_read`: every band by subject, then the
 * composed record) is one link away and opens on the stage with a way back.
 *
 * A subject that is the point narrows the list to it, as it narrowed the
 * daily read's bands; "show every …" clears it.
 */
import { useCallback, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ArrowRight } from 'lucide-react'
import { apiGet } from '@/lib/api'
import type { CountryScorecard } from '@/lib/evalOps'
import { PROFILE_UI } from '@/lib/profileUi'
import { humanizeId, subjectName, useSubjectIndex } from '@/lib/deskNames'
import { getFramePort } from '@/lib/panelActions'
import { DAILY_READ_VIEW } from '@/lib/surfaces'
import { cn } from '@/lib/cn'
import { selectRow } from '@/state/selection'
import { clearPoint, usePointSubject } from '@/state/scope'
import { SeverityDot } from '@/components/SeverityBadge'
import { useGapViews } from './GapsBand'
import { stateOf, type QueryState } from './bandChrome'
import { asSeverity, utcStamp } from './readFormat'
import { ClaimText, claimPlain } from './claimText'
import { BOARD_REFETCH_MS, sinceWords, useSince, useVisitCursor, useWindowFindings } from './boardData'
import {
  BOARD_FILTERS,
  BOARD_ROWS_SHOWN,
  changedRows,
  checkedRows,
  dueRows,
  gapRows,
  onSubjectRows,
  whenText,
  type BoardFilter,
  type BoardRow,
} from './boardList'

const SUBJECT = PROFILE_UI.subject.singular.toLowerCase()
const SUBJECTS = PROFILE_UI.subject.plural.toLowerCase()

/** What a filter says while its rows are not read, and when they are none. */
const EMPTY: Record<BoardFilter, { reading: string; none: string; failed: string }> = {
  changed: {
    reading: 'Reading what changed…',
    none: 'Nothing landed since you last looked. That is an answer, not a stalled read.',
    failed: 'What changed could not be read. Nothing is shown in its place.',
  },
  checked: {
    reading: 'Reading what was checked…',
    none: 'No finding was written in this window, so nothing was checked.',
    failed: 'What was checked could not be read, so no result is claimed either way.',
  },
  gaps: {
    reading: 'Reading where the evidence ran out…',
    none: `No gap is recorded on the ${SUBJECTS} read.`,
    failed: 'The gaps could not be read.',
  },
  due: {
    reading: 'Reading what is due…',
    none: 'Every call whose window closed has an outcome.',
    failed: 'What is due could not be read, so nothing is claimed as settled.',
  },
}

function Row({ row, now }: { row: BoardRow; now: number }) {
  const sev = asSeverity(row.severity)
  const sentence = claimPlain(row.sentence)
  const body = (
    <>
      <span className="flex w-3 shrink-0 justify-center pt-[0.4rem]" aria-hidden>
        {sev ? <SeverityDot severity={sev} className="h-2 w-2" /> : null}
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-baseline gap-3">
          <span className="w-[8rem] shrink-0 truncate text-body text-ink-2" data-testid="board-row-subject">
            {row.subject}
          </span>
          <span className="min-w-0 flex-1 text-body text-ink-1 line-clamp-2 [overflow-wrap:anywhere]" data-testid="board-row-sentence">
            <ClaimText text={row.sentence} />
          </span>
          <span className="shrink-0 font-mono text-label tabular-nums text-ink-3" title={utcStamp(row.at) ?? undefined} data-testid="board-row-when">
            {whenText(row.at, now)}
          </span>
        </span>
        {row.trust.length > 0 && (
          <span className="mt-0.5 flex gap-3 pl-[calc(8rem+0.75rem)] text-label text-ink-3" data-testid="board-row-trust">
            {row.trust.map((t) => (
              <span key={t.text} title={t.title}>
                {t.text}
              </span>
            ))}
          </span>
        )}
      </span>
    </>
  )
  const cls = 'flex w-full items-start gap-2 px-4 py-1.5 text-left'
  return (
    <li className="border-b border-line-subtle last:border-b-0" data-testid="board-row-item">
      {row.open ? (
        <button
          type="button"
          onClick={() => selectRow(row.open!.rowKind, row.open!.id, row.open!.label, { origin: 'board' })}
          className={cn(cls, 'hover:bg-surf-2')}
          title={row.detail ? `${row.subject}: ${claimPlain(row.detail, 400)}` : `${row.subject}: ${sentence}`}
          data-testid="board-row"
          data-key={row.key}
        >
          {body}
        </button>
      ) : (
        <div className={cls} title={row.detail ? claimPlain(row.detail, 400) : undefined} data-testid="board-row" data-key={row.key}>
          {body}
        </div>
      )}
    </li>
  )
}

export default function BoardList() {
  const { cursor, firstVisit } = useVisitCursor()
  const [filter, setFilter] = useState<BoardFilter>('changed')
  const [more, setMore] = useState(false)
  const now = Date.now()

  const since = useSince(cursor)
  const day = useWindowFindings(cursor)
  const wantGaps = filter === 'gaps'
  const cards = useQuery<CountryScorecard[]>({
    queryKey: ['morning-judgment', 'scorecards'],
    refetchInterval: BOARD_REFETCH_MS,
    enabled: wantGaps,
    queryFn: () => apiGet<CountryScorecard[]>('/v3/eval/country_scorecard'),
  })

  const point = usePointSubject()
  const subjectId = point?.id ?? null
  const scopedCards = useMemo(
    () => (subjectId && cards.data ? cards.data.filter((c) => c.target_id === subjectId) : cards.data),
    [cards.data, subjectId],
  )
  const gaps = useGapViews(wantGaps ? scopedCards : undefined, { enabled: wantGaps })

  // The roster names the subjects; the rows are re-said once it has answered.
  const names = useSubjectIndex()
  const checks = useMemo(() => new Map((day.data?.data ?? []).map((f) => [f.id, f])), [day.data])
  const all: Record<BoardFilter, BoardRow[]> = useMemo(
    () => ({
      changed: onSubjectRows(changedRows(since.data, checks), subjectId),
      checked: onSubjectRows(checkedRows(day.data?.data), subjectId),
      gaps: onSubjectRows(gapRows(gaps.views), subjectId),
      due: onSubjectRows(dueRows(since.data?.forecasts_due?.items), subjectId),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [since.data, checks, day.data, gaps.views, subjectId, names],
  )
  const states: Record<BoardFilter, QueryState> = {
    changed: stateOf(since),
    checked: stateOf(day),
    gaps: !wantGaps || cards.isLoading || gaps.pending ? 'loading' : cards.isError ? 'error' : 'ready',
    due: stateOf(since),
  }
  // A count is printed only once its rows are read; Gaps is read when asked for.
  const count = (f: BoardFilter): string | null =>
    f === 'gaps' && !wantGaps ? null : states[f] === 'ready' ? String(all[f].length) : states[f] === 'error' ? 'unavailable' : '—'

  const rows = all[filter]
  const shown = more ? rows : rows.slice(0, BOARD_ROWS_SHOWN)
  const state = states[filter]
  const pick = useCallback((f: BoardFilter) => {
    setFilter(f)
    setMore(false)
  }, [])
  const openDailyRead = () => {
    getFramePort()?.openView(DAILY_READ_VIEW, { place: 'stage', origin: 'operator' })
  }
  const spec = BOARD_FILTERS.find((f) => f.id === filter)!
  const subjects = new Set(all.changed.map((r) => r.subjectId ?? '')).size

  return (
    <section className="flex h-full min-h-0 w-full flex-col bg-surf-1" data-testid="board-list" data-filter={filter} data-read={state === 'ready' ? '1' : '0'}>
      <header className="shrink-0 border-b border-line px-4 pb-2 pt-3">
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="text-heading text-ink-1">What changed</h2>
          <span className="truncate text-label text-ink-3" data-testid="board-list-since" data-since={cursor}>
            {sinceWords({ cursor, firstVisit })}
            {states.changed === 'ready' && all.changed.length > 0
              ? ` · ${all.changed.length} across ${subjects} ${subjects === 1 ? SUBJECT : SUBJECTS}`
              : ''}
          </span>
        </div>
        <div className="mt-2 flex items-center gap-1" role="group" aria-label="Show" data-testid="board-filters">
          {BOARD_FILTERS.map((f) => {
            const n = count(f.id)
            return (
              <button
                key={f.id}
                type="button"
                aria-pressed={filter === f.id}
                title={f.question}
                onClick={() => pick(f.id)}
                data-testid={`board-filter-${f.id}`}
                className={cn(
                  'rounded-control px-2 py-0.5 text-label',
                  filter === f.id ? 'bg-surf-3 text-ink-1' : 'text-ink-3 hover:text-ink-1',
                )}
              >
                {f.label}
                {n !== null && (
                  <span className="ml-1.5 font-mono tabular-nums text-ink-3" data-testid={`board-filter-count-${f.id}`}>
                    {n}
                  </span>
                )}
              </button>
            )
          })}
          <button
            type="button"
            onClick={openDailyRead}
            className="ml-auto flex items-center gap-1 text-label text-ink-3 hover:text-ink-1"
            data-testid="board-daily-read"
            title="Every change, check, gap and due call by subject, then the composed record"
          >
            Read the full daily read
            <ArrowRight size={12} aria-hidden />
          </button>
        </div>
        {subjectId && (
          <p className="mt-1.5 text-label text-ink-2" data-testid="board-list-scoped">
            Only <span className="text-ink-1">{subjectName(subjectId, names) ?? (point && point.label !== point.id ? point.label : humanizeId(subjectId))}</span>.{' '}
            <button type="button" onClick={clearPoint} className="text-ink-3 underline decoration-dotted underline-offset-2 hover:text-ink-1" data-testid="board-list-scope-clear">
              Show every {SUBJECT}
            </button>
          </p>
        )}
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto" data-testid="board-list-body">
        {state !== 'ready' || rows.length === 0 ? (
          <p className="px-4 py-3 text-body text-ink-3" data-testid={`board-list-${state === 'ready' ? 'empty' : state}`}>
            {state === 'loading' ? EMPTY[filter].reading : state === 'error' ? EMPTY[filter].failed : EMPTY[filter].none}
          </p>
        ) : (
          <>
            <p className="sr-only">{spec.question}</p>
            <ul data-testid="board-rows">
              {shown.map((r) => (
                <Row key={r.key} row={r} now={now} />
              ))}
            </ul>
            {rows.length > shown.length && (
              <button
                type="button"
                onClick={() => setMore(true)}
                className="px-4 py-2 text-label text-ink-3 underline decoration-dotted underline-offset-2 hover:text-ink-1"
                data-testid="board-list-more"
              >
                {rows.length - shown.length} more
              </button>
            )}
            {filter === 'gaps' && gaps.heldBackDesks > 0 && (
              <p className="px-4 pb-2 text-label text-ink-3">
                Read on the first {SUBJECTS} only; {gaps.heldBackDesks} more are in the full daily read.
              </p>
            )}
            {filter === 'due' && (
              <p className="px-4 pb-2 text-label text-ink-3">
                These are the older pilot calls, not the sealed forecasts on the Ledger.
              </p>
            )}
          </>
        )}
      </div>
    </section>
  )
}
