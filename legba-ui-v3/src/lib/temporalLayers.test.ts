/**
 * The temporal layer model (W8 UI-B4/time): valid time vs record time, read
 * from the route shapes as they are served, with absence kept as absence.
 */
import { describe, expect, it } from 'vitest'
import {
  densityBuckets,
  eventRow,
  findingRow,
  instantZ,
  layerFigure,
  rowsInLayer,
  rowsWithoutAxis,
  signalRow,
  situationRow,
  sourcesOn,
  temporalLayers,
} from './temporalLayers'

const T = (iso: string) => Date.parse(iso)

describe('the row adapters read the two clocks off the live route shapes', () => {
  it('a signal: valid = the publisher\'s date, record = created_at (the route\'s fetched_at)', () => {
    // The `/signals` route projects fetched_at onto event_timestamp,
    // produced_at AND created_at — all three are the record time.
    const r = signalRow({
      id: 's1',
      title: 'A headline',
      category: 'Conflict',
      created_at: '2026-10-04T10:21:18Z',
      event_timestamp: '2026-10-04T10:21:18Z',
      produced_at: '2026-10-04T10:21:18Z',
      data: { published_at: '2026-10-04T09:37:12+00:00' },
    })
    expect(r.validMs).toBe(T('2026-10-04T09:37:12Z'))
    expect(r.recordMs).toBe(T('2026-10-04T10:21:18Z'))
    expect(r.category).toBe('conflict')
  })

  it('a signal whose source printed no date has NO valid time — never the record time', () => {
    const r = signalRow({ id: 's2', created_at: '2026-10-04T10:00:00Z', data: {} })
    expect(r.validMs).toBeNull()
    expect(r.recordMs).toBe(T('2026-10-04T10:00:00Z'))
    expect(r.title).toBe('(untitled)')
  })

  it('a finding, a situation (the substrate `name`/`status` shape) and an event', () => {
    const f = findingRow({ id: 'f', produced_at: '2026-10-04T10:15:19Z', created_at: '2026-10-04T10:15:20Z', severity: 'high' })
    expect([f.validMs, f.recordMs]).toEqual([T('2026-10-04T10:15:19Z'), T('2026-10-04T10:15:20Z')])
    const s = situationRow({
      id: 'sit',
      name: 'Strait closed',
      status: 'closed',
      last_event_at: '2026-09-23T17:18:12Z',
      created_at: '2026-09-23T17:25:01Z',
    })
    expect(s.title).toBe('Strait closed')
    expect(s.subtitle).toBe('closed')
    expect([s.validMs, s.recordMs]).toEqual([T('2026-09-23T17:18:12Z'), T('2026-09-23T17:25:01Z')])
    const e = eventRow({ id: 'e', valid_from: '2026-10-03T10:51:00Z', time_start: '2026-10-02T00:00:00Z', produced_at: '2026-10-04T02:15:47Z' })
    expect([e.validMs, e.recordMs]).toEqual([T('2026-10-03T10:51:00Z'), T('2026-10-04T02:15:47Z')])
  })

  it('an unparseable instant is absent, never 0', () => {
    const f = findingRow({ id: 'f', produced_at: 'not a date', created_at: null })
    expect(f.validMs).toBeNull()
    expect(f.recordMs).toBeNull()
  })
})

describe('layers, figures and density', () => {
  const window = { startMs: T('2026-10-04T00:00:00Z'), endMs: T('2026-10-04T12:00:00Z') }
  const rows = [
    // valid yesterday, recorded today — in the RECORD layer only.
    signalRow({ id: 'a', created_at: '2026-10-04T08:00:00Z', data: { published_at: '2026-10-03T20:00:00Z' } }),
    // valid and recorded today.
    signalRow({ id: 'b', created_at: '2026-10-04T09:30:00Z', data: { published_at: '2026-10-04T09:00:00Z' } }),
    // no valid time.
    signalRow({ id: 'c', created_at: '2026-10-04T11:00:00Z', data: {} }),
  ]

  it('one layer per source per clock, ids `<source>:<time>`', () => {
    expect(temporalLayers(sourcesOn({ signals: true, findings: false, events: true, entities: true }), window).map((l) => l.id)).toEqual([
      'signals:valid',
      'signals:record',
      'events:valid',
      'events:record',
    ])
  })

  it('the two clocks hold different rows for the same window', () => {
    const [valid, record] = temporalLayers(['signals'], window)
    expect(rowsInLayer(rows, valid).map((r) => r.id)).toEqual(['b'])
    expect(rowsInLayer(rows, record).map((r) => r.id)).toEqual(['a', 'b', 'c'])
    expect(rowsWithoutAxis(rows, 'valid')).toBe(1)
    expect(rowsWithoutAxis(rows, 'record')).toBe(0)
  })

  it('a figure is AS OF the latest record time among the rows it counts', () => {
    const [valid, record] = temporalLayers(['signals'], window)
    expect(layerFigure(rows, valid)).toEqual({ state: 'measured', value: 1, unit: 'rows', asOf: '2026-10-04 09:30Z' })
    expect(layerFigure(rows, record)).toEqual({ state: 'measured', value: 3, unit: 'rows', asOf: '2026-10-04 11:00Z' })
  })

  it('a layer with no rows in the window is *no read yet*, never 0', () => {
    const [findingsValid] = temporalLayers(['findings'], window)
    const fig = layerFigure(rows, findingsValid)
    expect(fig.state).toBe('no_read_yet')
    expect(fig.value).toBeUndefined()
    expect(fig.asOf).toEqual({ absent: 'no row in the window' })
  })

  it('density buckets count each clock across the span', () => {
    const lo = T('2026-10-03T12:00:00Z')
    const hi = T('2026-10-04T12:00:00Z')
    // 24 hourly buckets: valid at 20:00 (8) and 09:00 (21); record at 08:00 (20), 09:30 (21), 11:00 (23).
    const v = densityBuckets(rows, 'valid', lo, hi, 24)
    const r = densityBuckets(rows, 'record', lo, hi, 24)
    expect(v.reduce((a, b) => a + b, 0)).toBe(2)
    expect([v[8], v[21]]).toEqual([1, 1])
    expect([r[20], r[21], r[23]]).toEqual([1, 1, 1])
  })

  it('instantZ prints the as-of form', () => {
    expect(instantZ(T('2026-10-04T09:05:59Z'))).toBe('2026-10-04 09:05Z')
  })
})
