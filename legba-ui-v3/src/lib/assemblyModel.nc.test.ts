/**
 * N-C (2026-11-11) — the world-read selection fields reach the reader: the
 * weighed tension, contested facts, shared threads, the shared-thread slot and
 * the declared-empty slot. The sentences are the record body's own words
 * (`world_block_evidence.balance_sentence` / `contested_sentence`).
 */
import { describe, it, expect } from 'vitest'
import worldRow from '@/v4/read/__fixtures__/assembly.world.json'
import {
  balanceSentence,
  contestedSentence,
  floorSentence,
  projectAssembly,
  type ReadFindingRow,
} from './assemblyModel'

const world = worldRow as unknown as ReadFindingRow

function nc(): ReadFindingRow {
  const row = JSON.parse(JSON.stringify(world)) as ReadFindingRow
  const asm = (row.data as { data: { assembly: Record<string, unknown> } }).data.assembly
  const blocks = asm.blocks as Record<string, unknown>[]
  blocks[0].carried_by_thread = true
  blocks[0].contested_facts = [
    {
      event_terms: ['ahmed', 'arrest', 'haroun'],
      affirm_label: 'reported',
      affirmed_by: [
        {
          signal_id: 'cea8e1dc',
          source_id: 'source.jpost.frontpage',
          title: 'Sudan military arrests former ruling party chairman, Islamist leader Ahmed Haroun',
        },
      ],
      disputed_by: [
        {
          signal_id: '522747fd',
          source_id: 'source.dabanga.sudan',
          title: 'Alleged Sudan ‘arrest’ of ICC fugitive Ahmed Haroun in doubt',
        },
      ],
    },
  ]
  asm.tensions = [
    {
      kind: 'carried_pair',
      a: { ordinal: 1, span_index: 0 },
      b: { ordinal: 2, span_index: 0 },
      b_ref: null,
      b_carried: true,
      statement: 'The escalation and military_posture desks report on X.',
      evidence_balance: {
        basis: 'distinct_cited_sources.v1',
        a: { n_signals: 8, n_sources: 6, sources: [], newest_at: '2026-11-11T08:21:00+00:00' },
        b: { n_signals: 5, n_sources: 3, sources: [], newest_at: null },
        shared_signals: 0,
        favours: 'a',
      },
    },
  ]
  asm.threads = [
    {
      units: ['country_g20_gb', 'country_g20_us'],
      signal_ids: ['027f2cbd'],
      titles: ['U.S. charges three ‘Iranian operatives’ over U.K. airbase plot'],
      max_magnitude: 0.6,
      block_ordinals: [],
    },
  ]
  asm.carry_plan = {
    empty_slots: [
      { via_head_id: 'h', unit: 'hybrid_threat_composition', why: 'thematic_fallback_refused' },
    ],
  }
  return row
}

describe('N-C world-read selection fields', () => {
  it('projects every new field and leaves a pre-N-C record empty-handed', () => {
    const a = projectAssembly(nc())!
    expect(a.blocks[0].carried_by_thread).toBe(true)
    expect(a.blocks[0].contested_facts).toHaveLength(1)
    expect(a.tensions[0].evidence_balance?.favours).toBe('a')
    expect(a.threads).toHaveLength(1)
    expect(a.empty_slots?.[0].unit).toBe('hybrid_threat_composition')
    const old = projectAssembly(world)!
    expect(old.threads).toEqual([])
    expect(old.empty_slots).toEqual([])
    expect(old.blocks[0].contested_facts).toEqual([])
    expect(old.tensions.every((t) => t.evidence_balance === null)).toBe(true)
  })

  it('renders the balance and the contest in the record body’s own words', () => {
    const a = projectAssembly(nc())!
    expect(balanceSentence(a.tensions[0].evidence_balance, 'block 1', 'block 2')).toBe(
      'Evidence balance (cited signals only): block 1 cites 6 distinct sources, newest ' +
        '2026-11-11; block 2 cites 3 distinct sources — the count favours block 1.',
    )
    expect(contestedSentence(a.blocks[0].contested_facts![0])).toBe(
      'Contested in cited reporting — reported by jpost.frontpage ("Sudan military arrests ' +
        'former ruling party chairman, Islamist leader Ahmed Haroun") / disputed by ' +
        'dabanga.sudan ("Alleged Sudan ‘arrest’ of ICC fugitive Ahmed Haroun in doubt").',
    )
  })

  it('a declared-empty group is not a group the floor failed', () => {
    const sentence = floorSentence({
      version: 'subject_group_floor.v1',
      grain: 'region',
      per_group: 1,
      cap: 16,
      cap_effective: 16,
      honoured: true,
      promoted: [],
      displaced: [],
      stale_hours: 72,
      declared_empty: ['thematic:hybrid'],
      groups: [
        {
          group_id: 'region_africa',
          group_name: 'Africa',
          offered: 6,
          carried: 2,
          best_order_rank: 9,
          reason: 'in_prefix',
        },
        {
          group_id: 'thematic:hybrid',
          group_name: 'hybrid',
          offered: 1,
          carried: 0,
          best_order_rank: 5,
          reason: 'in_prefix',
        },
      ],
    })
    expect(sentence).toBe('1 region carried, floor honoured.')
  })
})
