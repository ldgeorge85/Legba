import { describe, expect, it } from 'vitest'
import {
  armCell,
  armRows,
  calibrationView,
  headerView,
  scorecardArmColumns,
  scorecardRows,
  scoresWithheld,
  withheldLine,
  scorecardState,
  type ScorecardResponse,
} from './scorecardModel'

const NOW = new Date('2026-11-12T00:00:00Z')

function q(slug: string, extra: Record<string, unknown> = {}) {
  return {
    id: slug,
    slug,
    question: `Fictional ${slug}`,
    status: 'open',
    resolver_kind: 'official_result',
    due_at: '2026-12-01T00:00:00Z',
    numbers: [],
    ...extra,
  }
}

const BODY = {
  available: true,
  header: { n_questions: 2, n_sealed: 2, n_open: 2, n_resolved: 0, n_void: 0, n_overdue: 0, min_sample: 5, skill_claim: false, statement: 's' },
  questions: [
    q('FX2', { due_at: '2026-12-09T00:00:00Z', single_arm: true, numbers: [{ issuer_kind: 'operator', p: 0.75 }] }),
    q('FX1', {
      numbers: [
        { issuer_kind: 'weird', p: 0.4 },
        { issuer_kind: 'tower', p: null, absent_reason: 'no analyst number' },
        { issuer_kind: 'operator', p: 0.85 },
      ],
      spread: { n: 2, min: 0.4, max: 0.85, range: 0.45 },
    }),
  ],
  calibration: { available: false, min_sample: 30, n: 0, reason: 'n too small (0 < 30)' },
} as unknown as ScorecardResponse

