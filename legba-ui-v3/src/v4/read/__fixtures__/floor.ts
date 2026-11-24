/**
 * W4 F-3 — a floored world record for the reader tests, built from the LIVE
 * world fixture rather than invented: block 8 becomes the one the region floor
 * KEPT (order rank 17 — the order had ranked it below the cut) and the first
 * not-selected row the one it DISPLACED (order rank 8). The `floor` audit line
 * is the producer's shape (`assembly_floor.stamp_floor`), six regions offered,
 * every one carried.
 */
import worldRow from './assembly.world.json'
import type { ReadFindingRow } from '@/lib/assemblyModel'

type Json = Record<string, unknown>

export function floorGroups(americasCarried = 1): Json[] {
  return [
    { group_id: 'region_africa', group_name: 'Africa', offered: 6, carried: 2, best_order_rank: 12, reason: 'in_prefix' },
    { group_id: 'region_americas', group_name: 'Americas', offered: 6, carried: americasCarried, best_order_rank: 17, reason: 'kept_by_floor' },
    { group_id: 'region_europe', group_name: 'Europe', offered: 6, carried: 1, best_order_rank: 2, reason: 'in_prefix' },
    { group_id: 'region_indo_pacific', group_name: 'Indo-Pacific', offered: 10, carried: 2, best_order_rank: 6, reason: 'in_prefix' },
    { group_id: 'region_mena', group_name: 'MENA', offered: 4, carried: 1, best_order_rank: 3, reason: 'in_prefix' },
    { group_id: 'thematic:escalation_composition', group_name: 'escalation_composition (cross-region thematic)', offered: 1, carried: 1, best_order_rank: 1, reason: 'in_prefix' },
    { group_id: 'thematic:escalation_dyad', group_name: 'escalation_dyad (cross-region thematic)', offered: 0, carried: 0, best_order_rank: null, reason: 'no_candidate' },
  ]
}

export function flooredWorldRow(opts: { stale?: boolean; americasCarried?: number } = {}): ReadFindingRow {
  const row = JSON.parse(JSON.stringify(worldRow)) as Json
  const asm = ((row.data as Json).data as Json).assembly as Json
  const blocks = asm.blocks as Json[]
  blocks.forEach((b, i) => {
    b.order_rank = i + 1
  })
  const kept = blocks[blocks.length - 1]
  kept.order_rank = 17
  kept.carried_by_floor = true
  if (opts.stale) {
    kept.evidence_age_h = 196.6
    kept.floor_stale = true
  }
  const drops = asm.drops as Json
  const ns = drops.not_selected as Json[]
  ns.forEach((d, i) => {
    d.order_rank = i + 9
  })
  ns[0].order_rank = 8
  ns[0].displaced_by_floor = true
  asm.floor = {
    version: 'subject_group_floor.v1',
    grain: 'region',
    per_group: 1,
    cap: 16,
    cap_effective: 16,
    honoured: (opts.americasCarried ?? 1) >= 1,
    promoted: [String(kept.finding_id)],
    displaced: [String(ns[0].finding_id)],
    groups: floorGroups(opts.americasCarried ?? 1),
    stale_hours: 72,
  }
  return row as unknown as ReadFindingRow
}
