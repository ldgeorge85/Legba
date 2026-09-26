/**
 * mobileModel — the Crossroads rule, tested without a DOM.
 *
 * The selection semantics are the whole product here, so they are tested as
 * pure functions against the LIVE read fixtures: a projection that silently
 * stopped finding a report's cited heads would break the phone surface in a way
 * no snapshot would catch.
 */
import { describe, it, expect } from 'vitest'

import worldRow from '@/v4/read/__fixtures__/assembly.world.json'
import countryRow from '@/v4/read/__fixtures__/assembly.country.json'
import thematicRow from '@/v4/read/__fixtures__/assembly.thematic.json'
import assessmentRow from '@/v4/read/__fixtures__/assessment.world.json'
import { projectAssembly, type ReadFindingRow } from '@/lib/assemblyModel'
import {
  dayKey,
  daysPresent,
  filterToEntity,
  groupByTier,
  leadThread,
  READ_ANALYST_CSV,
  reportEntities,
  reportFindings,
  reportSignals,
  rowsForDay,
  tierOf,
  toReportRow,
} from './mobileModel'

const WORLD = worldRow as unknown as ReadFindingRow
const COUNTRY = countryRow as unknown as ReadFindingRow
const THEMATIC = thematicRow as unknown as ReadFindingRow
const ASSESSMENT = assessmentRow as unknown as ReadFindingRow

describe('tier resolution', () => {
  it('resolves every tier from the producing analyst id', () => {
    expect(tierOf(WORLD)).toBe('world')
    expect(tierOf(ASSESSMENT)).toBe('assessment')
    expect(tierOf(COUNTRY)).toBe('country')
    expect(tierOf(THEMATIC)).toBe('thematic')
  })

  it('refuses a row from an analyst outside the read set', () => {
    expect(tierOf({ ...WORLD, analyst_id: 'country_critic' })).toBeNull()
    expect(toReportRow({ ...WORLD, analyst_id: 'country_critic' })).toBeNull()
  })

  it('does not depend on the payload tier field', () => {
    // `AssemblyTier` has no 'region' member and `projectAssembly` collapses a
    // rollup regime into 'assembly', so keying tiers off the payload would
    // mis-file a region read. The analyst id is the discriminator instead.
    const asRegion = { ...COUNTRY, analyst_id: 'region_composition' }
    expect(tierOf(asRegion)).toBe('region')
  })

  it('asks for every read producer in one request', () => {
    expect(READ_ANALYST_CSV.split(',')).toEqual([
      'world_assessor',
      'world_assessment',
      'region_composition',
      'country_composition',
      'escalation_composition',
    ])
  })
})

describe('navigator rows', () => {
  it('projects a world read into a row with lead, verify and day', () => {
    const row = toReportRow(WORLD, projectAssembly(WORLD))
    expect(row).not.toBeNull()
    expect(row!.tier).toBe('world')
    expect(row!.day).toBe('2026-09-03')
    expect(row!.leadThread.length).toBeGreaterThan(20)
    expect(row!.verifyScore).toBeTypeOf('number')
    expect(row!.verifyState).toBe('scored')
  })

  it('takes the lead thread from the LEAD block, not merely the first', () => {
    const assembly = projectAssembly(WORLD)!
    const leadOrdinals = assembly.lead?.block_ordinals ?? []
    // The fixture earns a lead; the thread must be that block's BLUF.
    expect(leadOrdinals.length).toBeGreaterThan(0)
    const leadBlock = assembly.blocks.find((b) => b.ordinal === leadOrdinals[0])!
    const expected = leadBlock.spans.find((s) => s.role === 'bluf')!.text.trim()
    expect(leadThread(WORLD, assembly)).toBe(expected)
  })

  it('falls back to the body for the assessment channel, skipping the as-of slice', () => {
    const thread = leadThread(ASSESSMENT, null)
    expect(thread).not.toMatch(/^\*As of/)
    expect(thread.length).toBeGreaterThan(20)
  })

  it('never fabricates a thread when the row carries no prose', () => {
    expect(leadThread({ ...WORLD, body: null, data: null }, null)).toBe('')
  })

  it('groups mixed tiers in reading order and drops empty ones', () => {
    const rows = [COUNTRY, WORLD, THEMATIC]
      .map((r) => toReportRow(r, projectAssembly(r))!)
      .filter(Boolean)
    const groups = groupByTier(rows)
    expect(groups.map((g) => g.tier)).toEqual(['world', 'country', 'thematic'])
    // 'assessment' and 'region' had no rows and are absent, not empty.
    expect(groups.find((g) => g.tier === 'assessment')).toBeUndefined()
  })
})

