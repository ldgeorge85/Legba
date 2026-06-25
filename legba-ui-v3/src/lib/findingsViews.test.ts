import { describe, it, expect, beforeEach } from 'vitest'
import {
  DEFAULT_FILTER,
  FLAT_CLUSTER_ID,
  buildSupersessionIndex,
  clusterBySituation,
  clusterKeyOf,
  hasClusteringData,
  isCriticFlagged,
  isSupersessionSummary,
  loadSavedViews,
  mapTailEnvelope,
  persistSavedViews,
  removeView,
  rowDedupKey,
  severityRank,
  signalRestToRow,
  signalTailToRow,
  situationIdOf,
  sortFindings,
  surfacedConfidence,
  upsertView,
  type FindingLike,
  type SavedView,
} from './findingsViews'
import type { RegistryEvent } from './ws'

/** Build a minimal RegistryEvent for the signal-tail mapper tests. */
function ev(payload: Record<string, unknown> | undefined, ts = '2026-06-03T00:00:00Z'): RegistryEvent {
  return { type: 'event', subject: 'legba.signals.x', ts, payload } as RegistryEvent
}

function f(over: Partial<FindingLike> & { id: string }): FindingLike {
  return { severity: null, produced_at: '2026-06-01T00:00:00Z', ...over }
}

describe('severityRank + sortFindings', () => {
  it('ranks severities high→low, unknown last', () => {
    expect(severityRank('critical')).toBeGreaterThan(severityRank('high'))
    expect(severityRank('high')).toBeGreaterThan(severityRank('low'))
    expect(severityRank(null)).toBe(0)
    expect(severityRank('bogus')).toBe(0)
  })

  it('recency sort orders newest first', () => {
    const rows = [
      f({ id: 'a', produced_at: '2026-06-01T00:00:00Z' }),
      f({ id: 'b', produced_at: '2026-06-03T00:00:00Z' }),
      f({ id: 'c', produced_at: '2026-06-02T00:00:00Z' }),
    ]
    expect(sortFindings(rows, 'recency').map((r) => r.id)).toEqual(['b', 'c', 'a'])
  })

  it('severity sort orders by severity then recency, and does not mutate input', () => {
    const rows = [
      f({ id: 'low-new', severity: 'low', produced_at: '2026-06-05T00:00:00Z' }),
      f({ id: 'crit-old', severity: 'critical', produced_at: '2026-06-01T00:00:00Z' }),
      f({ id: 'crit-new', severity: 'critical', produced_at: '2026-06-04T00:00:00Z' }),
      f({ id: 'high', severity: 'high', produced_at: '2026-06-02T00:00:00Z' }),
    ]
    const sorted = sortFindings(rows, 'severity')
    expect(sorted.map((r) => r.id)).toEqual(['crit-new', 'crit-old', 'high', 'low-new'])
    // original untouched
    expect(rows[0].id).toBe('low-new')
  })
})

describe('saved views (localStorage)', () => {
  beforeEach(() => localStorage.clear())

  it('round-trips through persist/load', () => {
    const v: SavedView = { ...DEFAULT_FILTER, name: 'criticals', severity: 'critical', sort: 'severity' }
    persistSavedViews([v])
    const loaded = loadSavedViews()
    expect(loaded).toHaveLength(1)
    expect(loaded[0]).toMatchObject({ name: 'criticals', severity: 'critical', sort: 'severity' })
  })

  it('returns [] for missing or malformed storage', () => {
    expect(loadSavedViews()).toEqual([])
    localStorage.setItem('legba.findings.views', '{not json')
    expect(loadSavedViews()).toEqual([])
    localStorage.setItem('legba.findings.views', '{"not":"array"}')
    expect(loadSavedViews()).toEqual([])
  })

  it('upsert replaces by name; remove drops by name', () => {
    const a: SavedView = { ...DEFAULT_FILTER, name: 'a' }
    const b: SavedView = { ...DEFAULT_FILTER, name: 'b' }
    let views = upsertView([], a)
    views = upsertView(views, b)
    expect(views.map((v) => v.name)).toEqual(['a', 'b'])
    // replace 'a' (target_id changed) — same length, no dup
    views = upsertView(views, { ...a, target_id: 'brazil' })
    expect(views.filter((v) => v.name === 'a')).toHaveLength(1)
    expect(views.find((v) => v.name === 'a')?.target_id).toBe('brazil')
    views = removeView(views, 'a')
    expect(views.map((v) => v.name)).toEqual(['b'])
  })
})

