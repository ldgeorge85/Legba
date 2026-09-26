/**
 * Unit tests for the desk brief page's derivation (P-A).
 *
 * The three contracts under test are the three the module exists to keep:
 * the roster is the denominator (a unit the composition never carried is a
 * ROW, not an omission), an unmeasured figure prints as unmeasured rather
 * than as a zero, and "what this page does not publish" is assembled from
 * fields rather than written.
 */
import { describe, it, expect } from 'vitest'
import {
  ABSENCES_NOT_ASKED,
  CITATION_UNDATED,
  EVIDENCE_STATE_LABEL,
  LIMIT_UNKNOWN,
  cadenceState,
  citationDateLine,
  deriveEvidenceTable,
  evidenceStateDetail,
  exportCitations,
  findItem,
  judgeText,
  notPublished,
  parseExportDocument,
  readingLimits,
  shareText,
  type ExportAbsences,
  type ExportCitation,
  type ExportDocument,
  type ExportItem,
} from './deskBriefPage'
import { GAP_STRIP_UNITS } from './gapStripModel'
import type { CoverageRow } from './assemblyModel'
import type { DimensionBand } from './evalOps'
import type { UnitCorrectness } from './unitCorrectnessModel'

const NOW = new Date('2026-09-26T12:00:00Z')
const FRESH = '2026-09-26T06:00:00Z'
const OLD = '2026-09-20T06:00:00Z'

function citation(over: Partial<ExportCitation> = {}): ExportCitation {
  return {
    marker: '[1]',
    citation_kind: 'signal',
    resolves_against: 'signals',
    marker_class: null,
    signal_id: 'sig-1',
    ref_id: null,
    title: 'A cited article',
    canonical_url: 'https://www.example.org/a',
    resolved: true,
    resolution_source: 'live',
    ...over,
  }
}

function item(over: Partial<ExportItem> = {}): ExportItem {
  return {
    kind: 'finding',
    id: 'f-1',
    row_kind: 'finding',
    title: 'Escalation read',
    analyst_id: 'escalation',
    target_id: 'country_watch_tw',
    severity: 'medium',
    produced_at: FRESH,
    superseded: false,
    body: 'Something happened [1].',
    citations: [citation()],
    verify_state: 'faithfulness=0.91',
    verify_flags: {},
    ...over,
  }
}

function absences(over: Partial<ExportAbsences> = {}): ExportAbsences {
  return {
    scope: 'country_watch_tw',
    read_at: '2026-09-26T11:00:00Z',
    item_count: 1,
    note: 'glossary note',
    by_kind: [
      {
        kind: 'not_collected',
        meaning: 'nothing covers this subject',
        state: 'absent',
        not_measured: null,
        count: 1,
        held_back: 0,
        items: [
          {
            subject: 'energy_security',
            reason: 'no source covers this unit for this desk',
            since: null,
            window: null,
            as_of: '2026-09-26T04:00:00Z',
            as_of_basis: 'the last scan',
            expires_at: null,
            stale: false,
            proof: {
              what_was_checked: 'the desk source map',
              checked_at: '2026-09-26T04:00:00Z',
              ref: null,
              ref_kind: null,
            },
          },
        ],
      },
      {
        kind: 'source_stale',
        meaning: 'a covering source stopped',
        state: 'not_measured',
        not_measured: 'the source-quality read failed for this desk',
        count: 0,
        held_back: 0,
        items: [],
      },
    ],
    ...over,
  }
}

function doc(over: Partial<ExportDocument> = {}): ExportDocument {
  return {
    title: 'Desk brief — country_watch_tw',
    generated_at: '2026-09-26T11:59:00Z',
    item_count: 1,
    missing_count: 0,
    provenance_note: 'composed from the substrate',
    items: [item()],
    appendix: null,
    absences: absences(),
    ...over,
  }
}

