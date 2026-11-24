import { describe, it, expect } from 'vitest'
import {
  DEFAULT_WINDOW_CHOICE,
  DEFAULT_WINDOW_HOURS,
  groundingFamily,
  parseTurnContract,
  windowChoiceWords,
  windowForRequest,
} from './consultContract'

const NOW = Date.parse('2026-11-14T09:00:00Z')

describe('the window a turn is sent with (ConsultRequest.window)', () => {
  it('defaults to the server’s declared 168 h, ending now', () => {
    expect(DEFAULT_WINDOW_CHOICE).toBe('168h')
    expect(DEFAULT_WINDOW_HOURS).toBe(168)
    expect(windowForRequest('168h', { startMs: 0, endMs: 1 }, NOW)).toEqual({
      hours: 168, end: '2026-11-14T09:00:00.000Z', label: 'console 168h',
    })
  })

  it('the time band sends its absolute ends; an unreadable band falls back to the default span', () => {
    const band = { startMs: Date.parse('2026-11-13T09:00:00Z'), endMs: NOW }
    expect(windowForRequest('band', band, NOW)).toEqual({
      start: '2026-11-13T09:00:00.000Z', end: '2026-11-14T09:00:00.000Z', label: 'console time band',
    })
    expect(windowForRequest('band', { startMs: NOW, endMs: NOW }, NOW)).toMatchObject({ hours: 168 })
    expect(windowForRequest('band', { startMs: NaN, endMs: NOW }, NOW)).toMatchObject({ hours: 168 })
  })

  it('never sends more than the four keys the server accepts', () => {
    for (const c of ['24h', '168h', '720h', 'band'] as const) {
      expect(Object.keys(windowForRequest(c, { startMs: NOW - 3600e3, endMs: NOW }, NOW)).length).toBeLessThanOrEqual(4)
    }
  })

  it('names the window for the point line', () => {
    expect(windowChoiceWords('168h', '24h')).toBe('7d')
    expect(windowChoiceWords('24h', '7d')).toBe('24h')
    expect(windowChoiceWords('720h', '24h')).toBe('30d')
    expect(windowChoiceWords('band', '24h')).toBe('24h (time band)')
  })
})

describe('parseTurnContract', () => {
  it('is null for no contract at all', () => {
    expect(parseTurnContract(null)).toBeNull()
    expect(parseTurnContract(undefined)).toBeNull()
    expect(parseTurnContract('x')).toBeNull()
  })

  it('leaves a part the server did not send as null', () => {
    const c = parseTurnContract({ schema: 'consult_contract/1', armed: false, on_screen: {} })!
    expect(c.window).toBeNull()
    expect(c.coverage).toBeNull()
    expect(c.hypotheses).toBeNull()
    expect(c.grounding).toBeNull()
    expect(c.costStop).toBeNull()
  })

  it('drops a coverage day missing a count instead of printing a zero', () => {
    const c = parseTurnContract({
      coverage: {
        state: 'measured',
        days: [{ day: '2026-11-13', signals: 3, sources: 1, findings: 0, events: 0 }, { day: '2026-11-14', signals: null }],
        zero_days: [], sparse_days: [], gaps: { paused_sources: [], error_sources: [{ id: 'x', errors: null }, { id: 'y', errors: 2 }] },
      },
    })!
    expect(c.coverage!.days.map((d) => d.day)).toEqual(['2026-11-13'])
    expect(c.coverage!.errorSources).toEqual([{ id: 'y', errors: 2 }])
  })

  it('reads an unmeasured coverage and an unmeasured reach as such', () => {
    const c = parseTurnContract({ coverage: { state: 'unavailable', reason: 'read_failed: Timeout' }, reach: { state: 'unmeasured' } })!
    expect(c.coverage).toMatchObject({ state: 'unavailable', reason: 'read_failed: Timeout' })
    expect(c.reach).toMatchObject({ state: 'unmeasured', citedDated: null })
  })

  it('reads hypotheses, grounding counts and the cost stop', () => {
    const c = parseTurnContract({
      hypotheses: { conforming: false, repair: 'still_failing', problems: ['p sums to 1.3'], items: [{ explanation: 'x', p: 0.9, for: [{ kind: 'finding', id: 'f' }], test: { observable: 'o', by: '2026-12-01', p_observable: 0.2 } }], something_else_p: 0.4 },
      grounding: { mode: 'record_only', applied: true, counts: { cut_no_row: 2, cut_general_knowledge: 1, kept_with_rows: 4 } },
      cost_stop: 'stopped',
    })!
    expect(c.hypotheses!.items[0]).toMatchObject({ p: 0.9, forRefs: ['finding:f'], observable: 'o', pObservable: 0.2 })
    expect(c.hypotheses!.conforming).toBe(false)
    expect(groundingFamily(c.grounding!, 'cut')).toBe(3)
    expect(groundingFamily(c.grounding!, 'kept')).toBe(4)
    expect(c.costStop).toBe('stopped')
  })
})