describe('clusterKeyOf — mirrors backend derive_signature', () => {
  it('uses explicit situation_id / situation_signature → sit:<id>', () => {
    expect(clusterKeyOf(f({ id: 'a', data: { situation_id: 's1' } }))).toBe('sit:s1')
    expect(clusterKeyOf(f({ id: 'b', data: { situation_signature: 'sig-x' } }))).toBe('sit:sig-x')
    // explicit wins over derivable entities
    expect(
      clusterKeyOf(f({ id: 'c', data: { situation_id: 's2', entities: ['lula'] } })),
    ).toBe('sit:s2')
  })

  it('derives sig:<topic>|<sorted entity tokens> when no explicit id', () => {
    const key = clusterKeyOf(
      f({ id: 'a', data: { category: 'Energy', entities: ['Petrobras', 'Lula'] } }),
    )
    // topic lowercased; entity tokens lowercased, deduped, sorted
    expect(key).toBe('sig:energy|lula,petrobras')
  })

  it('two phrasings of the same situation collide on the derived signature', () => {
    const a = clusterKeyOf(f({ id: 'a', data: { topic: 'coup', actors: ['Army', 'Lula'] } }))
    const b = clusterKeyOf(f({ id: 'b', data: { topic: 'coup', actors: ['lula', 'army'] } }))
    expect(a).toBe(b)
  })

  it('falls back topic→_ with entities, and returns null for a bare summary', () => {
    expect(clusterKeyOf(f({ id: 'a', data: { entities: ['x1'] } }))).toBe('sig:_|x1')
    expect(clusterKeyOf(f({ id: 'b', data: { summary: 'nothing structured' } }))).toBeNull()
    expect(clusterKeyOf(f({ id: 'c' }))).toBeNull()
  })

  it('situationIdOf reads the explicit binding only', () => {
    expect(situationIdOf(f({ id: 'a', data: { situation_id: 's1' } }))).toBe('s1')
    expect(situationIdOf(f({ id: 'b', data: { situation_signature: 'sg' } }))).toBe('sg')
    expect(situationIdOf(f({ id: 'c', data: { entities: ['x'] } }))).toBeNull()
    expect(situationIdOf(f({ id: 'd' }))).toBeNull()
  })
})

