/**
 * Pure shaping for the Read Scoreboard (D2e).
 *
 * The load-bearing one is DENSIFICATION: the server sends only the days that
 * have events, so a naive strip would draw a fortnight of silence as a
 * fortnight of reading. A wager whose scoreboard cannot show a zero day is
 * not an instrument.
 */

import { describe, it, expect } from 'vitest'
import { DRILL_KINDS, kindTotals, readSummaryLine, recentDays } from './readScoreboard'
import type { ReadRollupDay, ReadRollupResponse } from '@/lib/api'

function cell(day: string, kind: string, events: number, sessions = 1): ReadRollupDay {
  return { day, event_kind: kind, events, sessions }
}

function rollup(over: Partial<ReadRollupResponse> = {}): ReadRollupResponse {
  return {
    since: '2026-07-31',
    totals: {
      reads_today: 0,
      reads_this_week: 0,
      brief_reads_today: 0,
      brief_reads_this_week: 0,
      brief_read_days: 0,
      active_days: 0,
      sessions_this_week: 0,
      window_days: 30,
    },
    days: [],
    ...over,
  }
}

describe('kindTotals', () => {
  it('folds the grid per kind, busiest first, with distinct day counts', () => {
    const out = kindTotals([
      cell('2026-08-29', 'panel_open', 10),
      cell('2026-08-28', 'panel_open', 5),
      cell('2026-08-29', 'brief_read', 1),
      cell('2026-08-28', 'brief_read', 1),
      cell('2026-08-27', 'brief_read', 1),
    ])
    expect(out).toEqual([
      { kind: 'panel_open', events: 15, days: 2 },
      { kind: 'brief_read', events: 3, days: 3 },
    ])
  })

  it('keeps events and days separate — a burst is not a habit', () => {
    // 40 events on one day vs 40 across twenty. Same total, opposite finding,
    // and only the second settles the wager.
    const burst = kindTotals([cell('2026-08-29', 'finding_open', 40)])
    expect(burst[0]).toEqual({ kind: 'finding_open', events: 40, days: 1 })

    const habit = kindTotals(
      Array.from({ length: 20 }, (_, i) =>
        cell(`2026-08-${String(i + 1).padStart(2, '0')}`, 'finding_open', 2),
      ),
    )
    expect(habit[0]).toEqual({ kind: 'finding_open', events: 40, days: 20 })
  })

  it('is empty for an empty log, not undefined', () => {
    expect(kindTotals([])).toEqual([])
  })

  it('names the two trust operations §2.2 measured at zero', () => {
    expect([...DRILL_KINDS].sort()).toEqual(['citation_drill', 'lineage_walk'])
  })
})

describe('recentDays', () => {
  it('DENSIFIES — a day with nothing gets a zero cell, not an omission', () => {
    const out = recentDays(
      [cell('2026-08-29', 'brief_read', 1), cell('2026-08-26', 'panel_open', 3)],
      5,
    )
    expect(out.map((d) => d.day)).toEqual([
      '2026-08-25',
      '2026-08-26',
      '2026-08-27',
      '2026-08-28',
      '2026-08-29',
    ])
    expect(out.map((d) => d.events)).toEqual([0, 3, 0, 0, 1])
  })

  it('marks only the days the morning read was actually opened', () => {
    const out = recentDays(
      [
        cell('2026-08-29', 'brief_read', 1),
        cell('2026-08-29', 'panel_open', 7),
        cell('2026-08-28', 'panel_open', 4),
      ],
      3,
    )
    expect(out.map((d) => d.briefRead)).toEqual([false, false, true])
    // A busy day with no brief_read is still NOT a morning read.
    expect(out[1]).toEqual({ day: '2026-08-28', events: 4, briefRead: false })
  })

  it('anchors on the newest day IN THE DATA, never the browser clock', () => {
    // The rest of this plane already decided not to trust the client clock
    // (readTelemetry's skew bound); the strip must not reintroduce it.
    const out = recentDays([cell('2026-01-05', 'brief_read', 1)], 3)
    expect(out.map((d) => d.day)).toEqual(['2026-01-03', '2026-01-04', '2026-01-05'])
  })

  it('crosses a month boundary correctly', () => {
    const out = recentDays([cell('2026-03-02', 'brief_read', 1)], 4)
    expect(out.map((d) => d.day)).toEqual([
      '2026-02-27',
      '2026-02-28',
      '2026-03-01',
      '2026-03-02',
    ])
  })

  it('returns nothing for an empty log rather than inventing a window', () => {
    expect(recentDays([], 14)).toEqual([])
  })
})

describe('readSummaryLine', () => {
  it('says an empty log is empty, in words', () => {
    expect(readSummaryLine(rollup())).toBe(
      'nothing read in 30d — the read log is empty',
    )
  })

  it('leads with the ratio the wager is graded on', () => {
    const line = readSummaryLine(
      rollup({
        totals: {
          reads_today: 14,
          reads_this_week: 61,
          brief_reads_today: 1,
          brief_reads_this_week: 4,
          brief_read_days: 12,
          active_days: 18,
          sessions_this_week: 5,
          window_days: 30,
        },
      }),
    )
    expect(line.startsWith('morning read opened on 12/30d')).toBe(true)
    expect(line).toContain('14 reads today')
    expect(line).toContain('61 this week over 5 sessions')
  })

  it('does not pluralise a single read or a single session', () => {
    const line = readSummaryLine(
      rollup({
        totals: {
          reads_today: 1,
          reads_this_week: 1,
          brief_reads_today: 1,
          brief_reads_this_week: 1,
          brief_read_days: 1,
          active_days: 1,
          sessions_this_week: 1,
          window_days: 7,
        },
      }),
    )
    expect(line).toContain('1 read today')
    expect(line).toContain('over 1 session')
  })
})
