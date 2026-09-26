/**
 * ReportsList — the navigator, and the landing screen.
 *
 * The v2 Reports panel was a split pane: list left, rendered report right. A
 * phone has room for one of those at a time, so the split becomes a push: this
 * list, then the report, with Back as the way home.
 *
 * Every row answers the four questions the operator asks before deciding to
 * read: what tier, about what, from when, and does it hold up (verify). The
 * lead thread is the fifth line only because it is the one that decides.
 */

import { memo } from 'react'
import {
  groupByTier,
  rowsForDay,
  TIER_LABEL,
  type ReportRow,
  type ReportTier,
} from '../mobileModel'
import { score } from '@/v4/read/readFormat'

/** The roster class a country target id carries (`country_g20_sa` → `g20`). */
export function rosterClass(targetId: string | null): string | null {
  if (!targetId) return null
  const m = /^country_([a-z0-9]+)_/.exec(targetId)
  return m ? m[1] : null
}

const ROSTER_LABEL: Record<string, string> = {
  g20: 'G20',
  watch: 'Watch list',
}

/**
 * Verify, as a chip.
 *
 * `score()` renders an absent number as an em dash rather than `0.00`, which is
 * the difference between "unmeasured" and "measured as bad" — a distinction the
 * whole verify apparatus exists to preserve.
 */
function VerifyChip({ row }: { row: ReportRow }) {
  const value = row.verifyScore
  const tone = value == null ? 'm-chip-quiet' : value >= 0.8 ? 'm-chip-ok' : 'm-chip-warn'
  return (
    <span
      className={`m-chip ${tone}`}
      title={row.verifyState ? `verify: ${row.verifyState}` : 'not scored'}
    >
      {score(value)}
    </span>
  )
}

function ReportRowItem({ row, onOpen }: { row: ReportRow; onOpen: (id: string) => void }) {
  return (
    <li>
      <button
        type="button"
        className="m-row"
        data-testid="report-row"
        data-report-id={row.id}
        onClick={() => onOpen(row.id)}
      >
        <span className="m-row-head">
          <span className="m-row-target">{row.target}</span>
          <VerifyChip row={row} />
        </span>
        <span className="m-row-meta">
          {TIER_LABEL[row.tier]} · {row.day || 'undated'}
        </span>
        {row.leadThread ? (
          <span className="m-row-lead">{row.leadThread}</span>
        ) : (
          <span className="m-row-lead m-quiet">no lead recorded</span>
        )}
      </button>
    </li>
  )
}

/** Country reads, sub-grouped by roster class — the one grouping the row carries. */
function CountryGroup({ rows, onOpen }: { rows: ReportRow[]; onOpen: (id: string) => void }) {
  const byRoster = new Map<string, ReportRow[]>()
  for (const r of rows) {
    const key = rosterClass(r.targetId) ?? 'other'
    const list = byRoster.get(key)
    if (list) list.push(r)
    else byRoster.set(key, [r])
  }
  return (
    <>
      {[...byRoster.entries()].map(([roster, group]) => (
        <div key={roster} className="m-subgroup">
          <h3 className="m-subheading">
            {ROSTER_LABEL[roster] ?? roster} <span className="m-count">{group.length}</span>
          </h3>
          <ul className="m-list">
            {group.map((r) => (
              <ReportRowItem key={r.id} row={r} onOpen={onOpen} />
            ))}
          </ul>
        </div>
      ))}
    </>
  )
}

export interface ReportsListProps {
  rows: ReportRow[]
  day: string | null
  days: string[]
  onOpen: (id: string) => void
  onDay: (day: string | null) => void
  /** The journal's latest consolidation, rendered as its own row when present. */
  consolidation: { id: string; title: string; producedAt: string } | null
  onOpenConsolidation: () => void
  cachedAt: string | null
}

function ReportsListImpl({
  rows,
  day,
  days,
  onOpen,
  onDay,
  consolidation,
  onOpenConsolidation,
  cachedAt,
}: ReportsListProps) {
  const visible = rowsForDay(rows, day)
  const groups = groupByTier(visible)
  const dayIndex = day ? days.indexOf(day) : 0
  const olderDay = days[dayIndex + 1] ?? null
  const newerDay = dayIndex > 0 ? days[dayIndex - 1] : null

  return (
    <div className="m-screen" data-testid="reports-list">
      <header className="m-masthead">
        <h1 className="m-title">Reports</h1>
        <p className="m-sub">
          {visible.length} read{visible.length === 1 ? '' : 's'}
          {day ? ` · ${day}` : ''}
        </p>
        {cachedAt ? (
          <p className="m-cached" data-testid="cached-banner">
            offline — {cachedAt}
          </p>
        ) : null}
      </header>

      <nav className="m-datenav" aria-label="Date navigation">
        <button
          type="button"
          className="m-btn"
          disabled={!newerDay}
          onClick={() => onDay(newerDay)}
          data-testid="day-newer"
        >
          ‹ Newer
        </button>
        <span className="m-daylabel">{day ?? days[0] ?? '—'}</span>
        <button
          type="button"
          className="m-btn"
          disabled={!olderDay}
          onClick={() => onDay(olderDay)}
          data-testid="day-older"
        >
          Older ›
        </button>
      </nav>

      {groups.length === 0 ? (
        <p className="m-empty" data-testid="reports-empty">
          No reads for this day.
        </p>
      ) : (
        groups.map((g) => (
          <section key={g.tier} className="m-group" data-testid={`tier-group-${g.tier}`}>
            <h2 className="m-heading">
              {g.label} <span className="m-count">{g.rows.length}</span>
            </h2>
            {g.tier === ('country' as ReportTier) ? (
              <CountryGroup rows={g.rows} onOpen={onOpen} />
            ) : (
              <ul className="m-list">
                {g.rows.map((r) => (
                  <ReportRowItem key={r.id} row={r} onOpen={onOpen} />
                ))}
              </ul>
            )}
          </section>
        ))
      )}

      {consolidation ? (
        <section className="m-group" data-testid="journal-group">
          <h2 className="m-heading">Journal</h2>
          <ul className="m-list">
            <li>
              <button type="button" className="m-row" onClick={onOpenConsolidation}>
                <span className="m-row-head">
                  <span className="m-row-target">Latest consolidation</span>
                </span>
                <span className="m-row-meta">
                  Journal · {consolidation.producedAt.slice(0, 10)}
                </span>
                <span className="m-row-lead">{consolidation.title}</span>
              </button>
            </li>
          </ul>
        </section>
      ) : null}
    </div>
  )
}

export default memo(ReportsListImpl)
