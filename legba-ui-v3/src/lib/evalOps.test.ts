import { describe, it, expect } from 'vitest'
import {
  buildScorecards,
  externalTruthLabel,
  fmtRate,
  orderedStrata,
  EXTERNAL_TRUTH_STRATA,
  type ExternalTruthPopulation,
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
  operatorSegment,
  correctnessLabel,
  orderedBandHorizons,
  bandRateLabel,
  bandCalibrationEmpty,
  bandCalibrationState,
  bandOutcomeRows,
  bandShareLabel,
  bandSliceHorizon,
  bandSliceRows,
  BAND_OUTCOME_MEANING,
  BAND_OUTCOME_ORDER,
  type ScorecardRow,
  type UnitCorrectnessRow,
  type ConsumerLagRow,
  type GovernorEventRow,
  type AuditEntryRow,
  type CalibrationScoreboard,
  type DimensionEval,
  type BandCalibrationSection,
  type BandCalibrationHorizon,
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
    band_calibration: null,
    ...over,
  }
}

function horizon(over: Partial<BandCalibrationHorizon> = {}): BandCalibrationHorizon {
  return {
    resolved: 10,
    open: 2,
    outcomes: { held: 6, reverted: 2, worsened: 2 },
    confirmed: 8,
    reverted: 2,
    scored: 10,
    excluded_insufficient: 0,
    excluded_unresolvable: 0,
    persistence_rate: 0.8,
    reversal_rate: 0.2,
    ...over,
  }
}

