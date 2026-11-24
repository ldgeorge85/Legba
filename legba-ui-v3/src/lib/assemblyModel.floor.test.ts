/**
 * W4 F-3 — the reader's half of the subject-group floor: `assembly.floor`, the
 * `order_rank` / `carried_by_floor` / `floor_stale` / `displaced_by_floor`
 * stamps, and the one floor sentence the ledger and the mobile view share.
 * Absence stays absence: a record with no floor projects `floor: null` and no
 * sentence, never a zero.
 */
import { describe, it, expect } from 'vitest'
import countryRow from '@/v4/read/__fixtures__/assembly.country.json'
import worldRow from '@/v4/read/__fixtures__/assembly.world.json'
import { flooredWorldRow } from '@/v4/read/__fixtures__/floor'
import { floorSentence, projectAssembly, type ReadFindingRow } from './assemblyModel'

describe('the floor projection', () => {
  it('projects the audit line, groups and stamps off a floored world record', () => {
    const a = projectAssembly(flooredWorldRow())!
    expect(a.floor).not.toBeNull()
    expect(a.floor!.version).toBe('subject_group_floor.v1')
    expect(a.floor!.grain).toBe('region')
    expect(a.floor!.honoured).toBe(true)
    expect(a.floor!.groups).toHaveLength(7)
    expect(a.floor!.groups[1]).toEqual({
      group_id: 'region_americas',
      group_name: 'Americas',
      offered: 6,
      carried: 1,
      best_order_rank: 17,
      reason: 'kept_by_floor',
    })
    // A no_candidate group keeps its absent rank as null, never 0.
    expect(a.floor!.groups[6].best_order_rank).toBeNull()
    const kept = a.blocks.filter((b) => b.carried_by_floor)
    expect(kept.map((b) => b.order_rank)).toEqual([17])
    expect(a.blocks[0].carried_by_floor).toBe(false)
    const displaced = a.drops!.not_selected.filter((r) => r.displaced_by_floor)
    expect(displaced.map((r) => r.order_rank)).toEqual([8])
  })

  it('projects floor: null and no stamps on records without one', () => {
    for (const row of [worldRow, countryRow]) {
      const a = projectAssembly(row as unknown as ReadFindingRow)!
      expect(a.floor).toBeNull()
      expect(a.blocks.every((b) => b.order_rank === null && !b.carried_by_floor)).toBe(true)
      expect(a.drops!.not_selected.every((r) => r.order_rank === null)).toBe(true)
      expect(floorSentence(a.floor ?? null)).toBeNull()
    }
  })
})

describe('the floor sentence', () => {
  it('says the floor held and how many blocks it kept — the body line, verbatim', () => {
    const a = projectAssembly(flooredWorldRow())!
    expect(floorSentence(a.floor ?? null)).toBe(
      '6 regions carried, floor honoured — 1 kept by the floor.',
    )
  })

  it('drops the suffix when the floor kept nothing', () => {
    const a = projectAssembly(flooredWorldRow())!
    expect(floorSentence({ ...a.floor!, promoted: [] })).toBe('6 regions carried, floor honoured.')
  })

  it('names the group a broken floor missed', () => {
    const a = projectAssembly(flooredWorldRow({ americasCarried: 0 }))!
    expect(floorSentence(a.floor ?? null)).toBe(
      '5 of 6 regions carried, floor NOT honoured: Americas.',
    )
  })
})
