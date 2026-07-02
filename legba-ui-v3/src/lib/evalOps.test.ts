import { describe, it, expect } from 'vitest'
import {
  buildScorecards,
  calibrationBanner,
  critScoreTrend,
  scoreBand,
  lagSeverity,
  sortLag,
  diffLines,
  diffStat,
  mapGovernorEnvelope,
  summariseGovernor,
  verifyStatus,
  chainHealth,
  actorRollup,
  relTime,
  ACUTE_TARGET_N,
  bandTone,
  isInsufficient,
  insufficientLabel,
  evalBadge,
  type ScorecardRow,
  type ConsumerLagRow,
  type GovernorEventRow,
  type AuditEntryRow,
  type CalibrationScoreboard,
  type DimensionEval,
} from './evalOps'

// A fully-populated, honest scoreboard we mutate per-case.
function cal(over: Partial<CalibrationScoreboard> = {}): CalibrationScoreboard {
  return {
    available: true,
    produced_at: '2026-06-30T00:00:00Z',
    brier: 0.2,
    brier_exogenous: 0.18,
    exogenous_sample_size: 12,
    sample_size: 40,
    insufficient_exogenous: false,
    self_consistency_only: false,
    brier_forecast_acute: 0.11,
    brier_skill_score: 0.25,
    forecast_acute_sample_size: 18,
    forecast_acute_ready: true,
    forecast_acute_degenerate: false,
    forecast_acute_status: 'ready',
    forecast_unproven: false,
    calibration_thin: false,
    refs: ['cal-1'],
    ...over,
  }
}

// --------------------------------------------------------------------------
// scorecard
// --------------------------------------------------------------------------
const SC: ScorecardRow[] = [
  {
    id: 's1',
    analyst_id: 'cred.analyst',
    analyst_version: 'v1',
    scores: { calibration: 0.4, evidence: 0.6 },
    overall_score: 0.5,
    ground_truth_accuracy: 0.45,
    produced_at: '2026-06-01T00:00:00Z',
  },
  {
    id: 's2',
    analyst_id: 'cred.analyst',
    analyst_version: 'v1',
    scores: { calibration: 0.8, evidence: 0.8 },
    overall_score: 0.8,
    ground_truth_accuracy: 0.7,
    produced_at: '2026-06-03T00:00:00Z',
  },
  {
    id: 's3',
    analyst_id: 'coup.analyst',
    analyst_version: 'v2',
    scores: { calibration: 0.2 },
    overall_score: 0.3,
    produced_at: '2026-06-02T00:00:00Z',
  },
]

describe('buildScorecards', () => {
  it('groups by analyst, computes trend + axis means, worst-first', () => {
    const cards = buildScorecards(SC)
    expect(cards).toHaveLength(2)
    // worst latest_overall first → coup.analyst (0.3) before cred (0.8)
    expect(cards[0].analyst_id).toBe('coup.analyst')
    const cred = cards.find((c) => c.analyst_id === 'cred.analyst')!
    expect(cred.latest_overall).toBe(0.8) // newest row
    expect(cred.mean_overall).toBeCloseTo(0.65)
    expect(cred.trend_delta).toBeCloseTo(0.3) // 0.8 - 0.5
    expect(cred.axis_means.calibration).toBeCloseTo(0.6) // (0.4+0.8)/2
    expect(cred.latest_accuracy).toBe(0.7) // newest row's accuracy
  })

  it('rows are newest-first for display', () => {
    const cred = buildScorecards(SC).find((c) => c.analyst_id === 'cred.analyst')!
    expect(cred.rows[0].id).toBe('s2')
    expect(cred.rows[1].id).toBe('s1')
  })

  it('handles analysts with no ground-truth accuracy', () => {
    const coup = buildScorecards(SC).find((c) => c.analyst_id === 'coup.analyst')!
    expect(coup.latest_accuracy).toBeNull()
  })
})

describe('critScoreTrend', () => {
  it('returns chronological overall series', () => {
    const t = critScoreTrend(SC.filter((r) => r.analyst_id === 'cred.analyst'))
    expect(t.map((x) => x.overall)).toEqual([0.5, 0.8])
  })
})

describe('scoreBand', () => {
  it('bands by threshold', () => {
    expect(scoreBand(0.9)).toBe('good')
    expect(scoreBand(0.6)).toBe('warn')
    expect(scoreBand(0.2)).toBe('bad')
  })
})

