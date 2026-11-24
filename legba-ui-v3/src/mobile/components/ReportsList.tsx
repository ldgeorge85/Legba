/**
 * ReportsList — the navigator, and the landing screen.
 *
 * The v2 Reports panel was a split pane: list left, rendered report right. A
 * phone has room for one of those at a time, so the split becomes a push: this
 * list, then the report, with Back as the way home.
 *
 * Every row answers the questions a reader asks before deciding to read: what
 * tier, about what, from when — and the lead, the line that decides. (M-0a: the
 * unexplained verify pill left the card; it was an engine figure.)
 */

import { memo } from 'react'
import {
  groupByTier,
  PROFILE_TIERS,
  readerSubjectName,
  rowsForDay,
  tierLabel,
  type ReportRow,
} from '../mobileModel'
import { rosterTagOf, type SubjectIndex } from '@/lib/deskNames'
import { rosterLabel, subjectTierIds, type ProfileFrame } from '@/lib/profileUi'

/**
 * The roster leg a read's subject sits on — read off the subject's declared
 * tags against the profile's `rosters.subject_tags` (`lib/deskNames.ts`), never
 * out of its id. Null when the subject is on no declared leg.
 */
export function rosterClass(targetId: string | null, subjects?: SubjectIndex): string | null {
  return rosterTagOf(targetId, subjects)
}

/** The sub-group heading of a subject on no declared roster leg. */
const OFF_ROSTER = 'other'

function ReportRowItem({
  row,
  onOpen,
  tiers,
}: {
  row: ReportRow
  onOpen: (id: string) => void
  tiers: readonly ProfileFrame[]
}) {
  return (
    <li>
      <button
        type="button"
        className="m-card"
        data-testid="report-row"
        data-report-id={row.id}
        onClick={() => onOpen(row.id)}
      >
        <span className="m-row-head">
          {/* U-1 — a list cached before rows were named may still carry the
              machine id as the title; name it here too. */}
          <span className="m-row-target">
            {row.targetId && row.target === row.targetId ? readerSubjectName(row.targetId) : row.target}
          </span>
        </span>
        <span className="m-row-meta">
          {tierLabel(row.tier, tiers)} · {row.day || 'undated'}
        </span>
        {/* M-0a — the summary row's lead is its title's headline; a title
            that is only a subject and a date has none, and nothing is drawn. */}
        {row.leadThread ? <span className="m-row-lead">{row.leadThread}</span> : null}
      </button>
    </li>
  )
}

/**
 * A per-subject tier's reads (the exemplar's country tier), sub-grouped by the
 * roster leg each subject declares, headed by the profile's roster label.
 */
function RosterGroup({
  rows,
  onOpen,
  tiers,
  subjects,
}: {
  rows: ReportRow[]
  onOpen: (id: string) => void
  tiers: readonly ProfileFrame[]
  subjects?: SubjectIndex
}) {
  const byRoster = new Map<string, ReportRow[]>()
  for (const r of rows) {
    const key = rosterClass(r.targetId, subjects) ?? OFF_ROSTER
    const list = byRoster.get(key)
    if (list) list.push(r)
    else byRoster.set(key, [r])
  }
  return (
    <>
      {[...byRoster.entries()].map(([roster, group]) => (
        <div key={roster} className="m-subgroup" data-testid={`roster-group-${roster}`}>
          <h3 className="m-subheading">
            {roster === OFF_ROSTER ? OFF_ROSTER : rosterLabel(roster)} <span className="m-count">{group.length}</span>
          </h3>
          <ul className="m-list">
            {group.map((r) => (
              <ReportRowItem key={r.id} row={r} onOpen={onOpen} tiers={tiers} />
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
  /** M-0a — set when a refetch failed: "No connection. Showing what was loaded at …". */
  offlineNote: string | null
  /** The read tiers (the projection's when the plane answers; the profile's otherwise). */
  tiers?: readonly ProfileFrame[]
  /** The subject roster (`useSubjectIndex`); the session's index when omitted. */
  subjects?: SubjectIndex
}

function ReportsListImpl({
  rows,
  day,
  days,
  onOpen,
  onDay,
  consolidation,
  onOpenConsolidation,
  offlineNote,
  tiers = PROFILE_TIERS,
  subjects,
}: ReportsListProps) {
  const visible = rowsForDay(rows, day)
  const groups = groupByTier(visible, tiers)
  const rosterTiers = subjectTierIds(tiers)
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
        {offlineNote ? (
          <p className="m-cached" data-testid="offline-banner" role="status">
            {offlineNote}
          </p>
        ) : null}
        {/* The workstation redirects a phone here (`lib/phoneSurface.ts`); this
            is the way back to the desk, remembered for the tab's session. */}
        <a className="m-desktop" href="/?desktop=1" data-testid="desktop-view">
          Desktop view
        </a>
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
            {rosterTiers.has(g.tier) ? (
              <RosterGroup rows={g.rows} onOpen={onOpen} tiers={tiers} subjects={subjects} />
            ) : (
              <ul className="m-list">
                {g.rows.map((r) => (
                  <ReportRowItem key={r.id} row={r} onOpen={onOpen} tiers={tiers} />
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
              <button type="button" className="m-card" onClick={onOpenConsolidation}>
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