describe('scorecardModel', () => {
  it('orders arm columns canonically, unknown last', () => {
    expect(scorecardArmColumns(BODY)).toEqual(['operator', 'tower', 'weird'])
    expect(scorecardArmColumns(null)).toEqual([])
  })

  it('projects rows by due date with dashes and reasons, never blanks', () => {
    const rows = scorecardRows(BODY, NOW)
    expect(rows.map((r) => r.slug)).toEqual(['FX1', 'FX2'])
    const fx1 = rows[0]
    expect(fx1.cells.map((c) => c.text)).toEqual(['0.85', '—', '0.40'])
    expect(fx1.cells[1].title).toBe('no analyst number')
    expect(fx1.spread).toBe('0.450')
    expect(rows[1].singleArm).toBe(true)
    expect(rows[1].cells[1].text).toBe('—')
  })

  it('marks overdue and tags a proposal with needs judgement', () => {
    const p = {
      ...BODY,
      questions: [
        q('FX3', { due_at: '2026-11-01T00:00:00Z', proposal: { outcome: 'needs_judgement', judgements: [{ key: 'k', question: 'x' }] } }),
      ],
    } as unknown as ScorecardResponse
    const r = scorecardRows(p, NOW)[0]
    expect(r.overdue).toBe(true)
    expect(r.proposed).toBe(true)
    expect(r.needsJudgement).toBe(true)
    expect(r.outcome).toBe('needs_judgement')
  })

  it('splits evidence refs into urls and others; shows Brier only when resolved', () => {
    const p = {
      ...BODY,
      scores_withheld: false,
      questions: [
        q('FX4', {
          status: 'resolved',
          outcome: 'yes',
          numbers: [{ issuer_kind: 'operator', p: 0.8, brier: 0.04 }],
          resolution: { outcome: 'yes', evidence_refs: ['url:https://example.test/a', 'signal:abc'] },
        }),
        q('FX5', { numbers: [{ issuer_kind: 'operator', p: 0.8, brier: 0.04 }] }),
      ],
    } as unknown as ScorecardResponse
    const rows = scorecardRows(p, NOW)
    const r4 = rows.find((r) => r.slug === 'FX4')!
    expect(r4.evidence).toEqual({ count: 2, urls: ['https://example.test/a'], others: ['signal:abc'] })
    expect(r4.cells[0].brier).toBe('0.040')
    expect(r4.outcome).toBe('yes')
    expect(rows.find((r) => r.slug === 'FX5')!.cells[0].brier).toBeNull()
    // Below 5 resolved the route withholds scores: no Brier even on a resolved row.
    const held = scorecardRows({ ...p, scores_withheld: true } as ScorecardResponse, NOW)
    expect(held.find((r) => r.slug === 'FX4')!.cells[0].brier).toBeNull()
    expect(held.find((r) => r.slug === 'FX4')!.outcome).toBe('yes')
  })

  it('a payload without scores_withheld:false is treated as withheld', () => {
    expect(scoresWithheld(BODY)).toBe(true)
    expect(scoresWithheld({ ...BODY, scores_withheld: false })).toBe(false)
    expect(withheldLine({ ...BODY, scores_min_resolved: 5 })).toBe('0 resolved; scores appear at 5')
    const a = armRows({ ...BODY, arms: [{ issuer_kind: 'operator', n: 3, mean_brier: 0.1, baseline_mean_brier: 0.25, skill_vs_climatology: 0.6, below_min_sample: false }] } as unknown as ScorecardResponse)
    expect(a[0]).toEqual({ kind: 'operator', n: 3, mean: 'scores appear at 5', baseline: '—', skill: null })
  })

  it('armCell never prints 0 or blank', () => {
    const small = { issuer_kind: 'operator', n: 0, mean_brier: null, baseline_mean_brier: null, skill_vs_climatology: null, below_min_sample: true }
    expect(armCell(small)).toBe('n too small (0 < 5)')
    expect(armCell({ ...small, n: 7, mean_brier: 0.1234, below_min_sample: false })).toBe('0.123 (n=7)')
    expect(armCell({ ...small, mean_brier: null, below_min_sample: false, n: 9 })).toBe('n too small (9 < 5)')
  })

  it('armRows hides skill when not stated', () => {
    const p = {
      ...BODY,
      scores_withheld: false,
      arms: [
        { issuer_kind: 'operator', n: 7, mean_brier: 0.2, baseline_mean_brier: 0.25, skill_vs_climatology: 0.2, below_min_sample: false },
        { issuer_kind: 'tower', n: 1, mean_brier: null, baseline_mean_brier: null, skill_vs_climatology: null, below_min_sample: true },
      ],
    } as unknown as ScorecardResponse
    const a = armRows(p)
    expect(a[0]).toMatchObject({ mean: '0.200 (n=7)', baseline: '0.250', skill: '0.200' })
    expect(a[1]).toMatchObject({ mean: 'n too small (1 < 5)', baseline: '—', skill: null })
  })

  it('calibrationView gives a reason, not zero bins, when unavailable', () => {
    expect(calibrationView(BODY)).toEqual({ available: false, reason: 'n too small (0 < 30)', bins: [] })
    const ok = calibrationView({
      available: true,
      calibration: { available: true, min_sample: 30, n: 40, bins: [{ lo: 0, hi: 0.1, n: 4, mean_p: 0.05, observed: 0.25 }] },
    })
    expect(ok.bins[0]).toEqual({ range: '0.0–0.1', n: 4, meanP: '0.05', observed: '0.25' })
    expect(calibrationView(null).available).toBe(false)
  })

  it('headerView and state handle null and unavailable payloads', () => {
    expect(headerView(BODY).noClaimMarker).toBe('scores appear at 5')
    expect(headerView({ ...BODY, scores_withheld: false }).noClaimMarker).toBeNull()
    expect(headerView(BODY).resolvedText).toBe('0 resolved')
    expect(headerView(null).resolvedText).toBe('0 resolved')
    expect(scorecardState(null)).toBe('not_deployed')
    expect(scorecardState({ available: false })).toBe('unavailable')
    expect(scorecardState(BODY)).toBe('ready')
    expect(scorecardRows({ available: false }, NOW)).toEqual([])
  })
})