// --------------------------------------------------------------------------
// calibration / skill scoreboard — the honest-top reducer (P4-T4)
// --------------------------------------------------------------------------
describe('calibrationBanner', () => {
  it('absent (null / unavailable) reads "no calibration finding yet", not insufficient', () => {
    for (const c of [null, undefined, cal({ available: false })]) {
      const b = calibrationBanner(c)
      expect(b.absent).toBe(true)
      expect(b.exogenous.label).toBe('no calibration finding computed yet')
      expect(b.exogenous.insufficient).toBe(false) // distinct from insufficient-sample
      expect(b.exogenous.value).toBeNull()
      expect(b.acute.bss).toBeNull()
    }
  })

  it('insufficient exogenous sample -> the VERBATIM message, no number', () => {
    const b = calibrationBanner(
      cal({ insufficient_exogenous: true, exogenous_sample_size: 3, sample_size: 40 }),
    )
    expect(b.exogenous.insufficient).toBe(true)
    expect(b.exogenous.value).toBeNull()
    expect(b.exogenous.label).toBe('INSUFFICIENT exogenous sample (n_exo=3/40)')
  })

  it('null exogenous brier is treated as insufficient (no leaked number)', () => {
    const b = calibrationBanner(cal({ brier_exogenous: null, brier: null }))
    expect(b.exogenous.insufficient).toBe(true)
    expect(b.exogenous.value).toBeNull()
  })

  it('sufficient exogenous sample shows the Brier number', () => {
    const b = calibrationBanner(cal({ insufficient_exogenous: false, brier_exogenous: 0.174 }))
    expect(b.exogenous.insufficient).toBe(false)
    expect(b.exogenous.value).toBe(0.174)
    expect(b.exogenous.label).toBe('0.174')
  })

  it('degenerate acute pilot -> "skill claim withheld", NO bss number', () => {
    // gate order: degenerate wins even when ready + a positive bss is present.
    const b = calibrationBanner(
      cal({ forecast_acute_degenerate: true, forecast_acute_ready: true, brier_skill_score: 0.9 }),
    )
    expect(b.acute.tag).toBe('degenerate')
    expect(b.acute.label).toBe('degenerate — skill claim withheld')
    expect(b.acute.bss).toBeNull()
  })

  it('not-ready acute pilot -> accumulating (n/target), no number', () => {
    const b = calibrationBanner(
      cal({ forecast_acute_ready: false, forecast_acute_sample_size: 11 }),
    )
    expect(b.acute.tag).toBe('accumulating')
    expect(b.acute.label).toBe(`accumulating (n=11/${ACUTE_TARGET_N})`)
    expect(b.acute.bss).toBeNull()
  })

  it('ready + non-degenerate + positive bss -> the BSS number, tag ready', () => {
    const b = calibrationBanner(
      cal({ forecast_acute_ready: true, forecast_acute_degenerate: false, brier_skill_score: 0.32 }),
    )
    expect(b.acute.tag).toBe('ready')
    expect(b.acute.bss).toBe(0.32)
    expect(b.acute.label).toBe('BSS 0.320')
  })

  it('ready but non-positive bss -> still withheld, NO bare number', () => {
    for (const bss of [0, -0.1, null]) {
      const b = calibrationBanner(
        cal({ forecast_acute_ready: true, forecast_acute_degenerate: false, brier_skill_score: bss }),
      )
      expect(b.acute.tag).not.toBe('ready')
      expect(b.acute.bss).toBeNull()
    }
  })
})

// --------------------------------------------------------------------------
// consumer lag
// --------------------------------------------------------------------------
const LAG: ConsumerLagRow[] = [
  {
    stream: 'SIGNALS',
    durable: 'tgt-brazil',
    scope_kind: 'target',
    scope_id: 'brazil',
    num_pending: 5,
    num_ack_pending: 1,
    num_redelivered: 0,
    num_waiting: 2,
    delivered_stream_seq: 100,
    ack_floor_stream_seq: 99,
  },
  {
    stream: 'SIGNALS',
    durable: 'src-gdelt',
    scope_kind: 'source',
    scope_id: 'gdelt',
    num_pending: 1500,
    num_ack_pending: 3,
    num_redelivered: 40,
    num_waiting: 0,
    delivered_stream_seq: 9000,
    ack_floor_stream_seq: 7500,
  },
]

describe('lagSeverity', () => {
  it('ok when pending low and no redeliveries', () => {
    expect(lagSeverity(LAG[0])).toBe('ok')
  })
  it('critical when pending over critical threshold', () => {
    expect(lagSeverity(LAG[1])).toBe('critical')
  })
  it('critical on poison redeliveries even with low pending', () => {
    expect(
      lagSeverity({ num_pending: 0, num_ack_pending: 0, num_redelivered: 30 }),
    ).toBe('critical')
  })
  it('warn on a single redelivery', () => {
    expect(
      lagSeverity({ num_pending: 1, num_ack_pending: 0, num_redelivered: 1 }),
    ).toBe('warn')
  })
})

