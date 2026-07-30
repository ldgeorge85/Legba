/**
 * Tests for wallModel (P1-7) — cursor lifecycle, movers grouping/sorting,
 * top-severe ordering, and the health rollup. Pure logic, no DOM.
 */
import { describe, it, expect } from 'vitest'
import {
  DEFAULT_LOOKBACK_HOURS,
  MAX_LOOKBACK_DAYS,
  WALL_CURSOR_KEY,
  bandChangeDeskLabel,
  bandDirectionTone,
  buildMovers,
  healthRollup,
  loadWallCursor,
  resolveWallCursor,
  sincePath,
  storeWallCursor,
  topSevereVerified,
  type SinceFinding,
  type SinceResponse,
} from './wallModel'
import type { AnalystCadenceRow, SourceFiringRow } from '@/lib/api'

const NOW = Date.parse('2026-07-24T12:00:00Z')

// ---------------------------------------------------------------------------
// Cursor lifecycle
// ---------------------------------------------------------------------------

describe('resolveWallCursor', () => {
  it('first-ever open → 24h default lookback', () => {
    const r = resolveWallCursor(null, NOW)
    expect(r.firstVisit).toBe(true)
    expect(r.clamped).toBe(false)
    expect(Date.parse(r.cursor)).toBe(NOW - DEFAULT_LOOKBACK_HOURS * 3_600_000)
  })

  it('an invalid stored value falls back to the 24h default', () => {
    const r = resolveWallCursor('not-a-date', NOW)
    expect(r.firstVisit).toBe(true)
    expect(Date.parse(r.cursor)).toBe(NOW - DEFAULT_LOOKBACK_HOURS * 3_600_000)
  })

  it('a valid stored cursor is used verbatim', () => {
    const stored = '2026-07-23T08:30:00.000Z'
    const r = resolveWallCursor(stored, NOW)
    expect(r.firstVisit).toBe(false)
    expect(r.clamped).toBe(false)
    expect(Date.parse(r.cursor)).toBe(Date.parse(stored))
  })

  it('clamps a cursor older than the 90d server bound (route 400s beyond it)', () => {
    const ancient = new Date(NOW - 200 * 86_400_000).toISOString()
    const r = resolveWallCursor(ancient, NOW)
    expect(r.clamped).toBe(true)
    const age = NOW - Date.parse(r.cursor)
    expect(age).toBeLessThanOrEqual(MAX_LOOKBACK_DAYS * 86_400_000)
  })

  it('clamps a future cursor (clock skew) back to now', () => {
    const future = new Date(NOW + 3_600_000).toISOString()
    const r = resolveWallCursor(future, NOW)
    expect(r.clamped).toBe(true)
    expect(Date.parse(r.cursor)).toBe(NOW)
  })
})

describe('load/storeWallCursor', () => {
  function memStorage(): Pick<Storage, 'getItem' | 'setItem'> & { map: Map<string, string> } {
    const map = new Map<string, string>()
    return {
      map,
      getItem: (k: string) => map.get(k) ?? null,
      setItem: (k: string, v: string) => void map.set(k, v),
    }
  }

  it('round-trips server_now through the storage key', () => {
    const s = memStorage()
    storeWallCursor('2026-07-24T11:59:00Z', s)
    expect(s.map.get(WALL_CURSOR_KEY)).toBe('2026-07-24T11:59:00Z')
    expect(loadWallCursor(s)).toBe('2026-07-24T11:59:00Z')
  })

  it('never moves the cursor backwards (a stale poll cannot rewind)', () => {
    const s = memStorage()
    storeWallCursor('2026-07-24T11:00:00Z', s)
    storeWallCursor('2026-07-24T10:00:00Z', s)
    expect(s.map.get(WALL_CURSOR_KEY)).toBe('2026-07-24T11:00:00Z')
  })

  it('ignores an unparseable server_now', () => {
    const s = memStorage()
    storeWallCursor('garbage', s)
    expect(s.map.size).toBe(0)
  })

  it('tolerates a null storage (private mode)', () => {
    expect(loadWallCursor(null)).toBe(null)
    expect(() => storeWallCursor('2026-07-24T11:00:00Z', null)).not.toThrow()
  })
})