describe('parseExportDocument', () => {
  it('returns the document for well-formed JSON', () => {
    expect(parseExportDocument(JSON.stringify(doc()))?.item_count).toBe(1)
  })

  it('returns null — never throws — for a body that is not a document', () => {
    expect(parseExportDocument('not json')).toBeNull()
    expect(parseExportDocument('[]')).toBeNull()
    expect(parseExportDocument('{"detail":"Bad Gateway"}')).toBeNull()
    expect(parseExportDocument(null)).toBeNull()
  })

  it('findItem is null for an id the document does not carry', () => {
    expect(findItem(doc(), 'nope')).toBeNull()
    expect(findItem(null, 'f-1')).toBeNull()
  })
})

describe('exportCitations', () => {
  it('renames the route-side keys onto the one citation model', () => {
    const [c] = exportCitations(item())
    expect(c.marker).toBe('[1]')
    expect(c.refKind).toBe('signal')
    expect(c.refId).toBe('sig-1')
    expect(c.source).toBe('https://www.example.org/a')
    expect(c.title).toBe('A cited article')
  })

  it('carries a stated citation date and INVENTS none when the route had none', () => {
    const dated = exportCitations(
      item({ citations: [citation({ citation_date: '2026-09-04', citation_date_label: 'published' })] }),
    )
    expect(dated[0].producedAt).toBe('2026-09-04')
    const undated = exportCitations(item())
    expect(undated[0].producedAt).toBeUndefined()
  })

  it('keeps a grounding block as grounding, with its observation camel-cased', () => {
    const [c] = exportCitations(
      item({
        citations: [
          citation({
            marker: '[4]',
            citation_kind: 'observation',
            marker_class: 'desk_grounding',
            signal_id: null,
            ref_id: 'obs-9',
            observation: {
              valid_from: '2019-01-01',
              valid_to: '2019-12-31',
              record_time: '2026-02-01',
              stale_tense: '(historical: valid 2019, recorded 2026-02)',
            },
          }),
        ],
      }),
    )
    expect(c.refKind).toBe('observation')
    expect(c.observation?.validFrom).toBe('2019-01-01')
    expect(c.observation?.staleTense).toBe('(historical: valid 2019, recorded 2026-02)')
  })

  it('is empty for an item with no citations', () => {
    expect(exportCitations(item({ citations: [] }))).toEqual([])
    expect(exportCitations(null)).toEqual([])
  })
})

describe('citationDateLine', () => {
  it('keeps the route’s own label with the date', () => {
    expect(citationDateLine(citation({ citation_date: '2026-09-04', citation_date_label: 'published' }))).toBe(
      'published 2026-09-04',
    )
    expect(citationDateLine(citation({ citation_date: '2026-09-04', citation_date_label: 'fetched' }))).toBe(
      'fetched 2026-09-04',
    )
  })

  it('states the absence in words for an undated citation', () => {
    expect(citationDateLine(citation())).toBe(CITATION_UNDATED)
    expect(citationDateLine(citation({ citation_date: '  ' }))).toBe(CITATION_UNDATED)
  })

  it('prints a bare date when the route stated no label', () => {
    expect(citationDateLine(citation({ citation_date: '2026-09-04' }))).toBe('2026-09-04')
  })
})