describe('sortLag', () => {
  it('worst pending first', () => {
    expect(sortLag(LAG)[0].scope_id).toBe('gdelt')
  })
})

// --------------------------------------------------------------------------
// prompt-module diff
// --------------------------------------------------------------------------
describe('diffLines', () => {
  it('marks same / add / del', () => {
    const lines = diffLines('a\nb\nc', 'a\nB\nc\nd')
    const stat = diffStat(lines)
    expect(stat.unchanged).toBe(2) // a, c
    expect(stat.deleted).toBe(1) // b
    expect(stat.added).toBe(2) // B, d
  })
  it('numbers old/new lines independently', () => {
    const lines = diffLines('x\ny', 'x\nz\ny')
    const added = lines.find((l) => l.op === 'add')!
    expect(added.text).toBe('z')
    expect(added.oldNo).toBeNull()
    expect(added.newNo).toBe(2)
  })
  it('identical text is all same', () => {
    expect(diffStat(diffLines('p\nq', 'p\nq'))).toEqual({
      added: 0,
      deleted: 0,
      unchanged: 2,
    })
  })
})

// --------------------------------------------------------------------------
// governor events
// --------------------------------------------------------------------------
describe('mapGovernorEnvelope', () => {
  it('maps a block payload', () => {
    const row = mapGovernorEnvelope({
      pack_id: 'osint',
      decision: 'block',
      cause: 'over_budget',
      tool_name: 'web.search',
      cap_dimension: 'usd',
      cap_limit: 5,
      observed_value: 6.2,
      occurred_at: '2026-06-03T00:00:00Z',
    })
    expect(row).not.toBeNull()
    expect(row!.decision).toBe('block')
    expect(row!.cap_limit).toBe(5)
    expect(row!._live).toBe(true)
  })
  it('rejects payloads without a valid decision', () => {
    expect(mapGovernorEnvelope({ pack_id: 'x' })).toBeNull()
    expect(mapGovernorEnvelope(undefined)).toBeNull()
  })
  it('defaults missing fields', () => {
    const row = mapGovernorEnvelope({ decision: 'allow' })!
    expect(row.cause).toBe('ok')
    expect(row.budget_account).toBe('system')
    expect(row.tenant_id).toBe('default')
  })
})

describe('summariseGovernor', () => {
  it('counts blocks / allows + causes + blocked packs', () => {
    const rows: GovernorEventRow[] = [
      mapGovernorEnvelope({ pack_id: 'a', decision: 'block', cause: 'over_budget' })!,
      mapGovernorEnvelope({ pack_id: 'a', decision: 'block', cause: 'over_budget' })!,
      mapGovernorEnvelope({ pack_id: 'b', decision: 'block', cause: 'not_allowed' })!,
      mapGovernorEnvelope({ pack_id: 'a', decision: 'allow' })!,
    ]
    const s = summariseGovernor(rows)
    expect(s.blocked).toBe(3)
    expect(s.allowed).toBe(1)
    expect(s.blocked_packs.sort()).toEqual(['a', 'b'])
    expect(s.by_cause.over_budget).toBe(2)
    expect(s.by_cause.not_allowed).toBe(1)
  })
})

// --------------------------------------------------------------------------
// audit chain
// --------------------------------------------------------------------------
const AUDIT: AuditEntryRow[] = [
  {
    id: 'a1',
    occurred_at: '2026-06-03T00:00:00Z',
    actor_id: 'op',
    actor_role: 'operator',
    namespace: 'target',
    descriptor_id: 'brazil',
    action: 'register',
    from_version: null,
    to_version: 'v1',
    change_summary: {},
    signer_did: 'did:key:z6Mk',
    signature_verified: true,
  },
  {
    id: 'a2',
    occurred_at: '2026-06-03T01:00:00Z',
    actor_id: 'op',
    actor_role: 'operator',
    namespace: 'target',
    descriptor_id: 'brazil',
    action: 'update',
    from_version: 'v1',
    to_version: 'v2',
    change_summary: {},
    signer_did: 'did:key:z6Mk',
    signature_verified: false,
  },
  {
    id: 'a3',
    occurred_at: '2026-06-03T02:00:00Z',
    actor_id: 'op',
    actor_role: 'operator',
    namespace: 'target',
    descriptor_id: 'brazil',
    action: 'promote',
    from_version: 'v2',
    to_version: 'v3',
    change_summary: {},
    signer_did: 'did:key:z6Mk',
    signature_verified: null,
  },
]

