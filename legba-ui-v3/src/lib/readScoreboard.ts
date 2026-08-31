/**
 * Pure shaping for the Read Scoreboard (D2e) — no DOM, no fetch, all of it
 * unit-testable, following the `judgeStats.ts` / `productionGauge.ts` idiom.
 *
 * The server returns a sparse (day, kind) grid: a cell exists only where an
 * event happened. Everything here is about turning that sparseness into an
 * honest picture — most importantly by DENSIFYING the day strip, because a
 * day on which the operator read nothing is the single most informative cell
 * on the panel and a sparse array would silently omit it.
 */

import type { ReadRollupDay, ReadRollupResponse } from '@/lib/api'

/**
 * The two kinds that constitute a TRUST operation, per PREMISE §2.2 — the
 * ones the premise review measured at zero. Kept separate from the general
 * read count on purpose: folding them in would hide the thing under test.
 */
export const DRILL_KINDS: readonly string[] = ['lineage_walk', 'citation_drill']

export interface KindTotal {
  kind: string
  events: number
  /** Distinct days this kind was seen on — a burst and a habit differ. */
  days: number
}

/**
 * Fold the (day, kind) grid into per-kind totals, busiest first.
 *
 * `days` is carried alongside `events` because the two answer different
 * questions and the panel prints both: 40 events on one day is a session; 40
 * events across 20 days is a habit, and only the second one settles the
 * wager.
 */
export function kindTotals(cells: readonly ReadRollupDay[]): KindTotal[] {
  const events = new Map<string, number>()
  const days = new Map<string, Set<string>>()
  for (const c of cells) {
    events.set(c.event_kind, (events.get(c.event_kind) ?? 0) + c.events)
    const seen = days.get(c.event_kind) ?? new Set<string>()
    seen.add(c.day)
    days.set(c.event_kind, seen)
  }
  return [...events.entries()]
    .map(([kind, n]) => ({ kind, events: n, days: days.get(kind)?.size ?? 0 }))
    .sort((a, b) => b.events - a.events || a.kind.localeCompare(b.kind))
}

export interface DayCell {
  day: string
  events: number
  /** Was the morning read opened on this day? The wager's per-day verdict. */
  briefRead: boolean
}

/**
 * The last `n` calendar days, DENSE — oldest first, with a zero cell for every
 * day nothing happened.
 *
 * The densification is the point. The server sends only the days that have
 * events, so rendering its rows directly would draw a strip with no gaps and
 * make a week of silence look like a week of reading. The strip is anchored on
 * the newest day PRESENT IN THE DATA rather than on the browser's clock, so
 * the panel never invents "today" from a client clock the rest of this plane
 * has already decided not to trust (see readTelemetry.ts on skew).
 */
export function recentDays(cells: readonly ReadRollupDay[], n: number): DayCell[] {
  if (cells.length === 0) return []
  const byDay = new Map<string, { events: number; briefRead: boolean }>()
  for (const c of cells) {
    const prev = byDay.get(c.day) ?? { events: 0, briefRead: false }
    byDay.set(c.day, {
      events: prev.events + c.events,
      briefRead: prev.briefRead || c.event_kind === 'brief_read',
    })
  }
  const newest = [...byDay.keys()].sort().at(-1)!
  const anchor = new Date(`${newest}T00:00:00Z`)

  const out: DayCell[] = []
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(anchor)
    d.setUTCDate(d.getUTCDate() - i)
    const key = d.toISOString().slice(0, 10)
    const hit = byDay.get(key)
    out.push({ day: key, events: hit?.events ?? 0, briefRead: hit?.briefRead ?? false })
  }
  return out
}

/**
 * The one-line subtitle.
 *
 * It leads with the ratio, not the total, because "opened on 12 of 30 days"
 * is the wager's metric and "412 reads" is not — a number that large is
 * mostly panel opens and would flatter a morning nobody actually read.
 */
export function readSummaryLine(data: ReadRollupResponse): string {
  const t = data.totals
  if (t.active_days === 0) {
    return `nothing read in ${t.window_days}d — the read log is empty`
  }
  return (
    `morning read opened on ${t.brief_read_days}/${t.window_days}d · ` +
    `${t.reads_today} read${t.reads_today === 1 ? '' : 's'} today · ` +
    `${t.reads_this_week} this week over ${t.sessions_this_week} session` +
    `${t.sessions_this_week === 1 ? '' : 's'}`
  )
}
