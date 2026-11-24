/**
 * reportSections (W4) — the report screen's counts and citations for an
 * Assessment row, against fixtures sanitised from the REAL payloads of
 * world_assessment 64f74201 and its spine world_assessor e30e67d3 (read text
 * replaced with placeholders; ids, structure, markers, ordinals, targets,
 * verification blocks as the API returned them).
 */
import { describe, expect, it } from 'vitest'
import assessmentFixture from './__fixtures__/world_assessment.64f74201.json'
import spineFixture from './__fixtures__/world_spine.e30e67d3.json'
import { projectArms, projectAssessment, type ReadFindingRow } from '@/lib/assemblyModel'
import { filterToEntity } from './mobileModel'
import { reportSections } from './reportSections'

const ASSESSMENT = assessmentFixture as unknown as ReadFindingRow
const SPINE = spineFixture as unknown as ReadFindingRow

describe('the fixtures are the real shape', () => {
  it('the assessment names its spine and carries 16 ordinal-stamped citations', () => {
    expect(projectAssessment(ASSESSMENT)!.spine_id).toBe(SPINE.id)
    const cites = (ASSESSMENT.data as { data: { citations: { ordinal: number }[] } }).data.citations
    expect(cites.map((c) => c.ordinal)).toEqual(Array.from({ length: 16 }, (_, i) => i + 1))
  })
})

describe('an Assessment row, citations only (no spine loaded)', () => {
  const s = reportSections(ASSESSMENT)

  it('counts the heads, targets and places it quotes instead of 0', () => {
    expect(s.source).toBe('assessment_citations')
    expect(s.findings).toHaveLength(16)
    // 16 citations over 15 distinct targets (blocks 1 and 2 are both Russia).
    expect(s.entities).toHaveLength(15)
    expect(s.where).toHaveLength(15)
    expect(s.entities[0].ordinals).toHaveLength(2)
  })

  it('reports signals as UNAVAILABLE, never as zero', () => {
    expect(s.signals).toBeNull()
  })

  it('resolves every [[ref:N]] marker in the body', () => {
    expect(s.citations.map((c) => c.marker)).toEqual(
      Array.from({ length: 16 }, (_, i) => `[[ref:${i + 1}]]`),
    )
    expect(s.citations.every((c) => c.refId === SPINE.id)).toBe(true)
  })

  it('names targets through the supplied namer, falling back to the id', () => {
    const named = reportSections(ASSESSMENT, null, (id) => (id === 'country_g20_ru' ? 'Russia' : null))
    expect(named.entities.find((e) => e.targetId === 'country_g20_ru')!.name).toBe('Russia')
    expect(named.entities.find((e) => e.targetId === 'country_watch_ir')!.name).toBe('country_watch_ir')
  })

  it('a tapped target still narrows the findings', () => {
    const { findings } = filterToEntity('country_g20_ru', s.findings, [], s.entities)
    expect(findings.map((f) => f.ordinal)).toEqual([1, 2])
  })
})

describe('an Assessment row with its spine loaded', () => {
  const s = reportSections(ASSESSMENT, SPINE)

  it('reads the spine blocks the assessment cites, signals included', () => {
    expect(s.source).toBe('assessment_spine')
    expect(s.findings).toHaveLength(16)
    expect(s.entities).toHaveLength(15)
    expect(s.signals).not.toBeNull()
    expect(s.signals!.length).toBeGreaterThan(0)
    expect(s.citations).toHaveLength(16)
  })

  it('restricts to the cited blocks when the assessment cites a subset', () => {
    const subset = JSON.parse(JSON.stringify(ASSESSMENT)) as {
      data: { data: { citations: { ordinal: number }[] } }
    }
    subset.data.data.citations = subset.data.data.citations.filter((c) => c.ordinal <= 3)
    const part = reportSections(subset as unknown as ReadFindingRow, SPINE)
    expect(part.findings.map((f) => f.ordinal)).toEqual([1, 2, 3])
  })

  it('ignores a spine row the assessment does not name', () => {
    const other = { ...SPINE, id: 'some-other-spine' } as ReadFindingRow
    expect(reportSections(ASSESSMENT, other).source).toBe('assessment_citations')
  })
})

describe('an assembly row (the world spine itself)', () => {
  it('is unchanged: its own blocks and signals, per-block citations', () => {
    const s = reportSections(SPINE)
    expect(s.source).toBe('assembly')
    expect(s.assembly!.blocks).toHaveLength(16)
    expect(s.findings).toHaveLength(16)
    expect(s.signals!.length).toBeGreaterThan(0)
    expect(s.citations).toEqual([])
  })
})

describe('a legacy prose row', () => {
  it('has no sections and says so with empty lists', () => {
    const s = reportSections({
      id: 'legacy',
      produced_at: '2026-09-01T12:00:00Z',
      body: 'prose',
      data: { data: { meta: true, citations: [] } },
    })
    expect(s.source).toBe('none')
    expect(s.findings).toEqual([])
  })
})

describe('projectArms reads what the server writes (branch_scores.assembly)', () => {
  it('the spine: headline score 1.0 and quote fidelity 16/16, gate passed', () => {
    const arms = projectArms(SPINE)!
    expect(arms.score).toBe(1)
    expect(arms.quote_fidelity).toBe(1)
    expect(arms.spans).toBe(16)
    expect(arms.gate).toBe('passed')
    expect(arms.failed_ordinals).toEqual([])
  })

  it('the Assessment is judged, not armed: no arms, never a fabricated number', () => {
    expect(projectArms(ASSESSMENT)).toBeNull()
  })

  it('a headline under 1.0 fails the gate, and names no block it cannot name', () => {
    const v = JSON.parse(JSON.stringify(SPINE.verification)) as {
      branch_scores: { assembly: { score: number } }
    }
    v.branch_scores.assembly.score = 0.85
    const arms = projectArms({ ...SPINE, verification: v as unknown as Record<string, unknown> })!
    expect(arms.gate).toBe('failed')
    expect(arms.score).toBe(0.85)
    expect(arms.failed_ordinals).toEqual([])
  })

  it('no span checked: quote fidelity is absent, not 0', () => {
    const v = JSON.parse(JSON.stringify(SPINE.verification)) as {
      counters: Record<string, number>
    }
    v.counters.assembly_quote_spans_checked = 0
    expect(
      projectArms({ ...SPINE, verification: v as unknown as Record<string, unknown> })!.quote_fidelity,
    ).toBeNull()
  })

  it('still reads the legacy assembly_arms shape the fixtures model', () => {
    const arms = projectArms({
      id: 'x',
      produced_at: '2026-09-01T00:00:00Z',
      verification: { assembly_arms: { gate: 'passed', quote_fidelity: 1, failed_ordinals: [] } },
    })!
    expect(arms.quote_fidelity).toBe(1)
    expect(arms.score).toBeNull()
  })
})