describe('date navigation', () => {
  it('reads a UTC day from both stamp dialects', () => {
    expect(dayKey('2026-09-03T12:00:00Z')).toBe('2026-09-03')
    expect(dayKey('2026-09-03 12:00:38.413004+00')).toBe('2026-09-03')
    expect(dayKey(null)).toBe('')
  })

  it('lists days newest first and filters to one', () => {
    const base = toReportRow(WORLD, projectAssembly(WORLD))!
    const rows = [
      base,
      { ...base, id: 'b', producedAt: '2026-09-02T12:00:00Z', day: '2026-09-02' },
    ]
    expect(daysPresent(rows)).toEqual(['2026-09-03', '2026-09-02'])
    expect(rowsForDay(rows, '2026-09-02').map((r) => r.id)).toEqual(['b'])
    expect(rowsForDay(rows, null)).toHaveLength(2)
  })
})

describe('the sections a report governs', () => {
  const assembly = projectAssembly(WORLD)!

  it('derives the cited heads from the blocks, needing no second request', () => {
    const findings = reportFindings(assembly)
    expect(findings).toHaveLength(assembly.blocks.length)
    expect(findings[0].findingId).toBe(assembly.blocks[0].finding_id)
    expect(findings[0].bluf.length).toBeGreaterThan(10)
  })

  it('dedupes cited signals across the blocks that share them', () => {
    const signals = reportSignals(assembly)
    const ids = signals.map((s) => s.signalId).filter(Boolean)
    expect(new Set(ids).size).toBe(ids.length)
    expect(signals.length).toBeGreaterThan(0)
    // A signal cited by more than one block records both ordinals.
    const shared = signals.find((s) => s.ordinals.length > 1)
    if (shared) expect(shared.ordinals.length).toBeGreaterThan(1)
  })

  it('names the targets the record speaks about', () => {
    const entities = reportEntities(assembly)
    expect(entities.length).toBeGreaterThan(0)
    expect(entities[0].name).toBeTruthy()
    expect(entities[0].ordinals.length).toBeGreaterThan(0)
  })
})

describe('one selection, everything follows', () => {
  const assembly = projectAssembly(WORLD)!
  const findings = reportFindings(assembly)
  const signals = reportSignals(assembly)
  const entities = reportEntities(assembly)

  it('narrows findings and signals to the selected entity', () => {
    const target = entities[0]
    const out = filterToEntity(target.targetId, findings, signals, entities)
    expect(out.findings.length).toBeLessThanOrEqual(findings.length)
    expect(out.findings.every((f) => target.ordinals.includes(f.ordinal))).toBe(true)
    expect(out.signals.every((s) => s.ordinals.some((o) => target.ordinals.includes(o)))).toBe(
      true,
    )
  })

  it('restores the full report when the selection is cleared', () => {
    const out = filterToEntity(null, findings, signals, entities)
    expect(out.findings).toHaveLength(findings.length)
    expect(out.signals).toHaveLength(signals.length)
  })

  it('leaves the report intact for an entity it does not contain', () => {
    const out = filterToEntity('country_nowhere', findings, signals, entities)
    expect(out.findings).toHaveLength(findings.length)
  })
})