describe('deriveEvidenceTable', () => {
  const coverage: CoverageRow[] = [
    { unit: 'escalation', unit_name: 'Escalation', status: 'in_basis', read_date: FRESH, age_h: 6 },
    {
      unit: 'energy_security',
      unit_name: 'Energy security',
      status: 'below_floor',
      read_date: OLD,
      age_h: 150,
    },
  ]
  const questions = new Map([['escalation', 'What changed on the escalation ladder?']])
  const latest = new Map([
    ['escalation', { id: 'f-1', producedAt: FRESH }],
    ['energy_security', { id: 'f-2', producedAt: OLD }],
    ['military_posture', { id: 'f-3', producedAt: OLD }],
  ])
  const dimensions: Record<string, DimensionBand> = {
    escalation: {
      band: 'elevated',
      basis: ['f-1'],
      severity_tag: 'medium',
      effective_confidence: 0.8,
      confidence: 0.9,
      critic_score: 0.78,
      damped: false,
      reason: 'qualified',
      produced_at: FRESH,
    } as unknown as DimensionBand,
    internal_stability: {
      band: 'insufficient-evidence',
      basis: [],
      severity_tag: null,
      effective_confidence: null,
      confidence: null,
      critic_score: null,
      damped: false,
      reason: 'low-faithfulness',
      produced_at: null,
    } as unknown as DimensionBand,
  }
  const correctness = new Map<string, UnitCorrectness>([
    ['escalation', { analyst_id: 'escalation', coverage_share: 0.244, badge: 'correctness 90.9%' } as UnitCorrectness],
  ])

  function table() {
    return deriveEvidenceTable({
      coverage,
      questions,
      latestByUnit: latest,
      dimensions,
      correctness,
      absences: absences(),
      now: NOW,
    })
  }

  it('is one row per unit on the ROSTER, not per unit the composition carried', () => {
    const rows = table()
    expect(rows).toHaveLength(GAP_STRIP_UNITS.length)
    expect(rows.map((r) => r.unitId)).toEqual(GAP_STRIP_UNITS.map((u) => u.id))
  })

  it('lets the composition’s coverage register decide when it refused the unit', () => {
    const row = table().find((r) => r.unitId === 'energy_security')!
    expect(row.state).toBe('below_floor')
    expect(row.stateSource).toBe('coverage register')
    expect(row.coverageStatus).toBe('below_floor')
  })

  it('falls to the scorecard’s insufficient verdict, then to the cadence check', () => {
    const rows = table()
    const insufficient = rows.find((r) => r.unitId === 'internal_stability')!
    expect(insufficient.state).toBe('insufficient')
    expect(insufficient.stateSource).toBe('scorecard')
    expect(evidenceStateDetail(insufficient, dimensions)).toBe('excluded: low faithfulness')

    const stale = rows.find((r) => r.unitId === 'military_posture')!
    expect(stale.state).toBe('stale')
    expect(stale.stateSource).toBe('cadence')

    const none = rows.find((r) => r.unitId === 'proliferation_watch')!
    expect(none.state).toBe('no_read')
    expect(none.readAt).toBeNull()
    expect(none.findingId).toBeNull()
  })

  it('never fabricates a figure: an ungraded unit carries nulls, not zeros', () => {
    const rows = table()
    const graded = rows.find((r) => r.unitId === 'escalation')!
    expect(graded.state).toBe('in_basis')
    expect(graded.judgeScore).toBeCloseTo(0.78)
    expect(graded.correctnessCoverage).toBeCloseTo(0.244)
    expect(graded.correctnessBadge).toBe('correctness 90.9%')
    expect(graded.question).toBe('What changed on the escalation ladder?')

    const ungraded = rows.find((r) => r.unitId === 'proliferation_watch')!
    expect(ungraded.judgeScore).toBeNull()
    expect(ungraded.correctnessCoverage).toBeNull()
    expect(ungraded.correctnessBadge).toBeNull()
    expect(ungraded.question).toBeNull()
    expect(judgeText(ungraded.judgeScore)).toBe('unmeasured')
    expect(shareText(ungraded.correctnessCoverage)).toBe('unmeasured')
  })

  it('attaches the typed absences whose subject IS the unit, and only those', () => {
    const rows = table()
    const energy = rows.find((r) => r.unitId === 'energy_security')!
    expect(energy.absences).toHaveLength(1)
    expect(energy.absences[0].kind).toBe('not_collected')
    expect(energy.absences[0].label).toBe('not collected')
    expect(rows.find((r) => r.unitId === 'escalation')!.absences).toEqual([])
  })

  it('every state has a printable label and a cadence equivalent', () => {
    for (const row of table()) {
      expect(EVIDENCE_STATE_LABEL[row.state]).toBeTruthy()
      expect(cadenceState(row)).toBeTruthy()
    }
  })

  it('an empty desk is a full table of no-read rows, never an empty table', () => {
    const rows = deriveEvidenceTable({
      coverage: [],
      questions: new Map(),
      latestByUnit: new Map(),
      dimensions: null,
      correctness: new Map(),
      absences: null,
      now: NOW,
    })
    expect(rows).toHaveLength(GAP_STRIP_UNITS.length)
    expect(rows.every((r) => r.state === 'no_read')).toBe(true)
    expect(rows.every((r) => r.absences.length === 0)).toBe(true)
  })
})

