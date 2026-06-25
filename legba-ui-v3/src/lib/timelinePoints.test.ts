import { describe, it, expect } from 'vitest'
import {
  BAND,
  KIND_COLOR,
  SEVERITY_COLOR,
  findingMarkColor,
  findingPoints,
  pointOpacity,
  signalPoints,
  situationPoints,
  situationSpans,
  spanOpacity,
  timeDomain,
  type TLFinding,
  type TLSignal,
  type TLSituation,
} from './timelinePoints'

const sig: TLSignal[] = [
  { id: 's1', title: 'sig', category: 'news', produced_at: '2026-06-01T00:00:00Z' },
  { id: 's2', title: 'bad ts', category: 'news', produced_at: 'not-a-date' },
]
const find: TLFinding[] = [
  { id: 'f1', title: 'find', analyst_id: 'a', severity: 'high', produced_at: '2026-06-02T00:00:00Z' },
]
const sit: TLSituation[] = [
  {
    id: 'sit1',
    name: 'Escalation',
    status: 'active',
    category: 'conflict',
    produced_at: '2026-06-01T00:00:00Z',
    last_event_at: '2026-06-03T00:00:00Z',
    event_count: 5,
    intensity_score: 0.8,
  },
  {
    id: 'sit2',
    name: 'No last event',
    status: 'resolved',
    category: 'conflict',
    produced_at: '2026-06-02T00:00:00Z',
    last_event_at: null,
    event_count: 1,
    intensity_score: 0.2,
  },
]

describe('point derivation', () => {
  it('signalPoints land on band 1 and drop bad timestamps', () => {
    const pts = signalPoints(sig)
    expect(pts).toHaveLength(1)
    expect(pts[0].band).toBe(BAND.signal)
    expect(pts[0].id).toBe('s1')
  })

  it('findingPoints land on band 2 with analyst/severity subtitle + severity field', () => {
    const pts = findingPoints(find)
    expect(pts[0].band).toBe(BAND.finding)
    expect(pts[0].subtitle).toBe('a · high')
    expect(pts[0].severity).toBe('high')
  })

  it('findingMarkColor maps severity → palette, falls back to finding color', () => {
    expect(findingMarkColor('critical')).toBe(SEVERITY_COLOR.critical)
    expect(findingMarkColor('low')).toBe(SEVERITY_COLOR.low)
    expect(findingMarkColor(null)).toBe(KIND_COLOR.finding)
    expect(findingMarkColor('bogus')).toBe(KIND_COLOR.finding)
  })

  it('situationPoints land on band 3', () => {
    const pts = situationPoints(sit)
    expect(pts).toHaveLength(2)
    expect(pts[0].band).toBe(BAND.situation)
    expect(pts[0].subtitle).toContain('active')
  })
})

describe('situationSpans', () => {
  it('spans open → last_event_at', () => {
    const spans = situationSpans(sit)
    const s1 = spans.find((s) => s.id === 'sit1')!
    expect(s1.start).toBe(new Date('2026-06-01T00:00:00Z').getTime())
    expect(s1.end).toBe(new Date('2026-06-03T00:00:00Z').getTime())
    expect(s1.band).toBe(BAND.situation)
  })

  it('collapses to a point when last_event_at is missing', () => {
    const spans = situationSpans(sit)
    const s2 = spans.find((s) => s.id === 'sit2')!
    expect(s2.start).toBe(s2.end)
  })
})

describe('timeDomain', () => {
  it('returns undefined for empty input', () => {
    expect(timeDomain([], [])).toBeUndefined()
  })

  it('covers points and spans with padding', () => {
    const dom = timeDomain([...signalPoints(sig), ...findingPoints(find)], situationSpans(sit))
    expect(dom).toBeDefined()
    const [min, max] = dom!
    // min should be <= earliest (2026-06-01), max >= latest (2026-06-03)
    expect(min).toBeLessThanOrEqual(new Date('2026-06-01T00:00:00Z').getTime())
    expect(max).toBeGreaterThanOrEqual(new Date('2026-06-03T00:00:00Z').getTime())
  })
})

describe('event-time preference', () => {
  it('plots a signal by published_at when present, else produced_at', () => {
    const rows: TLSignal[] = [
      {
        id: 'e1', title: 'event', category: 'news',
        produced_at: '2026-06-10T00:00:00Z', // fetched later
        published_at: '2026-06-01T00:00:00Z', // happened earlier
      },
      { id: 'e2', title: 'no-pub', category: 'news', produced_at: '2026-06-05T00:00:00Z' },
    ]
    const [p1, p2] = signalPoints(rows)
    expect(p1.ts).toBe(new Date('2026-06-01T00:00:00Z').getTime()) // event time wins
    expect(p2.ts).toBe(new Date('2026-06-05T00:00:00Z').getTime()) // fallback
  })
})

describe('spanOpacity', () => {
  it('fades active > dormant > closed and scales with intensity', () => {
    const active = spanOpacity({ status: 'active', intensity: 8 })
    const dormant = spanOpacity({ status: 'dormant', intensity: 8 })
    const closed = spanOpacity({ status: 'closed', intensity: 8 })
    expect(active).toBeGreaterThan(dormant)
    expect(dormant).toBeGreaterThan(closed)
    // lower intensity dims an active span
    expect(spanOpacity({ status: 'active', intensity: 0.5 })).toBeLessThan(active)
    // bounded
    expect(active).toBeLessThanOrEqual(1)
    expect(closed).toBeGreaterThanOrEqual(0.1)
  })
})

describe('pointOpacity', () => {
  it('is full for now/future and fades with age toward the floor', () => {
    const now = Date.parse('2026-06-15T00:00:00Z')
    expect(pointOpacity(now, now)).toBe(1)
    expect(pointOpacity(now + 1000, now)).toBe(1)
    const fresh = pointOpacity(now - 1 * 24 * 3600_000, now)
    const old = pointOpacity(now - 20 * 24 * 3600_000, now)
    expect(fresh).toBeGreaterThan(old)
    expect(old).toBeGreaterThanOrEqual(0.2) // floor
  })
})
