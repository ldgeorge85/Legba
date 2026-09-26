/**
 * The scope projectors, against the SAME fixtures the reader is tested with.
 *
 * The design's load-bearing claim is that a report's world costs no backend
 * work — every member id is already on the row the Navigator fetched. These
 * tests are that claim, checked: nothing here mocks a fetch, because nothing
 * here performs one.
 */
import { describe, expect, it } from 'vitest'

import worldRow from '@/v4/read/__fixtures__/assembly.world.json'
import countryRow from '@/v4/read/__fixtures__/assembly.country.json'
import assessmentRow from '@/v4/read/__fixtures__/assessment.world.json'
import { projectAssembly, type ReadFindingRow } from './assemblyModel'
import type { LineageReport } from './graphModel'
import {
  MIN_WINDOW_HOURS,
  isReportRow,
  membersFromAssembly,
  reportLabel,
  scopeFromAssembly,
  scopeFromJournalEntry,
  scopeFromLineage,
  scopeFromRow,
  windowHoursOf,
} from './scopeFromReport'

const world = worldRow as unknown as ReadFindingRow
const country = countryRow as unknown as ReadFindingRow
const assessment = assessmentRow as unknown as ReadFindingRow

describe('isReportRow — a report IS a finding row', () => {
  it('recognises the composition producers, both voices included', () => {
    expect(isReportRow(world)).toBe(true)
    expect(isReportRow(country)).toBe(true)
    expect(isReportRow(assessment)).toBe(true)
    expect(isReportRow({ analyst_id: 'region_composition' })).toBe(true)
    // The country-grain voice is a report in exactly the sense the world one is.
    expect(isReportRow({ analyst_id: 'country_assessment' })).toBe(true)
    expect(isReportRow({ analyst_id: 'chronicle_assessor' })).toBe(true)
  })

  it('does not mistake an ordinary desk finding for a report', () => {
    expect(isReportRow({ analyst_id: 'country_assessor' })).toBe(false)
    expect(isReportRow({})).toBe(false)
  })
})

describe('membersFromAssembly — every id is already on the row', () => {
  const assembly = projectAssembly(world)!

  it('projects one member per block head, deduped and in order', () => {
    const m = membersFromAssembly(assembly)
    expect(m.findingIds.length).toBe(assembly.blocks.length)
    expect(m.findingIds[0]).toBe(assembly.blocks[0].finding_id)
    expect(new Set(m.findingIds).size).toBe(m.findingIds.length)
  })

  it('carries the desks the read is composed from', () => {
    const m = membersFromAssembly(assembly)
    expect(m.targetIds).toContain('country_g20_sa')
    expect(m.targetIds.length).toBeGreaterThan(1)
  })

  it('carries the cited signals and their sources, dropping the unresolved ones', () => {
    const m = membersFromAssembly(assembly)
    // The world fixture's first block has markers with no `signal_id` — those
    // are absences on the record, and an absence must not become a member.
    expect(m.signalIds).not.toContain(null)
    expect(m.signalIds).toContain('7e98612b-b6db-436b-ac07-dfe5d014d98c')
    expect(m.sourceIds).toContain('source.gcaptain.all')
  })

  it('seeds the graph with the desk SUBJECTS, not an invented entity list', () => {
    // The assembly carries no NER entity list. Deriving one would be exactly
    // the derivation this reader refuses, so the honest seed set is the set of
    // subjects the record is about.
    const m = membersFromAssembly(assembly)
    expect(m.entityNames).toContain('G20 — Saudi Arabia')
  })

  it('takes as_of off the payload, not off the row', () => {
    expect(membersFromAssembly(assembly).asOf).toBe('2026-09-03T12:00:00Z')
  })
})