describe('verifyStatus + chainHealth', () => {
  it('classifies each verify state', () => {
    expect(verifyStatus(AUDIT[0])).toBe('verified')
    expect(verifyStatus(AUDIT[1])).toBe('failed')
    expect(verifyStatus(AUDIT[2])).toBe('unverifiable')
  })
  it('chain is NOT intact when any verify failed', () => {
    const h = chainHealth(AUDIT)
    expect(h.total).toBe(3)
    expect(h.verified).toBe(1)
    expect(h.failed).toBe(1)
    expect(h.unverifiable).toBe(1)
    expect(h.intact).toBe(false)
  })
  it('chain intact when no failures', () => {
    expect(chainHealth([AUDIT[0], AUDIT[2]]).intact).toBe(true)
  })
})

// --------------------------------------------------------------------------
// actor rollup
// --------------------------------------------------------------------------
describe('actorRollup', () => {
  it('rolls up by kind + lifecycle + errors', () => {
    const r = actorRollup([
      { actor_kind: 'source', lifecycle: 'active', error_count: 0 },
      { actor_kind: 'analyst', lifecycle: 'error', error_count: 3 },
      { actor_kind: 'analyst', lifecycle: 'active', error_count: 0 },
    ])
    expect(r.byKind.analyst).toBe(2)
    expect(r.byKind.source).toBe(1)
    expect(r.byLifecycle.error).toBe(1)
    expect(r.errors).toBe(3)
    expect(r.stale).toBe(1)
  })
})

describe('relTime', () => {
  it('handles null + future + recent', () => {
    expect(relTime(null)).toBe('never')
    expect(relTime('not-a-date')).toBe('never')
    expect(relTime(new Date().toISOString())).toMatch(/s ago$/)
  })
})

// --------------------------------------------------------------------------
// country banded scorecard — the honest-top drillable card (P4-T3/T5)
// --------------------------------------------------------------------------
function ev(over: Partial<DimensionEval> = {}): DimensionEval {
  return {
    faithfulness: 0.88,
    correctness_vs_reference: 0.71,
    n_labeled: 12,
    faithfulness_flagged: false,
    ...over,
  }
}

describe('isInsufficient', () => {
  it('true only for the insufficient-evidence band', () => {
    expect(isInsufficient({ band: 'insufficient-evidence' })).toBe(true)
    expect(isInsufficient({ band: 'elevated' })).toBe(false)
    expect(isInsufficient({ band: 'watch' })).toBe(false)
  })
})

describe('bandTone', () => {
  it('maps insufficient-evidence to its own honest tone (never a severity)', () => {
    expect(bandTone('insufficient-evidence')).toBe('insufficient')
  })
  it('maps known severity bands', () => {
    expect(bandTone('critical')).toBe('critical')
    expect(bandTone('high')).toBe('high')
    expect(bandTone('severe')).toBe('high')
    expect(bandTone('elevated')).toBe('elevated')
    expect(bandTone('watch')).toBe('watch')
    expect(bandTone('moderate')).toBe('watch')
    expect(bandTone('good')).toBe('good')
    expect(bandTone('stable')).toBe('good')
  })
  it('falls back to watch for an unknown label (no invented severity)', () => {
    expect(bandTone('mystery')).toBe('watch')
  })
})

describe('insufficientLabel', () => {
  it('renders each honest reason as a human string', () => {
    expect(insufficientLabel('no-finding')).toBe('no unit finding yet')
    expect(insufficientLabel('verify-failed')).toBe('faithfulness verify never ran')
    expect(insufficientLabel('below-floor')).toBe('below confidence floor')
    expect(insufficientLabel('low-faithfulness')).toBe('excluded: low faithfulness')
    expect(insufficientLabel('no-severity-tag')).toBe('no severity emitted')
  })
  it('degrades gracefully for an unknown / missing reason', () => {
    expect(insufficientLabel('weird')).toBe('insufficient (weird)')
    expect(insufficientLabel(null)).toBe('insufficient')
    expect(insufficientLabel(undefined)).toBe('insufficient')
  })
})

describe('evalBadge', () => {
  it('composes faithfulness + correctness + n when measured', () => {
    expect(evalBadge(ev())).toBe('faithfulness 0.88 | correctness 0.71 (n=12)')
  })
  it('shows only the measured axes', () => {
    expect(evalBadge(ev({ correctness_vs_reference: null }))).toBe('faithfulness 0.88 (n=12)')
    expect(evalBadge(ev({ faithfulness: null }))).toBe('correctness 0.71 (n=12)')
  })
  it('unmeasured when both axes are null (never a fabricated number)', () => {
    expect(evalBadge(ev({ faithfulness: null, correctness_vs_reference: null }))).toBe('unmeasured')
    expect(evalBadge(null)).toBe('unmeasured')
    expect(evalBadge(undefined)).toBe('unmeasured')
  })
})
