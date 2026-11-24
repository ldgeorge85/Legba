/**
 * M-3a — the landing's data: "needs you" is what PAGED (the owner's rule),
 * Changed is the Board's ranked list plus the due forecasts, "since you last
 * looked" counts from the same answer, and one read per window.
 */
import { describe, expect, it } from 'vitest'
import { badgeCount, changedPath, changedRows, countsPath, pagedOnly, phoneWindowOf, sinceCounts, type PhoneSince } from './changedModel'

const ep = (id: string, why: string, at: string, paged: string | null) => ({
  id, kind: 'k', severity: 'high', summary: `alert ${id}`, subject_kind: 'target', subject_id: 'example_x',
  opened_at: at, last_raised_at: at, last_paged_at: paged, why,
})

const since: PhoneSince = {
  cursor: '2026-11-13T00:00:00Z',
  server_now: '2026-11-14T00:00:00Z',
  counts: {},
  new_findings: { items: [{ id: 'f1', analyst_id: 'a', target_id: 'example_x', title: 'A read', severity: 'high', confidence: 0.7, faithfulness_score: 0.8, effective_confidence: 0.7, produced_at: '2026-11-13T12:00:00Z' }], total: 1, truncated: false },
  superseded: { items: [], total: 0, truncated: false },
  band_changes: { items: [], total: 0, truncated: false },
  situations: { items: [{ id: 's1', name: 'A situation', target_id: null, category: 'c', change: 'escalating', from_status: 'watch', to_status: 'active', status: 'active', last_event_at: null, updated_at: '2026-11-13T06:00:00Z', intensity_score: 1 }], total: 1, truncated: false },
  alerts: { items: [], total: 0, truncated: false },
  forecasts_due: { items: [{ id: 'q1', region: 'example_x', event_class: 'e', window_start: '', window_end: '2026-11-12T00:00:00Z', p: 0.3, p_base: 0.2, method: 'm', method_version: null, scale_version: null, resolution_test: 'Will it?', resolved_by: null, issued_at: '', days_overdue: 1, mark: 'awaiting' }], total: 1, truncated: false },
  needs_you: {
    items: [ep('e1', 'paged', '2026-11-13T10:00:00Z', '2026-11-13T10:00:00Z'), ep('e2', 'open_at_severity', '2026-11-13T11:00:00Z', null), ep('e3', 'paged', '2026-11-13T20:00:00Z', '2026-11-13T20:00:00Z')],
    total: 3,
    truncated: false,
  },
}

describe('changedModel', () => {
  it('needs you is what paged, and only that', () => {
    expect(pagedOnly(since).map((e) => e.id)).toEqual(['e1', 'e3'])
    expect(pagedOnly(null)).toEqual([])
  })

  it('the rows: a read opens its page, a situation its Record, a due forecast its subject', () => {
    const rows = changedRows(since, (id) => id.toUpperCase())
    expect(rows.find((r) => r.kind === 'finding')?.open).toEqual({ read: 'f1' })
    expect(rows.find((r) => r.kind === 'situation')?.open).toMatchObject({ record: { kind: 'situation', id: 's1' } })
    const due = rows.find((r) => r.kind === 'forecast_due')!
    expect(due).toMatchObject({ open: { subject: 'example_x' }, word: 'awaiting confirmation', chip: 'forecast due', subject: 'EXAMPLE_X' })
  })

  it('since you last looked counts from the same answer; with no cursor everything is new', () => {
    const rows = changedRows(since, (id) => id)
    expect(sinceCounts(since, rows, '2026-11-13T09:00:00Z')).toEqual({ changed: 1, needsYou: 2 })
    expect(sinceCounts(since, rows, '2026-11-13T15:00:00Z')).toEqual({ changed: 0, needsYou: 1 })
    expect(sinceCounts(since, rows, null)).toEqual({ changed: 2, needsYou: 2 })
  })

  it('three windows; a read per window, to the minute', () => {
    expect(phoneWindowOf(6 * 3_600_000).id).toBe('24h')
    expect(phoneWindowOf(7 * 86_400_000).id).toBe('7d')
    expect(phoneWindowOf(30 * 86_400_000).id).toBe('30d')
    const now = Date.parse('2026-11-14T10:20:33Z')
    expect(changedPath(86_400_000, now)).toBe(`/v3/since?cursor=${encodeURIComponent('2026-11-13T10:20:00.000Z')}&include=needs_you`)
    expect(changedPath(86_400_000, now + 20_000)).toBe(changedPath(86_400_000, now))
  })
})

describe('roll 85: the badge reads the counts route', () => {
  it('asks /since/counts for the same window start as the list', () => {
    const now = Date.parse('2026-11-14T10:20:30Z')
    expect(countsPath(86_400_000, now)).toBe(`/v3/since/counts?cursor=${encodeURIComponent('2026-11-13T10:20:00.000Z')}`)
    expect(countsPath(86_400_000, now).split('cursor=')[1]).toBe(changedPath(86_400_000, now).split('cursor=')[1].split('&')[0])
  })

  it("uses the counts route's paged-only needs_you, and the list's paged rows until it answers", () => {
    const counts = { cursor: '', server_now: '', counts: {}, needs_you: 5, changed: 0, forecasts_due: 0 }
    expect(badgeCount(counts, since)).toBe(5)
    expect(badgeCount({ ...counts, needs_you: 0 }, since)).toBe(0)
    expect(badgeCount(null, since)).toBe(2)          // e1, e3 paged; e2 did not
    expect(badgeCount(undefined, null)).toBe(0)
  })
})