describe('notPublished', () => {
  it('names each kind the route could not read, in the route’s own words', () => {
    const lines = notPublished({ doc: doc(), missingUnits: [] })
    const stale = lines.find((l) => l.subject.includes('source stale'))
    expect(stale?.why).toBe('the source-quality read failed for this desk')
    expect(stale?.source).toBe('typed absence')
  })

  it('names the held-back items a per-kind cap kept out of the document', () => {
    const withCap = doc({
      absences: absences({
        by_kind: [
          {
            kind: 'collected_but_silent',
            meaning: 'm',
            state: 'absent',
            not_measured: null,
            count: 23,
            held_back: 18,
            items: [],
          },
        ],
      }),
    })
    const lines = notPublished({ doc: withCap, missingUnits: [] })
    expect(lines.some((l) => l.why.includes('18 of 23'))).toBe(true)
  })

  it('names a unit the coverage register declared and no read filled', () => {
    const lines = notPublished({
      doc: doc(),
      missingUnits: [{ unit: 'military_posture', unitName: 'Military posture', status: 'no_head_in_horizon' }],
    })
    const line = lines.find((l) => l.subject === 'unit · Military posture')
    expect(line?.source).toBe('coverage register')
    expect(line?.why).toContain('no_head_in_horizon')
  })

  it('names an id the export could not resolve, from the export’s own placeholder', () => {
    const lines = notPublished({
      doc: doc({ items: [item(), { kind: 'finding', id: 'gone', error: 'not found in substrate' }] }),
      missingUnits: [],
    })
    expect(lines.some((l) => l.subject === 'finding · gone')).toBe(true)
  })

  it('says so when the document carries no absence block at all', () => {
    const lines = notPublished({ doc: doc({ absences: undefined }), missingUnits: [] })
    expect(lines[0].why).toBe(ABSENCES_NOT_ASKED)
  })

  it('names the grader’s reference when it is not current, and the ungraded units', () => {
    const none = notPublished({ doc: doc(), missingUnits: [], referenceState: 'none' })
    expect(none.some((l) => l.subject.startsWith('correctness against'))).toBe(true)

    const current = notPublished({
      doc: doc(),
      missingUnits: [],
      referenceState: 'current',
      ungradedUnits: ['energy_security'],
    })
    expect(current.some((l) => l.subject === 'correctness · energy_security')).toBe(true)
    expect(current.some((l) => l.subject.startsWith('correctness against'))).toBe(false)
  })

  it('is empty — not a fabricated reassurance — with no document', () => {
    expect(notPublished({ doc: null, missingUnits: [] })).toEqual([])
  })
})

describe('readingLimits', () => {
  it('carries every instant the records state', () => {
    const limits = readingLimits({
      doc: doc(),
      compositionAsOf: '2026-09-26T11:42:00Z',
      scaleLine: 'intensity/2026-08 · situation_clustering/2026-09.1',
      scorecardAt: '2026-09-26T04:40:00Z',
      reference: {
        state: 'current',
        window_start: '2026-09-12T13:43:00Z',
        window_end: '2026-09-26T13:43:00Z',
        age_days: 0.11,
      },
    })
    const by = new Map(limits.map((l) => [l.label, l.value]))
    expect(by.get('scale era')).toBe('intensity/2026-08 · situation_clustering/2026-09.1')
    expect(by.get('composition as of')).toBe('2026-09-26T11:42:00Z')
    expect(by.get('document composed at')).toBe('2026-09-26T11:59:00Z')
    expect(by.get('typed absence read at')).toBe('2026-09-26T11:00:00Z')
    expect(by.get('judge scorecard produced at')).toBe('2026-09-26T04:40:00Z')
    expect(by.get('grader reference')).toContain('current · window 2026-09-12 → 2026-09-26')
  })

  it('states an unrecorded limit as unrecorded, never as a default instant', () => {
    const limits = readingLimits({ doc: null })
    expect(limits.every((l) => l.value === LIMIT_UNKNOWN)).toBe(true)
  })
})