describe('situation clustering', () => {
  it('returns a single flat pseudo-cluster when no situation data', () => {
    const rows = [f({ id: 'a' }), f({ id: 'b' })]
    expect(hasClusteringData(rows)).toBe(false)
    const clusters = clusterBySituation(rows)
    expect(clusters).toHaveLength(1)
    expect(clusters[0].flat).toBe(true)
    expect(clusters[0].situation_id).toBe(FLAT_CLUSTER_ID)
    expect(clusters[0].rows).toHaveLength(2)
  })

  it('groups by signature; ungrouped go to a flat tail', () => {
    const rows = [
      f({ id: 'a', data: { situation_id: 's1' }, produced_at: '2026-06-01T00:00:00Z' }),
      f({ id: 'b', data: { situation_id: 's1' }, produced_at: '2026-06-02T00:00:00Z' }),
      f({ id: 'c', data: { situation_id: 's2' } }),
      f({ id: 'd' }), // no situation → flat tail
    ]
    expect(hasClusteringData(rows)).toBe(true)
    const clusters = clusterBySituation(rows)
    const s1 = clusters.find((c) => c.situation_id === 'sit:s1')
    const s2 = clusters.find((c) => c.situation_id === 'sit:s2')
    const flat = clusters.find((c) => c.flat)
    // s1 cluster: latest first (b is newer), a is superseded history
    expect(s1?.rows.map((r) => r.id)).toEqual(['b', 'a'])
    expect(s1?.latest?.id).toBe('b')
    expect(s1?.history?.map((r) => r.id)).toEqual(['a'])
    // s2 is a singleton — no history
    expect(s2?.rows.map((r) => r.id)).toEqual(['c'])
    expect(s2?.history).toEqual([])
    expect(flat?.rows.map((r) => r.id)).toEqual(['d'])
  })

  it('A SET OF NEAR-DUP FINDINGS FOR ONE SITUATION RENDERS AS ONE CLUSTER (acceptance)', () => {
    // 4 near-dup re-assessments of one situation, varying produced_at.
    const rows = [
      f({ id: 'v1', data: { situation_id: 'brazil-coup' }, produced_at: '2026-06-01T00:00:00Z' }),
      f({ id: 'v2', data: { situation_id: 'brazil-coup' }, produced_at: '2026-06-02T00:00:00Z' }),
      f({ id: 'v3', data: { situation_id: 'brazil-coup' }, produced_at: '2026-06-03T00:00:00Z' }),
      f({ id: 'v4', data: { situation_id: 'brazil-coup' }, produced_at: '2026-06-04T00:00:00Z' }),
    ]
    const clusters = clusterBySituation(rows)
    expect(clusters).toHaveLength(1) // ONE cluster, not 4 rows
    const c = clusters[0]
    expect(c.flat).toBe(false)
    expect(c.latest?.id).toBe('v4') // freshest assessment surfaced
    expect(c.history?.map((r) => r.id)).toEqual(['v3', 'v2', 'v1']) // history latest-first
  })

  it('picks latest deterministically (produced_at then id) without an index', () => {
    const rows = [
      f({ id: 'aaa', data: { situation_id: 's' }, produced_at: '2026-06-03T00:00:00Z' }),
      f({ id: 'zzz', data: { situation_id: 's' }, produced_at: '2026-06-03T00:00:00Z' }),
    ]
    const c = clusterBySituation(rows)[0]
    expect(c.latest?.id).toBe('zzz') // tie on produced_at → largest id wins
    expect(c.confirmed).toBe(false)
  })

  it('honors the enabled=false flag (forced flat even with data)', () => {
    const rows = [f({ id: 'a', data: { situation_id: 's1' } })]
    const clusters = clusterBySituation(rows, false)
    expect(clusters).toHaveLength(1)
    expect(clusters[0].flat).toBe(true)
  })

  it('sorts clustered groups by size, flat tail last', () => {
    const rows = [
      f({ id: 'big1', data: { situation_id: 'big' } }),
      f({ id: 'big2', data: { situation_id: 'big' } }),
      f({ id: 'big3', data: { situation_id: 'big' } }),
      f({ id: 'small1', data: { situation_id: 'small' } }),
      f({ id: 'small2', data: { situation_id: 'small' } }),
      f({ id: 'loose' }),
    ]
    const clusters = clusterBySituation(rows)
    expect(clusters.map((c) => c.situation_id)).toEqual(['sit:big', 'sit:small', FLAT_CLUSTER_ID])
  })
})