function bandCalibration(over: Partial<BandCalibrationSection> = {}): BandCalibrationSection {
  return {
    available: true,
    produced_at: '2026-07-27T00:00:00Z',
    claims_total: 12,
    resolution_spec: 'hard_band_at_horizon_v1',
    horizons: { '14d': horizon(), '28d': horizon({ scored: 6, persistence_rate: 0.5 }) },
    by_direction: {},
    by_dimension: {},
    no_brier: true,
    honesty_note:
      'Band-persistence and reversal rates are ordinal stability measures over ' +
      'later scorecard rows. Bands are categorical risk verdicts, not ' +
      'probabilities: no Brier score, Brier skill score, or forecast-skill ' +
      'claim exists (or can exist) for this harness.',
    refs: ['bc-1'],
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

// --------------------------------------------------------------------------
// band-calibration harness (P2-3, NOT a Brier score)
// --------------------------------------------------------------------------

describe('orderedBandHorizons', () => {
  it('orders 14d before 28d, then any extras alphabetically', () => {
    const h = { '28d': horizon(), '90d': horizon(), '14d': horizon() }
    expect(orderedBandHorizons(h).map(([k]) => k)).toEqual(['14d', '28d', '90d'])
  })

  it('handles a section missing one of the known horizons', () => {
    const h = { '28d': horizon() }
    expect(orderedBandHorizons(h).map(([k]) => k)).toEqual(['28d'])
  })

  it('empty map → empty order', () => {
    expect(orderedBandHorizons({})).toEqual([])
  })
})

describe('bandRateLabel', () => {
  it('formats a rate as a rounded percentage', () => {
    expect(bandRateLabel(0.8)).toBe('80%')
    expect(bandRateLabel(0.333)).toBe('33%')
    expect(bandRateLabel(0)).toBe('0%')
    expect(bandRateLabel(1)).toBe('100%')
  })

  it('a null rate (zero scored denominator) is an honest empty label, never a fabricated 0%', () => {
    expect(bandRateLabel(null)).toBe('— (no scored claims yet)')
  })
})

describe('bandCalibrationEmpty', () => {
  it('absent section (no tracker finding yet) is empty', () => {
    expect(bandCalibrationEmpty(null)).toBe(true)
    expect(bandCalibrationEmpty(undefined)).toBe(true)
    expect(bandCalibrationEmpty(bandCalibration({ available: false, claims_total: null }))).toBe(
      true,
    )
  })

  it('available but zero claims logged is still empty', () => {
    expect(bandCalibrationEmpty(bandCalibration({ claims_total: 0 }))).toBe(true)
    expect(bandCalibrationEmpty(bandCalibration({ claims_total: null }))).toBe(true)
  })

  it('available with graded claims is not empty', () => {
    expect(bandCalibrationEmpty(bandCalibration({ claims_total: 12 }))).toBe(false)
  })
})

describe('bandCalibrationState', () => {
  it('separates "no finding at all" from "a finding that logged nothing"', () => {
    // No tracker finding — there is not even an as-of to show.
    expect(bandCalibrationState(null)).toBe('unavailable')
    expect(bandCalibrationState(undefined)).toBe('unavailable')
    expect(bandCalibrationState(bandCalibration({ available: false }))).toBe('unavailable')
    // A finding exists (so there IS an as-of), it just claimed nothing.
    expect(bandCalibrationState(bandCalibration({ claims_total: 0 }))).toBe('no-claims')
    expect(bandCalibrationState(bandCalibration({ claims_total: null }))).toBe('no-claims')
    expect(bandCalibrationState(bandCalibration({ claims_total: 12 }))).toBe('graded')
  })

  it('agrees with bandCalibrationEmpty on the union of the two absences', () => {
    for (const s of [
      null,
      bandCalibration({ available: false }),
      bandCalibration({ claims_total: 0 }),
      bandCalibration({ claims_total: 12 }),
    ]) {
      expect(bandCalibrationEmpty(s)).toBe(bandCalibrationState(s) !== 'graded')
    }
  })
})

describe('bandOutcomeRows', () => {
  it('orders the closed vocabulary the resolver does, and shares over RESOLVED', () => {
    const h = horizon({
      resolved: 12,
      outcomes: { reverted: 2, held: 6, unresolvable: 1, worsened: 2, insufficient: 1 },
    })
    const rows = bandOutcomeRows(h)
    expect(rows.map((r) => r.outcome)).toEqual([
      'held',
      'worsened',
      'reverted',
      'insufficient',
      'unresolvable',
    ])
    expect(rows[0]).toMatchObject({ n: 6, share: 0.5, scored: true })
    // The two abstains are reported but held OUT of the rate denominator.
    expect(rows.find((r) => r.outcome === 'insufficient')!.scored).toBe(false)
    expect(rows.find((r) => r.outcome === 'unresolvable')!.scored).toBe(false)
  })

  it('an outcome that never occurred is absent, not a zero row', () => {
    const rows = bandOutcomeRows(horizon({ resolved: 6, outcomes: { held: 6 } }))
    expect(rows.map((r) => r.outcome)).toEqual(['held'])
  })

  it('nothing resolved → no rows at all (never six zeroes)', () => {
    expect(bandOutcomeRows(horizon({ resolved: 0, outcomes: {} }))).toEqual([])
  })

  it('a zero resolved denominator yields a null share, never a fabricated 0%', () => {
    // Defensive: counts present but `resolved` not yet advanced.
    const rows = bandOutcomeRows(horizon({ resolved: 0, outcomes: { held: 3 } }))
    expect(rows[0].share).toBeNull()
    expect(bandShareLabel(rows[0].share)).toBe('—')
  })

  it('an outcome this build does not know sorts after the known vocabulary', () => {
    const rows = bandOutcomeRows(
      horizon({ resolved: 4, outcomes: { zzz_future: 1, held: 2, aaa_future: 1 } }),
    )
    expect(rows.map((r) => r.outcome)).toEqual(['held', 'aaa_future', 'zzz_future'])
    // Unknown outcomes are not silently counted into the rate denominator.
    expect(rows.find((r) => r.outcome === 'zzz_future')!.scored).toBe(false)
  })

  it('every outcome in the closed vocabulary carries a meaning for its hover', () => {
    for (const o of BAND_OUTCOME_ORDER) {
      expect(BAND_OUTCOME_MEANING[o]).toBeTruthy()
    }
  })
})

describe('bandShareLabel', () => {
  it('rounds a share to a percentage, and renders an absence as the dash', () => {
    expect(bandShareLabel(0.5)).toBe('50%')
    expect(bandShareLabel(1 / 6)).toBe('17%')
    expect(bandShareLabel(0)).toBe('0%')
    expect(bandShareLabel(null)).toBe('—')
  })
})

describe('bandSliceHorizon', () => {
  it('narrows by shape, so the numeric `claims` key can never read as a block', () => {
    const slice = { claims: 27, '14d': horizon() }
    expect(bandSliceHorizon(slice, '14d')).toMatchObject({ scored: 10 })
    expect(bandSliceHorizon(slice, 'claims')).toBeNull()
  })

  it('a horizon the slice never carried reads null, not a zeroed block', () => {
    expect(bandSliceHorizon({ claims: 3, '14d': horizon() }, '28d')).toBeNull()
  })
})

describe('bandSliceRows', () => {
  it('most claims first, ties by key', () => {
    const rows = bandSliceRows({
      improvement: { claims: 21, '14d': horizon() },
      deterioration: { claims: 27, '14d': horizon() },
      other: { claims: 21, '14d': horizon() },
    })
    expect(rows.map((r) => r.key)).toEqual(['deterioration', 'improvement', 'other'])
    expect(rows[0].claims).toBe(27)
  })

  it('an empty map yields no rows — the panel renders that as an absence', () => {
    expect(bandSliceRows({})).toEqual([])
  })
})

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

// --------------------------------------------------------------------------
// M-1 — the OPERATOR correctness axis on the dimension badge.
//
// A different measurement from the source-overlap `correctness` above: a human
// judged whether the finding was RIGHT, independent of the machine judge. The
// two never merge into one number, and the operator figure never appears
// without its n.
// --------------------------------------------------------------------------
describe('operatorSegment', () => {
  it('carries the n and marks a sub-floor reading indicative', () => {
    expect(
      operatorSegment(ev({ correctness_operator: 0.5, n_operator_scored: 2, operator_sufficient: false })),
    ).toBe('operator 0.50 (n=2, indicative)')
  })
  it('drops the qualifier once the server says the sample is sufficient', () => {
    expect(
      operatorSegment(ev({ correctness_operator: 0.62, n_operator_scored: 24, operator_sufficient: true })),
    ).toBe('operator 0.62 (n=24)')
  })
  it('renders nothing without verdicts — absence is absent, not zero', () => {
    expect(operatorSegment(ev())).toBe('')
    expect(operatorSegment(ev({ correctness_operator: 0.5, n_operator_scored: 0 }))).toBe('')
    expect(operatorSegment(ev({ correctness_operator: null, n_operator_scored: 3 }))).toBe('')
    expect(operatorSegment(null)).toBe('')
  })
  it('a real 0.0 is a verdict, not an absence', () => {
    expect(
      operatorSegment(ev({ correctness_operator: 0, n_operator_scored: 1, operator_sufficient: false })),
    ).toBe('operator 0.00 (n=1, indicative)')
  })
})

describe('evalBadge with the operator axis', () => {
  it('appends the operator segment with its OWN n, never merged', () => {
    expect(
      evalBadge(ev({ correctness_operator: 0.5, n_operator_scored: 2, operator_sufficient: false })),
    ).toBe('faithfulness 0.88 | correctness 0.71 (n=12) | operator 0.50 (n=2, indicative)')
  })
  it('operator verdicts alone leave the unmeasured state', () => {
    expect(
      evalBadge(
        ev({
          faithfulness: null,
          correctness_vs_reference: null,
          correctness_operator: 1,
          n_operator_scored: 1,
        }),
      ),
    ).toBe('operator 1.00 (n=1, indicative)')
  })
  it('still unmeasured when no axis has a number', () => {
    expect(
      evalBadge(ev({ faithfulness: null, correctness_vs_reference: null, correctness_operator: null })),
    ).toBe('unmeasured')
  })
})

describe('correctnessLabel', () => {
  const row = (over: Partial<UnitCorrectnessRow> = {}): UnitCorrectnessRow => ({
    unit: 'escalation',
    correctness: 1,
    n_labels: 1,
    n_scored: 1,
    n_unresolvable: 0,
    mix: { correct: 1 },
    sufficient: false,
    min_labels: 10,
    status: 'indicative only — n=1 scored verdict, below the 10 floor',
    display: 'correctness 1.00 (n=1 scored: 1 correct / 0 partial / 0 incorrect) — indicative only — n=1 scored verdict, below the 10 floor',
    correctness_vs_reference: null,
    n_reference_labels: 0,
    reference_status: 'no gold labels',
    faithfulness: 0.92,
    judge_pipeline_version: '2026-08-03/1',
    ...over,
  })

  it('renders the SERVER-composed display verbatim (mix + status included)', () => {
    // The UI must never recompose a ratio out of its evidence — the honesty
    // contract lives server-side, in one place.
    expect(correctnessLabel(row())).toContain('n=1 scored')
    expect(correctnessLabel(row())).toContain('1 correct')
    expect(correctnessLabel(row())).toContain('indicative only')
  })
  it('unmeasured for a missing row or an empty display', () => {
    expect(correctnessLabel(null)).toBe('unmeasured')
    expect(correctnessLabel(undefined)).toBe('unmeasured')
    expect(correctnessLabel(row({ display: '' }))).toBe('unmeasured')
  })
})

// ── W-6 · standing external truth ────────────────────────────────────────────

describe('externalTruthLabel / fmtRate / orderedStrata', () => {
  const pop = (over: Partial<ExternalTruthPopulation> = {}): ExternalTruthPopulation => ({
    population: 'assembly_span',
    label: 'the record (desk sentences, quoted)',
    display: 'the record (desk sentences, quoted) 0.71 (n=1,148 decided of 3,290 searched; decided rate 0.35)',
    accuracy: 0.7125,
    supported: 818,
    contradicted: 330,
    not_found: 2142,
    n_decided: 1148,
    n_searched: 3290,
    n_unchecked: 1799,
    n_uncheckable: 343,
    decided_rate: 0.349,
    sufficient: true,
    min_decided: 10,
    strata: { regime: { assembly: {} as never }, retrieval_origin: {} },
    instrument: { overlap_raw: 0.79, overlap_n: 329, band: 'contingent', instrument_limited: false },
    search: {},
    sampled: {},
    ...over,
  })

  it('renders the SERVER-composed display verbatim — the n travels with the rate', () => {
    // The tiny-n contract lives in ONE place, server-side. A UI that recomposes
    // a ratio out of its evidence is a second implementation of the honesty rule.
    expect(externalTruthLabel(pop())).toContain('n=1,148 decided')
    expect(externalTruthLabel(pop())).toContain('decided rate 0.35')
  })

  it('WITHHOLDS the number when the graders disagreed — a sentence, not a flag', () => {
    // R3's stop rule, made continuous: a well-powered rate from two raters who
    // disagree is not a better number, it is a better-disguised one.
    const limited = externalTruthLabel(
      pop({ instrument: { overlap_raw: 0.7451, overlap_n: 329, band: 'instrument_limited', instrument_limited: true } }),
    )
    expect(limited).toContain('did not agree with each other often enough')
    expect(limited).toContain('overlap 0.75')
    expect(limited).not.toContain('0.71')
  })

  it('names the fraction when the day was sampled', () => {
    expect(externalTruthLabel(pop({ sampled: { sample_fraction: 0.4 } }))).toContain(
      '40% sample',
    )
  })

  it('says so when nothing has been graded', () => {
    expect(externalTruthLabel(null)).toBe('not graded against the world yet')
  })

  it('fmtRate never renders a missing number as 0.00', () => {
    expect(fmtRate(null)).toBe('—')
    expect(fmtRate(undefined)).toBe('—')
    expect(fmtRate(0)).toBe('0.00')
    expect(fmtRate(0.7125, 4)).toBe('0.7125')
  })

  it('orderedStrata keeps every named stratum, empty ones included', () => {
    // retrieval_origin is NULL on every live signal today. An EMPTY stratum is a
    // measurement ("nothing in it"), not an omission ("not measured here").
    const axes = orderedStrata(pop()).map(([axis]) => axis)
    expect(axes).toEqual([...EXTERNAL_TRUTH_STRATA])
    const origin = orderedStrata(pop()).find(([axis]) => axis === 'retrieval_origin')
    expect(origin?.[1]).toEqual([])
  })
})
