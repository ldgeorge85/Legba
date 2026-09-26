/**
 * The honesty footer's arithmetic, and the four rules that make it honest.
 *
 * The bar states, unprompted and on every screen, how much of the intelligence
 * was covered. That only helps if the numbers cannot flatter, so what is pinned
 * here is mostly what the figures REFUSE to say: a route that did not answer is
 * the word `unmeasured` and never a zero; a mean never renders without the `n`
 * it was taken over; a measured zero stays a zero; and no figure states
 * anything a route did not carry.
 */
import { describe, it, expect } from 'vitest'
import {
  COVERAGE_FIGURE_PANEL,
  UNMEASURED,
  absenceFigure,
  graderFigure,
  judgeFigure,
  sourcesFigure,
} from '@/lib/coverageFooter'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import type { JudgeStatsResponse, SourceFiringRow } from '@/lib/api'
import type { GraderRoster } from '@/lib/graderRosterModel'
import type { AbsenceResponse } from '@/lib/absenceModel'

const NOW = Date.parse('2026-09-26T16:20:00.000Z')

function firingRow(over: Partial<SourceFiringRow> = {}): SourceFiringRow {
  return {
    source_id: 'source.a',
    state: 'active',
    signals_24h: 10,
    signals_7d: 70,
    last_seen_at: '2026-09-26T16:10:00.000Z',
    age_seconds: 600,
    last_poll_outcome: 'success',
    recent_error_count: 0,
    status: 'firing',
    freshness_grade: 'ok',
    budget_minutes: 120,
    ...over,
  }
}

describe('every figure has a panel that owns it', () => {
  it('each click target is a real registered panel kind', () => {
    for (const [id, kind] of Object.entries(COVERAGE_FIGURE_PANEL)) {
      expect(PANEL_REGISTRY[kind], `${id} → ${kind}`).toBeDefined()
    }
  })
})

describe('sources — the firing matrix over the wired roster', () => {
  it('states firing OVER wired, with the freshest signal as its as-of', () => {
    const f = sourcesFigure(
      [
        firingRow(),
        firingRow({ source_id: 'b', status: 'silent', freshness_grade: 'stale', last_seen_at: '2026-09-26T09:00:00.000Z' }),
        firingRow({ source_id: 'c', status: 'error', freshness_grade: 'ungraded', last_seen_at: null }),
      ],
      false,
      NOW,
    )
    expect(f.value).toBe('1/3 firing')
    expect(f.asOf).toBe('10m ago')
    expect(f.unmeasured).toBe(false)
    expect(f.title).toContain('1 firing · 1 silent · 1 in error · 0 paused, of 3 wired sources')
    // Grades are counted beside the statuses, never folded into them.
    expect(f.title).toContain('1 ok · 0 warn · 1 stale')
    expect(f.title).toContain('1 ungraded')
    expect(f.title).toContain('neither is a stale source')
  })

  it('a failed read is the word, not a zero', () => {
    const f = sourcesFigure(undefined, true, NOW)
    expect(f.value).toBe(UNMEASURED)
    expect(f.unmeasured).toBe(true)
    expect(f.asOf).toBeNull()
    expect(f.title).toContain('/v3/system/source-firing')
  })

  it('a shape that is not a list of rows degrades rather than throwing', () => {
    // Exactly what a stubbed or half-migrated route hands back.
    const f = sourcesFigure({ data: [] } as unknown as SourceFiringRow[], false, NOW)
    expect(f.unmeasured).toBe(true)
  })

  it('an empty roster is a roster with nothing in it, not a roster firing at zero', () => {
    const f = sourcesFigure([], false, NOW)
    expect(f.value).toBe(UNMEASURED)
    expect(f.title).toContain('no source is wired here')
  })

  it('has no as-of when nothing carries a signal instant', () => {
    const f = sourcesFigure([firingRow({ last_seen_at: null })], false, NOW)
    expect(f.asOf).toBeNull()
    expect(f.title).toContain('no as-of')
  })
})

describe('gaps — typed absence for the ACTIVE scope', () => {
  const res = (over: Partial<AbsenceResponse> = {}): AbsenceResponse => ({
    version: '2026-09/k5',
    scope: 'country_g20_ar',
    read_at: '2026-09-26T16:19:00.000Z',
    kinds: {},
    absences: [],
    not_measured: [],
    ...over,
  })

  it('counts the typed absences and carries the READ instant', () => {
    const f = absenceFigure(
      {
        scopeTargetId: 'country_g20_ar',
        scopeLabel: 'Argentina',
        res: res({
          absences: [
            { stale: false } as never,
            { stale: true } as never,
          ],
        }),
        failed: false,
      },
      NOW,
    )
    expect(f.value).toBe('2 typed')
    expect(f.asOf).toBe('1m ago')
    expect(f.title).toContain('2 typed absences on Argentina')
    expect(f.title).toContain('1 of them are last known and NOT re-checked')
    expect(f.title).toContain('Every absence kind was read')
  })

  it('a desk read with nothing found is a MEASURED zero, not unmeasured', () => {
    const f = absenceFigure(
      { scopeTargetId: 'country_g20_ar', res: res(), failed: false },
      NOW,
    )
    expect(f.value).toBe('0 typed')
    expect(f.unmeasured).toBe(false)
  })

  it('a scope with no single desk has no count to give, and says why', () => {
    const f = absenceFigure({ scopeTargetId: null, res: undefined, failed: false }, NOW)
    expect(f.value).toBe(UNMEASURED)
    expect(f.unmeasured).toBe(true)
    expect(f.title).toContain('one desk at a time')
  })

  it('names the kinds that could not be read for this scope at all', () => {
    const f = absenceFigure(
      {
        scopeTargetId: 'country_g20_ar',
        res: res({ not_measured: ['no loaded collection names this desk'] }),
        failed: false,
      },
      NOW,
    )
    expect(f.title).toContain('1 absence kind could not be read for this scope')
  })

  it('a failed read names the route and the desk', () => {
    const f = absenceFigure(
      { scopeTargetId: 'country_g20_ar', res: undefined, failed: true },
      NOW,
    )
    expect(f.value).toBe(UNMEASURED)
    expect(f.title).toContain('/v3/absence?scope=country_g20_ar')
  })
})