describe('supersession index (P-FS summary finding)', () => {
  function summary(over: { id: string; clusters: unknown; produced_at?: string }): FindingLike {
    return f({
      id: over.id,
      produced_at: over.produced_at ?? '2026-06-05T00:00:00Z',
      data: { sub_handler: 'finding_supersession', clusters: over.clusters },
    })
  }

  it('recognizes a P-FS summary finding', () => {
    expect(isSupersessionSummary(summary({ id: 's', clusters: [] }))).toBe(true)
    expect(isSupersessionSummary(f({ id: 'x', data: { situation_id: 'a' } }))).toBe(false)
  })

  it('builds an authoritative latest/superseded lookup', () => {
    const idx = buildSupersessionIndex([
      summary({
        id: 'sum',
        clusters: [
          {
            situation_signature: 'sit:s1',
            latest_finding_id: 'v3',
            superseded_finding_ids: ['v1', 'v2'],
            reason: 'situation_id',
            score: 1.0,
          },
        ],
      }),
    ])
    expect(idx.superseded.has('v1')).toBe(true)
    expect(idx.superseded.has('v2')).toBe(true)
    expect(idx.superseded.has('v3')).toBe(false)
    expect(idx.latestOf.get('v1')).toBe('v3')
    expect(idx.bySignature.get('sit:s1')?.reason).toBe('situation_id')
  })

  it('clustering honors the authoritative latest even against recency', () => {
    // v1 is NEWER than v2 by produced_at, but the summary names v2 as latest.
    const rows = [
      f({ id: 'v1', data: { situation_id: 's1' }, produced_at: '2026-06-09T00:00:00Z' }),
      f({ id: 'v2', data: { situation_id: 's1' }, produced_at: '2026-06-01T00:00:00Z' }),
      summary({
        id: 'sum',
        clusters: [
          {
            situation_signature: 'sit:s1',
            latest_finding_id: 'v2',
            superseded_finding_ids: ['v1'],
            reason: 'situation_id',
            score: 1.0,
          },
        ],
      }),
    ]
    const idx = buildSupersessionIndex(rows)
    const clusters = clusterBySituation(rows, true, idx)
    const c = clusters.find((x) => x.situation_id === 'sit:s1')
    expect(c?.latest?.id).toBe('v2') // authoritative, overrides recency
    expect(c?.confirmed).toBe(true)
    expect(c?.reason).toBe('situation_id')
    expect(c?.score).toBe(1.0)
    // the summary finding itself lives in the flat tail, not a situation
    const flat = clusters.find((x) => x.flat)
    expect(flat?.rows.map((r) => r.id)).toEqual(['sum'])
  })

  it('newer summary wins on signature collision', () => {
    const idx = buildSupersessionIndex([
      summary({
        id: 'old',
        produced_at: '2026-06-01T00:00:00Z',
        clusters: [{ situation_signature: 'sit:s', latest_finding_id: 'old-latest', superseded_finding_ids: [] }],
      }),
      summary({
        id: 'new',
        produced_at: '2026-06-09T00:00:00Z',
        clusters: [{ situation_signature: 'sit:s', latest_finding_id: 'new-latest', superseded_finding_ids: [] }],
      }),
    ])
    expect(idx.bySignature.get('sit:s')?.latest_finding_id).toBe('new-latest')
  })

  it('ignores malformed cluster refs', () => {
    const idx = buildSupersessionIndex([
      summary({ id: 'sum', clusters: [{ situation_signature: 'sit:s' }, 'junk', { latest_finding_id: 'x' }] }),
    ])
    expect(idx.bySignature.size).toBe(0)
  })
})

describe('mapTailEnvelope (NATS live-tail)', () => {
  it('maps a finding envelope to a feed row', () => {
    const row = mapTailEnvelope({
      id: 'fnd-1',
      title: 'Coup risk rising',
      severity: 'high',
      confidence: 0.7,
      target_id: 'brazil',
      analyst_id: 'cred.analyst',
      produced_at: '2026-06-03T12:00:00Z',
      derived_from: ['sig-1', 'sig-2'],
      data: { situation_id: 's9' },
    })
    expect(row).not.toBeNull()
    expect(row?.id).toBe('fnd-1')
    expect(row?.severity).toBe('high')
    expect(row?.target_id).toBe('brazil')
    expect(row?.derived_from).toEqual(['sig-1', 'sig-2'])
    expect(row?.live).toBe(true)
    expect(situationIdOf(row!)).toBe('s9')
  })

  it('accepts finding_id alias and supplies defaults', () => {
    const row = mapTailEnvelope({ finding_id: 'x' })
    expect(row?.id).toBe('x')
    expect(row?.kind).toBe('finding')
    expect(row?.title).toBe('(live finding)')
    expect(typeof row?.produced_at).toBe('string')
  })

  it('returns null without a usable id', () => {
    expect(mapTailEnvelope(undefined)).toBeNull()
    expect(mapTailEnvelope({})).toBeNull()
    expect(mapTailEnvelope({ title: 'no id' })).toBeNull()
  })
})

describe('S3 critic actuator (surfacedConfidence / isCriticFlagged)', () => {
  it('surfaces effective_confidence when present, else raw confidence', () => {
    expect(surfacedConfidence({ confidence: 0.9, effective_confidence: 0.3 })).toBe(0.3)
    expect(surfacedConfidence({ confidence: 0.7 })).toBe(0.7)
    expect(surfacedConfidence({})).toBeNull()
  })

  it('flags findings whose critic score fell under the threshold', () => {
    expect(isCriticFlagged({ critic_score: 0.4 })).toBe(true)
    expect(isCriticFlagged({ critic_score: 0.8 })).toBe(false)
    // never critiqued → not flagged.
    expect(isCriticFlagged({ critic_score: null })).toBe(false)
    expect(isCriticFlagged({})).toBe(false)
    // custom threshold.
    expect(isCriticFlagged({ critic_score: 0.6 }, 0.7)).toBe(true)
  })

  it('mapTailEnvelope leaves critic_score null on a fresh live finding', () => {
    const row = mapTailEnvelope({ id: 'x', confidence: 0.8 })
    expect(row?.critic_score).toBeNull()
    // effective falls back to the finding's own confidence pre-critique.
    expect(row?.effective_confidence).toBe(0.8)
  })

  it('mapTailEnvelope stamps source=finding', () => {
    expect(mapTailEnvelope({ id: 'x' })?.source).toBe('finding')
  })
})

