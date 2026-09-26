/**
 * Unit tests for the per-unit CORRECTNESS data layer (G2).
 *
 * Locks the three things that, if they drifted, would put a wrong number
 * beside a read:
 *
 *   * `findUnitCorrectness` matches a unit EXACTLY, and answers `null` for a
 *     unit with no row — so the badge renders nothing rather than borrowing
 *     the neighbouring desk's score.
 *   * `orderLedger` puts the claims the reference CONTRADICTED first. A
 *     reader opens the drawer to find what was wrong; burying it under thirty
 *     `silent` rows is the same as hiding it.
 *   * the model never composes a number itself — it carries the server's
 *     `badge` string through untouched.
 */
import { describe, it, expect } from 'vitest'
import {
  REFERENCE_STATE_LABEL,
  decisiveSpans,
  familyLabels,
  findUnitCorrectness,
  orderLedger,
  type UnitCorrectness,
  type UnitCorrectnessClaim,
  type UnitCorrectnessPage,
} from './unitCorrectnessModel'

const REF = {
  state: 'current' as const,
  id: 'ref-1',
  sha256: '2dc73bf3',
  builder: 'opus-web-lane',
  window_start: '2026-09-02T00:00:00Z',
  window_end: '2026-09-16T19:30:00Z',
  span_verified_rate: 0.95,
  thin_dimensions: ['proliferation_watch'],
  age_days: 0,
  age_source: 'derived' as const,
}

function unit(
  analyst_id: string,
  over: Partial<UnitCorrectness> = {},
): UnitCorrectness {
  return {
    id: `uc-${analyst_id}`,
    analyst_id,
    target_id: 'country_watch_il',
    head_id: 'head-1',
    as_of: '2026-09-16T19:30:00Z',
    rubric_sha: '0a011222',
    grain: 'desk',
    n_claims: 45,
    n_contains: 10,
    n_contradicts: 1,
    n_silent: 31,
    n_split: 2,
    n_unparseable: 1,
    n_single_family: 0,
    n_decided: 11,
    correctness_share: 10 / 11,
    coverage_share: 11 / 45,
    single_family: false,
    badge: 'correctness 90.9% (10/11) · coverage 24.4% (n=45) · as of 2026-09-16',
    families: {},
    cost_usd: 0,
    created_at: '2026-09-16T19:35:00Z',
    reference: REF,
    ...over,
  }
}

const PAGE: UnitCorrectnessPage = {
  target_id: 'country_watch_il',
  reference: REF,
  data: [unit('internal_stability'), unit('military_posture', {
    n_contains: 0,
    n_contradicts: 0,
    n_decided: 0,
    n_claims: 7,
    n_silent: 7,
    n_split: 0,
    n_unparseable: 0,
    correctness_share: null,
    coverage_share: 0,
    badge: 'correctness unmeasured (0 decided) · coverage 0.0% (n=7) · as of 2026-09-16',
  })],
  next_cursor: null,
}

function claim(
  claim_id: string,
  adjudicated: string,
  over: Partial<UnitCorrectnessClaim> = {},
): UnitCorrectnessClaim {
  return {
    id: `c-${claim_id}`,
    claim_id,
    grain: 'desk',
    claim_text: 'a claim',
    label_by_family: { F0: adjudicated, F2: adjudicated, F3: 'silent' },
    adjudicated,
    n_families: 3,
    single_family: false,
    spans: {},
    created_at: '2026-09-16T19:35:00Z',
    ...over,
  }
}

describe('findUnitCorrectness', () => {
  it('matches a unit by its exact analyst id', () => {
    const row = findUnitCorrectness(PAGE, 'internal_stability')
    expect(row?.correctness_share).toBeCloseTo(10 / 11)
    expect(row?.badge).toBe(PAGE.data[0].badge)
  })

  it('answers null for a unit with no row — never a neighbour’s number', () => {
    expect(findUnitCorrectness(PAGE, 'escalation')).toBeNull()
    expect(findUnitCorrectness(PAGE, 'internal_stabil')).toBeNull()
    expect(findUnitCorrectness(PAGE, null)).toBeNull()
    expect(findUnitCorrectness(null, 'internal_stability')).toBeNull()
  })

  it('carries a NULL correctness share through as null, not zero', () => {
    const row = findUnitCorrectness(PAGE, 'military_posture')
    expect(row?.correctness_share).toBeNull()
    expect(row?.coverage_share).toBe(0)
    expect(row?.badge).toContain('unmeasured')
  })
})

describe('orderLedger', () => {
  it('leads with the claims the reference contradicted', () => {
    const ordered = orderLedger([
      claim('a', 'silent'),
      claim('b', 'contains'),
      claim('c', 'contradicts'),
      claim('d', 'split'),
      claim('e', 'contains'),
    ])
    expect(ordered.map((c) => c.claim_id)).toEqual(['c', 'b', 'e', 'd', 'a'])
  })

  it('is stable within a group and safe on absent claims', () => {
    expect(orderLedger(null)).toEqual([])
    expect(orderLedger([])).toEqual([])
  })
})

describe('the per-claim detail the drawer renders', () => {
  it('flattens each family’s decisive span, skipping the ones with none', () => {
    const c = claim('x', 'contradicts', {
      spans: {
        F0: { decisive_span: '38 lists were filed' },
        F2: { reason: 'no span offered' },
        F3: { decisive_span: '   ' },
      },
    })
    expect(decisiveSpans(c)).toEqual([
      { family: 'F0', span: '38 lists were filed' },
    ])
  })

  it('lists every family’s label unpooled, in stable order', () => {
    const c = claim('y', 'contradicts', {
      label_by_family: { F3: 'contradicts', F0: 'contradicts', F2: 'silent' },
    })
    expect(familyLabels(c)).toEqual([
      { family: 'F0', label: 'contradicts' },
      { family: 'F2', label: 'silent' },
      { family: 'F3', label: 'contradicts' },
    ])
  })
})

describe('the reference-state vocabulary', () => {
  it('names the two absence states the badge can print', () => {
    expect(REFERENCE_STATE_LABEL.none).toBe('no reference')
    expect(REFERENCE_STATE_LABEL.stale).toBe('reference stale')
  })
})