describe('judge — faithfulness, never without its n', () => {
  const stats = (over: Partial<JudgeStatsResponse> = {}): JudgeStatsResponse =>
    ({
      generated_at: '2026-09-26T16:13:00.000Z',
      window_days: 7,
      measured: true,
      pools_across_pipeline_versions: false,
      totals: {
        critiques: 4497,
        by_status: {},
        attributed: 2973,
        unattributed: 1524,
        providers: 3,
        adjudicated_n: 4497,
        adjudicated_share: 0.98,
        faithfulness_n: 4497,
        faithfulness_mean: 0.8298,
        judge_calls: 100,
        judge_call_errors: 0,
      },
      providers: [],
      pipeline_versions: [],
      cells: [],
      sentinels: {},
      judge_statuses: [],
      ...over,
    }) as JudgeStatsResponse

  it('states the mean with its unit, its window and its as-of', () => {
    const f = judgeFigure(stats(), false, NOW)
    expect(f.value).toBe('0.83 faithfulness')
    expect(f.asOf).toBe('7m ago')
    expect(f.title).toContain('over 4,497 scored critiques in a 7d window')
    expect(f.title).toContain('2,973 attributed')
    expect(f.title).toContain('1,524 could not be attributed')
  })

  it('carries the pooled-graders caveat when the window straddles a pipeline change', () => {
    const f = judgeFigure(stats({ pools_across_pipeline_versions: true }), false, NOW)
    expect(f.title).toContain('pools two different graders')
  })

  it('a null mean is the word, never 0.00', () => {
    const s = stats()
    const f = judgeFigure(
      { ...s, totals: { ...s.totals, faithfulness_mean: null } },
      false,
      NOW,
    )
    expect(f.value).toBe(UNMEASURED)
    expect(f.value).not.toContain('0.00')
    expect(f.title).toContain('no mean to state')
  })

  it('`measured: false` is a FAILED READ, not a quiet judge', () => {
    const f = judgeFigure(stats({ measured: false }), false, NOW)
    expect(f.unmeasured).toBe(true)
    expect(f.title).toContain('failed read, not a quiet judge')
  })
})

describe('grader — how much of the roster the reference reached', () => {
  const roster = (over: Partial<GraderRoster> = {}): GraderRoster =>
    ({
      available: true,
      nights: 7,
      grain: 'desk',
      as_of: '2026-09-26T09:20:00.000Z',
      desks: [],
      roster: {
        desks_graded: 44,
        desks_unmeasured: 143,
        correctness_mean_of_desks: 0.4606,
        coverage_mean_of_desks: 0.0812,
        claims_pooled: {
          n_claims: 940,
          n_decided: 80,
          n_contains: 40,
          correctness_pooled: 0.5,
          coverage_pooled: 0.085,
        },
      },
      honesty_note: '',
      ...over,
    }) as GraderRoster

  it('states graded OVER the roster, with correctness beside its coverage', () => {
    const f = graderFigure(roster(), false, NOW)
    expect(f.value).toBe('44/187 desks graded')
    expect(f.asOf).toBe('7h ago')
    expect(f.title).toContain('143 are unmeasured')
    expect(f.title).toContain('correctness 46.1% at coverage 8.1%')
    expect(f.title).toContain('read them together')
  })

  it('a null mean prints the word rather than a percentage of nothing', () => {
    const r = roster()
    const f = graderFigure(
      { ...r, roster: { ...r.roster, correctness_mean_of_desks: null } },
      false,
      NOW,
    )
    expect(f.title).toContain(`correctness ${UNMEASURED}`)
  })

  it('an empty roster is "never run", not a roster of zeros', () => {
    const f = graderFigure(roster({ available: false }), false, NOW)
    expect(f.value).toBe(UNMEASURED)
    expect(f.title).toContain('not a roster of zeros')
  })

  it('a failed read names its route', () => {
    const f = graderFigure(undefined, true, NOW)
    expect(f.value).toBe(UNMEASURED)
    expect(f.title).toContain('/v3/eval/grader_roster')
  })
})