describe('windowHoursOf — derived, never invented', () => {
  it('reaches back to the oldest piece of evidence the record quotes', () => {
    const h = windowHoursOf(projectAssembly(world)!)
    expect(h).not.toBeNull()
    expect(h!).toBeGreaterThanOrEqual(MIN_WINDOW_HOURS)
  })

  it('is null when nothing on the record carries an age', () => {
    const bare = { ...projectAssembly(world)!, blocks: [], coverage: [] }
    expect(windowHoursOf(bare)).toBeNull()
  })

  it('floors at MIN_WINDOW_HOURS so a rounding artefact is not a window', () => {
    const a = projectAssembly(world)!
    const tiny = {
      ...a,
      coverage: [],
      blocks: [{ ...a.blocks[0], evidence_age_h: 0.5, signals: [] }],
    }
    expect(windowHoursOf(tiny)).toBe(MIN_WINDOW_HOURS)
  })
})

describe('scopeFromAssembly / scopeFromRow', () => {
  it('scopes a world read to its whole world', () => {
    const s = scopeFromAssembly(world, projectAssembly(world)!)
    expect(s.kind).toBe('report')
    expect(s.id).toBe(world.id)
    expect(s.origin).toBe('navigator')
    expect(s.members.analystIds).toEqual(['world_assessor'])
    expect(s.members.targetIds.length).toBeGreaterThan(1)
  })

  it('scopes a country composition to its single desk — the case the feed can push server-side', () => {
    const s = scopeFromRow(country)
    expect(s.members.targetIds.length).toBeGreaterThanOrEqual(1)
  })

  it('scopeFromRow degrades to the row itself when there is no assembly block', () => {
    // A degraded scope still filters and still flies the map. Returning NONE
    // would make the click read as a broken link, which is the defect the whole
    // train exists to remove.
    const legacy: ReadFindingRow = {
      id: 'legacy-1',
      analyst_id: 'world_assessor',
      target_id: 'world',
      title: 'A run that predates the assembly',
      produced_at: '2026-08-01T06:00:00Z',
      data: { body: 'prose' },
    }
    const s = scopeFromRow(legacy)
    expect(s.kind).toBe('report')
    expect(s.members.findingIds).toEqual(['legacy-1'])
    expect(s.members.targetIds).toEqual(['world'])
    expect(s.members.asOf).toBe('2026-08-01T06:00:00Z')
  })

  it('labels a report for a chip, never as a raw UUID', () => {
    const label = reportLabel(world, '2026-09-03T12:00:00Z')
    expect(label).toContain('2026-09-03')
    expect(label).not.toBe(world.id)
  })

  it('falls back to producer · day when a row has no title', () => {
    expect(reportLabel({ id: 'x', analyst_id: 'region_composition', produced_at: '2026-09-05T00:00:00Z' })).toBe(
      'region_composition · 2026-09-05',
    )
  })
})

describe('scopeFromLineage — the payload with no assembly block', () => {
  const row: ReadFindingRow = {
    id: 'rollup-1',
    analyst_id: 'region_composition',
    target_id: 'region_mena',
    title: 'MENA rollup',
    produced_at: '2026-09-05T06:00:00Z',
  }
  const report = {
    root: { id: 'rollup-1', row_kind: 'finding', target_id: 'region_mena' },
    nodes: [
      { id: 'f1', row_kind: 'finding', target_id: 'country_watch_ir' },
      { id: 'f2', row_kind: 'meta_finding', target_id: 'country_watch_ir' },
      { id: 's1', row_kind: 'signal', target_id: null },
    ],
    edges: [],
  } as unknown as LineageReport

  it('separates findings from signals off the walk, without a second request', () => {
    const s = scopeFromLineage(row, report)
    expect(s.members.findingIds).toEqual(['f1', 'f2'])
    expect(s.members.signalIds).toEqual(['s1'])
    expect(s.members.targetIds).toEqual(['region_mena', 'country_watch_ir'])
  })

  it('falls back to the row itself when the walk is empty', () => {
    expect(scopeFromLineage(row, null).members.findingIds).toEqual(['rollup-1'])
  })
})

describe('scopeFromJournalEntry', () => {
  it('scopes to the entry, labelled by its title', () => {
    const s = scopeFromJournalEntry('j1', 'Week of 1 Sep')
    expect(s.kind).toBe('journal_entry')
    expect(s.id).toBe('j1')
    expect(s.label).toBe('Week of 1 Sep')
  })
})