describe('sincePath', () => {
  it('URL-encodes the cursor', () => {
    expect(sincePath('2026-07-24T11:00:00+00:00')).toBe(
      '/v3/since?cursor=2026-07-24T11%3A00%3A00%2B00%3A00',
    )
  })
})

// ---------------------------------------------------------------------------
// Movers
// ---------------------------------------------------------------------------

function emptySince(overrides: Partial<SinceResponse> = {}): SinceResponse {
  return {
    cursor: '2026-07-24T10:00:00Z',
    server_now: '2026-07-24T12:00:00Z',
    counts: {},
    new_findings: { items: [], total: 0, truncated: false },
    superseded: { items: [], total: 0, truncated: false },
    band_changes: { items: [], total: 0, truncated: false },
    situations: { items: [], total: 0, truncated: false },
    alerts: { items: [], total: 0, truncated: false },
    ...overrides,
  }
}

describe('bandDirectionTone', () => {
  it('colors risk direction, not coverage events', () => {
    expect(bandDirectionTone('deterioration')).toBe('bad')
    expect(bandDirectionTone('improvement')).toBe('good')
    expect(bandDirectionTone('evidence-gained')).toBe('neutral')
    expect(bandDirectionTone('evidence-lost')).toBe('neutral')
    expect(bandDirectionTone('indeterminate')).toBe('neutral')
  })
})

describe('bandChangeDeskLabel', () => {
  it('humanizes a g20/watch desk target id to its country name', () => {
    expect(bandChangeDeskLabel('country_g20_br')).toBe('Brazil')
    expect(bandChangeDeskLabel('country_watch_sd')).toBe('Sudan')
  })

  it('never renders the raw snake_case id for a non-desk target', () => {
    expect(bandChangeDeskLabel('japan_news')).toBe('Japan News')
  })
})