// --- #90 unified feed: signal mappers + the clusterKeyOf source guard ---

describe('signalRestToRow / signalTailToRow (unified feed signals)', () => {
  it('maps a REST signal row: source/kind=signal, severity null, geo/tags/source carried', () => {
    const row = signalRestToRow({
      id: 's1',
      title: 'Quake',
      confidence: 0.7,
      source_id: 'usgs.quakes',
      geo: ['brazil'],
      tags: ['seismic'],
      produced_at: '2026-06-02T00:00:00Z',
      data: { summary: 'M5.0' },
    })
    expect(row.source).toBe('signal')
    expect(row.kind).toBe('signal')
    expect(row.severity).toBeNull()
    expect(row.confidence).toBe(0.7)
    expect(row.source_id).toBe('usgs.quakes')
    expect(row.geo).toEqual(['brazil'])
    expect(row.tags).toEqual(['seismic'])
    expect(row.produced_at).toBe('2026-06-02T00:00:00Z')
  })

  it('drops a 0.0 source_credibility confidence to null (no misleading c=0.00)', () => {
    expect(signalRestToRow({ id: 's2', confidence: 0 }).confidence).toBeNull()
    expect(signalRestToRow({ id: 's3' }).confidence).toBeNull()
  })

  it('falls back produced_at → event_timestamp when produced_at absent', () => {
    expect(signalRestToRow({ id: 's4', event_timestamp: '2026-06-02T01:00:00Z' }).produced_at).toBe(
      '2026-06-02T01:00:00Z',
    )
  })

  it('tail signal with a real id carries NO dedupKey (id is stable)', () => {
    const row = signalTailToRow(ev({ id: 'sig-x', title: 'Live' }))
    expect(row?.id).toBe('sig-x')
    expect(row?.source).toBe('signal')
    expect(row?.live).toBe(true)
    expect(row?.dedupKey).toBeUndefined()
  })

  it('id-less tail signal synthesizes an ephemeral sig: dedupKey', () => {
    const row = signalTailToRow(ev({ title: 'Live no id' }))
    expect(row?.dedupKey).toBeDefined()
    expect(row?.id.startsWith('sig:')).toBe(true)
    // content_hash is accepted as a stable id when present.
    expect(signalTailToRow(ev({ content_hash: 'abc123' }))?.id).toBe('abc123')
  })

  it('two id-less signals on the same subject+ts get DISTINCT dedupKeys (no collapse)', () => {
    const a = signalTailToRow(ev({ title: 'no id A' }))
    const b = signalTailToRow(ev({ title: 'no id B' }))
    expect(a?.dedupKey).toBeDefined()
    expect(b?.dedupKey).toBeDefined()
    expect(a?.dedupKey).not.toBe(b?.dedupKey)
  })

  it('tail signal with no payload returns null', () => {
    expect(signalTailToRow(ev(undefined))).toBeNull()
  })

  it('rowDedupKey is composite (source:id) so a finding + signal sharing an id never collide', () => {
    const finding = mapTailEnvelope({ id: 'shared' })!
    const signal = signalRestToRow({ id: 'shared' })
    expect(rowDedupKey(finding)).toBe('finding:shared')
    expect(rowDedupKey(signal)).toBe('signal:shared')
    expect(rowDedupKey(finding)).not.toBe(rowDedupKey(signal))
  })
})

describe('clusterKeyOf source guard (signals never cluster)', () => {
  it('returns null for a signal even when its data carries a geo entity token', () => {
    // A finding with the same geo DOES cluster — proving the discriminant is
    // `source`, not the data shape.
    expect(clusterKeyOf(f({ id: 'fnd', data: { geo: ['brazil'], topic: 'unrest' } }))).not.toBeNull()
    const signal = signalRestToRow({ id: 'sig', geo: ['brazil'], data: { geo: ['brazil'] } })
    expect(clusterKeyOf(signal)).toBeNull()
  })
})