describe('buildMovers', () => {
  it('sorts band changes deterioration-first, then newest', () => {
    const mk = (direction: string, changed_at: string) => ({
      target_id: 't',
      dimension: 'd',
      from_band: 'low',
      to_band: 'high',
      direction,
      severity: 'high',
      from_scorecard_row_id: 'a',
      to_scorecard_row_id: 'b',
      changed_at,
    })
    const since = emptySince({
      band_changes: {
        items: [
          mk('improvement', '2026-07-24T11:00:00Z'),
          mk('deterioration', '2026-07-24T10:00:00Z'),
          mk('deterioration', '2026-07-24T11:30:00Z'),
          mk('evidence-lost', '2026-07-24T11:45:00Z'),
        ],
        total: 4,
        truncated: false,
      },
    })
    const view = buildMovers(since)
    expect(view.bandChanges.map((c) => c.direction)).toEqual([
      'deterioration',
      'deterioration',
      'evidence-lost',
      'improvement',
    ])
    // Within deteriorations: newest first.
    expect(view.bandChanges[0].changed_at).toBe('2026-07-24T11:30:00Z')
  })

  it('sorts situation edges appeared/escalating first, then intensity', () => {
    const mk = (change: string, intensity: number, id: string) => ({
      id,
      name: id,
      target_id: null,
      category: 'geopolitical',
      change,
      from_status: null,
      to_status: 'active',
      status: 'active',
      last_event_at: null,
      updated_at: '2026-07-24T11:00:00Z',
      intensity_score: intensity,
    })
    const since = emptySince({
      situations: {
        items: [mk('resolved', 99, 'a'), mk('escalating', 5, 'b'), mk('appeared', 1, 'c'), mk('escalating', 9, 'd')],
        total: 4,
        truncated: false,
      },
    })
    const view = buildMovers(since)
    expect(view.situationEdges.map((s) => s.id)).toEqual(['c', 'd', 'b', 'a'])
  })

  it('carries totals + truncation honestly and derives the empty state', () => {
    const empty = buildMovers(emptySince())
    expect(empty.isEmpty).toBe(true)

    const notEmpty = buildMovers(
      emptySince({ superseded: { items: [], total: 41, truncated: true } }),
    )
    expect(notEmpty.isEmpty).toBe(false)
    expect(notEmpty.supersededCount).toBe(41)
    expect(notEmpty.supersededTruncated).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Top severe verified findings
// ---------------------------------------------------------------------------

describe('topSevereVerified', () => {
  const mk = (
    id: string,
    severity: string | null,
    eff: number,
    produced_at: string,
  ): SinceFinding => ({
    id,
    analyst_id: 'a',
    target_id: 't',
    title: id,
    severity,
    confidence: eff,
    faithfulness_score: 1,
    effective_confidence: eff,
    produced_at,
  })

  it('orders by severity, then effective confidence, then recency; caps at n', () => {
    const items = [
      mk('low-late', 'low', 0.9, '2026-07-24T11:00:00Z'),
      mk('crit', 'critical', 0.6, '2026-07-24T09:00:00Z'),
      mk('high-strong', 'high', 0.9, '2026-07-24T08:00:00Z'),
      mk('high-weak', 'high', 0.7, '2026-07-24T10:00:00Z'),
      mk('med', 'medium', 0.99, '2026-07-24T11:30:00Z'),
      mk('none', null, 1, '2026-07-24T11:45:00Z'),
    ]
    const top = topSevereVerified(items, 5)
    expect(top.map((f) => f.id)).toEqual(['crit', 'high-strong', 'high-weak', 'med', 'low-late'])
  })

  it('breaks a full tie by recency', () => {
    const items = [
      mk('older', 'high', 0.8, '2026-07-24T08:00:00Z'),
      mk('newer', 'high', 0.8, '2026-07-24T11:00:00Z'),
    ]
    expect(topSevereVerified(items, 2)[0].id).toBe('newer')
  })
})

// ---------------------------------------------------------------------------
// Health rollup
// ---------------------------------------------------------------------------

function srcRow(over: Partial<SourceFiringRow>): SourceFiringRow {
  return {
    source_id: 's',
    state: 'active',
    signals_24h: 0,
    signals_7d: 0,
    last_seen_at: null,
    age_seconds: null,
    last_poll_outcome: null,
    recent_error_count: 0,
    status: 'firing',
    freshness_grade: 'ungraded',
    budget_minutes: null,
    ...over,
  }
}

function anaRow(over: Partial<AnalystCadenceRow>): AnalystCadenceRow {
  return {
    analyst_id: 'a',
    last_run_at: null,
    age_seconds: null,
    runs_1h: 0,
    runs_24h: 0,
    last_outcome: null,
    status: 'healthy',
    ...over,
  }
}

describe('healthRollup', () => {
  it('sums signals_24h, counts sub-hour sources, errors, stale/never analysts', () => {
    const h = healthRollup(
      [
        srcRow({ source_id: 'a', signals_24h: 10, age_seconds: 120 }),
        srcRow({ source_id: 'b', signals_24h: 5, age_seconds: 7200 }),
        srcRow({ source_id: 'c', signals_24h: 0, status: 'error' }),
      ],
      [
        anaRow({ analyst_id: 'x' }),
        anaRow({ analyst_id: 'y', status: 'stale' }),
        anaRow({ analyst_id: 'z', status: 'never' }),
      ],
    )
    expect(h.signals24h).toBe(15)
    expect(h.sourcesSeenLastHour).toBe(1)
    expect(h.sourceErrors).toBe(1)
    expect(h.analystsStale).toBe(1)
    expect(h.analystsNever).toBe(1)
    expect(h.worst).toBe('red')
  })

  it('amber when analysts are stale or no source fired within the hour', () => {
    expect(
      healthRollup([srcRow({ age_seconds: 120 })], [anaRow({ status: 'stale' })]).worst,
    ).toBe('amber')
    expect(healthRollup([srcRow({ age_seconds: 7200 })], [anaRow({})]).worst).toBe('amber')
  })

  it('green when sources fire and analysts are healthy', () => {
    expect(healthRollup([srcRow({ age_seconds: 60 })], [anaRow({})]).worst).toBe('green')
  })

  it('empty inputs → green zeros (honest, not fabricated red)', () => {
    const h = healthRollup([], [])
    expect(h).toMatchObject({ signals24h: 0, sourcesTotal: 0, analystsTotal: 0, worst: 'green' })
  })
})
