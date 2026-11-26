/**
 * ledgerProof — the Ledger page's read model (U-11). The owner's two rules
 * are tested here first: no score below five resolutions (including the
 * 4 → 5 edge, and either side of the gate saying "not yet"), and every
 * resolved forecast listed with its outcome, misses as plainly as hits.
 * Invented rows only (`panels/ledger/__fixtures__/ledger.ts`).
 */
import { describe, expect, it } from 'vitest'
import {
  SCORE_FLOOR,
  armScores,
  armWords,
  calibration,
  evalRecordState,
  evalResultRows,
  evalRunFacts,
  evalStanding,
  evidenceLine,
  forecastState,
  orderEvalRuns,
  parseTs,
  questionLeafInput,
  resolutionQueue,
  resolutionRule,
  resolvedRows,
  sealCheck,
  statedNumbers,
  trackRecordGate,
} from './ledgerProof'
import { ANCHORS, EVAL_RUNS, EVAL_RUNS_VERSIONED, forecast, scorecard } from '@/panels/ledger/__fixtures__/ledger'

describe('the track record gate — no score below five', () => {
  it('the floor is five', () => {
    expect(SCORE_FLOOR).toBe(5)
  })

  for (const n of [0, 1, 2, 3, 4]) {
    it(`${n} resolved: withheld, the count toward five, nothing scored`, () => {
      const sc = scorecard(n)
      const g = trackRecordGate(sc)
      expect(g.state).toBe('withheld')
      expect(g.resolved).toBe(n)
      expect(g.remaining).toBe(5 - n)
      expect(g.line).toBe(`${n} of the 5 resolutions needed before any score is shown.`)
      expect(armScores(sc, g)).toEqual([])
      expect(calibration(sc, g)).toBeNull()
      expect(resolvedRows(sc, g)).toEqual([])
    })
  }

  it('4 → 5: the gate opens exactly at five', () => {
    expect(trackRecordGate(scorecard(4)).state).toBe('withheld')
    const five = trackRecordGate(scorecard(5))
    expect(five.state).toBe('scored')
    expect(five.line).toBe('Scored over 5 resolved forecasts.')
  })

  it('a route that drops its own floor cannot open the gate below five', () => {
    const sc = scorecard(4, { withheld: false })
    expect(sc.scores_withheld).toBe(false)
    const g = trackRecordGate(sc)
    expect(g.state).toBe('withheld')
    expect(armScores(sc, g)).toEqual([])
    expect(resolvedRows(sc, g)).toEqual([])
  })

  it('a route still withholding at five keeps the gate shut, and says so', () => {
    const g = trackRecordGate(scorecard(6, { withheld: true }))
    expect(g.state).toBe('withheld')
    expect(g.line).toMatch(/scores are not published yet/)
  })

  it('a route that RAISES its floor is obeyed', () => {
    const sc = { ...scorecard(6), scores_min_resolved: 8 }
    const g = trackRecordGate(sc)
    expect(g.floor).toBe(8)
    expect(g.state).toBe('withheld')
    expect(g.line).toBe('6 of the 8 resolutions needed before any score is shown.')
  })

  it('reading, not deployed, unreadable and failed each say so and score nothing', () => {
    expect(trackRecordGate(undefined).state).toBe('reading')
    expect(trackRecordGate(null).state).toBe('not_deployed')
    expect(trackRecordGate({ available: false }).state).toBe('unreadable')
    expect(trackRecordGate(scorecard(9), { failed: true }).state).toBe('error')
    for (const g of [trackRecordGate(undefined), trackRecordGate(null), trackRecordGate({ available: false })]) {
      expect(g.resolved).toBeNull()
      expect(g.line).not.toMatch(/\d\.\d{3}/)
    }
  })

  it('voided forecasts are said not to count', () => {
    expect(trackRecordGate(scorecard(5, { voids: 2 })).line).toBe('Scored over 5 resolved forecasts. 2 voided, not counted.')
  })
})

describe('the scored record — losses included', () => {
  const sc = scorecard(6)
  const g = trackRecordGate(sc)

  it('each forecaster against the baseline of always saying 50 %, with n; a small sample says why it has no score', () => {
    const arms = armScores(sc, g)
    const owner = arms.find((a) => a.kind === 'operator')!
    expect(owner.who).toBe('Owner')
    expect(owner.n).toBe(6)
    expect(owner.brier).toMatch(/^0\.\d{3}$/)
    expect(owner.baseline).toBe('0.250')
    expect(owner.versus).toMatch(/than always saying 50 %$/)
    const frontier = arms.find((a) => a.kind === 'frontier_arm')!
    expect(frontier.brier).toBe('no score: 3 resolved from this forecaster (fewer than 5)')
  })

  it('every resolved forecast is listed — the misses as rows like the hits, newest first', () => {
    const rows = resolvedRows(sc, g)
    expect(rows).toHaveLength(6)
    const kinds = rows.map((r) => r.verdictKind)
    expect(kinds).toContain('called')
    expect(kinds).toContain('missed')
    const miss = rows.find((r) => r.verdictKind === 'missed')!
    expect(miss.verdict).toBe('missed')
    expect(miss.outcome).toBe('no')
    expect(miss.p).toBe('60 %')
    expect(miss.briers.length).toBeGreaterThan(0)
    expect(rows.map((r) => r.resolvedOn)).toEqual([...rows.map((r) => r.resolvedOn)].sort().reverse())
  })

  it('calibration: ten bands with the count in each; the observed rate only when the route publishes it', () => {
    const cal = calibration(sc, g)!
    expect(cal.who).toBe('Owner')
    expect(cal.rows).toHaveLength(10)
    expect(cal.rows.reduce((n, r) => n + r.n, 0)).toBe(6)
    expect(cal.rows.find((r) => r.band === '80–90 %')!.n).toBe(3)
    expect(cal.rows.find((r) => r.band === '60–70 %')!.n).toBe(3)
    expect(cal.rows.every((r) => r.observed === '—')).toBe(true)
    expect(cal.note).toMatch(/published from 30 resolved forecasts; 6 so far/)
    const published = {
      ...sc,
      calibration: {
        available: true,
        min_sample: 30,
        n: 6,
        bins_arm: 'operator',
        bins: [{ lo: 0.8, hi: 0.9, n: 3, mean_p: 0.8, observed: 1 }],
      },
    }
    const c2 = calibration(published, trackRecordGate(published))!
    expect(c2.rows.find((r) => r.band === '80–90 %')!.observed).toBe('100 %')
    expect(c2.note).toBe('')
  })
})

describe('one forecast', () => {
  const now = new Date('2026-03-10T00:00:00Z')

  it('open, past due, awaiting confirmation, needs judgement, resolved, voided', () => {
    expect(forecastState(forecast(1), null, null, now).kind).toBe('open')
    expect(forecastState(forecast(1), null, null, new Date('2026-03-25T00:00:00Z'))).toMatchObject({ kind: 'overdue', tone: 'warning' })
    const prop = { outcome: 'yes', recorded_at: '2026-03-21T06:00:00Z' }
    expect(forecastState(forecast(1), { proposal: prop }, null, now)).toMatchObject({
      kind: 'awaiting_confirmation',
      label: 'Proposed: yes — awaiting confirmation',
    })
    expect(forecastState(forecast(1), { proposal: { ...prop, outcome: 'needs_judgement' } }, null, now).kind).toBe('needs_judgement')
    // An act after the proposal: no longer awaiting.
    expect(forecastState(forecast(1), { proposal: prop, latest_act: { outcome: 'yes', recorded_at: '2026-03-21T07:00:00Z' } }, null, now).kind).toBe('open')
    const yes = forecastState(forecast(1, { status: 'resolved', resolution: { id: 'r', outcome: 'yes', resolved_at: '2026-03-21T00:00:00Z' } }))
    expect(yes).toMatchObject({ kind: 'resolved_yes', label: 'Resolved: yes', tone: 'neutral' })
    const no = forecastState(forecast(1, { status: 'resolved', resolution: { id: 'r', outcome: 'no' } }))
    expect(no).toMatchObject({ kind: 'resolved_no', tone: 'neutral' })
    const v = forecastState(forecast(1, { status: 'void', resolution: { id: 'r', outcome: 'void', note: 'the vote was postponed' } }))
    expect(v).toMatchObject({ kind: 'void', detail: 'Voided: the vote was postponed' })
    expect(forecastState(forecast(1, { status: 'void' }), null, 'no report by due + grace').detail).toBe('Voided: no report by due + grace')
    expect(forecastState(forecast(1, { status: 'void' })).detail).toBe('Voided: no reason was recorded with it')
  })

  it('every forecaster’s number in reader words, an absence with its reason', () => {
    const ns = statedNumbers(forecast(1))
    expect(ns.map((n) => n.who)).toEqual(['Owner', 'This system', 'Frontier model'])
    expect(ns[0].text).toBe('70 %')
    expect(ns[1]).toMatchObject({ p: null, text: 'no number', reason: 'no number was stated in time' })
  })

  it('the resolution rule in plain words, per kind; an unknown kind falls back honestly', () => {
    const r = resolutionRule(forecast(1))
    expect(r.rule).toBe('Yes if a fictional ceasefire announcement is reported (by AA and BB) between 2026-03-01 and 2026-03-20.')
    expect(r.confirmation).toBe('an official statement of either government')
    expect(r.voidRule).toBe('Neither government says anything by due + grace.')
    expect(r.timing).toBe('Due 2026-03-20, with 72 hours of grace for the confirming report.')
    expect(
      resolutionRule({ ...forecast(2), resolver_kind: 'series_threshold', resolver_spec: { source: 'XS', series: 'IDX', op: '>=', value: 100, on: '2026-12-31' } }).rule,
    ).toBe('Yes if XS IDX is at or above 100 on 2026-12-31. Settled by the published value itself.')
    expect(
      resolutionRule({ ...forecast(3), resolver_kind: 'official_result', resolver_spec: { race: 'Mayor of Example', outcome: 'the incumbent re-elected', authority: 'the election office' } }).rule,
    ).toBe('Yes if the result is: the incumbent re-elected (Mayor of Example), as published by the election office.')
    expect(
      resolutionRule({
        ...forecast(4),
        resolver_kind: 'official_document',
        resolver_spec: { issuers: ['AA', 'BB'], doc_type: 'joint halt', in_effect_on: '2026-04-01', also: [{ kind: 'event_reported', spec: { class: 'strike', window: ['2026-03-25', '2026-04-01'] }, negate: true }] },
      }).rule,
    ).toBe('Yes if AA and BB issue a joint halt, in effect on 2026-04-01, and no strike is reported between 2026-03-25 and 2026-04-01.')
    expect(resolutionRule({ ...forecast(5), resolver_kind: 'event_x1', resolver_spec: {} }).rule).toBe(
      'Resolved by the rule sealed with the question, as the question states it.',
    )
  })
})

describe('the seal', () => {
  it('covered by the first daily anchor after the seal — and never "verified"', () => {
    const c = sealCheck('2026-02-27T06:00:00+00:00', ANCHORS.anchors)
    expect(c.state).toBe('anchored')
    expect(c.anchor).toMatchObject({ day: '2026-02-28', calendars: 2, leafCount: 40 })
    expect(c.text).toMatch(/^Covered by the daily anchor of 2026-02-28, submitted to two public timestamp calendars\./)
    expect(c.text).toMatch(/The timestamp itself is not checked in the console/)
    expect(c.text.toLowerCase()).not.toMatch(/\bverified\b(?! in the console)/)
  })
  it('sealed after the newest anchor: not covered yet', () => {
    expect(sealCheck('2026-02-28T06:00:00Z', ANCHORS.anchors).state).toBe('not_covered')
  })
  it('an anchor that has not reached a calendar is pending', () => {
    const pend = [{ ...ANCHORS.anchors[0], status: 'pending' }]
    expect(sealCheck('2026-02-27T06:00:00Z', pend).state).toBe('pending')
  })
  it('unsealed, reading and unreadable are three different answers', () => {
    expect(sealCheck(null, ANCHORS.anchors).state).toBe('unsealed')
    expect(sealCheck('2026-02-27T06:00:00Z', undefined).state).toBe('reading')
    expect(sealCheck('2026-02-27T06:00:00Z', null).state).toBe('unreadable')
  })
  it('the leaf recipe is the anchor handler’s', () => {
    expect(questionLeafInput('q1', 'ab')).toBe('forecast_question:q1ab')
  })
  it('reads Postgres text timestamps', () => {
    expect(parseTs('2026-11-14 00:10:01.368939+00')).toBe(Date.parse('2026-11-14T00:10:01.368Z'))
    expect(parseTs('nonsense')).toBeNull()
  })
})

describe('the evaluation record — GET /v3/eval/runs', () => {
  it('an empty list is "none published"; a 404 is "not served"; unreadable and failed differ', () => {
    expect(evalRecordState(undefined)).toBe('reading')
    expect(evalRecordState({ available: true, runs: [] })).toBe('none_published')
    expect(evalRecordState(null)).toBe('not_served')
    expect(evalRecordState({ available: false, runs: [] })).toBe('unreadable')
    expect(evalRecordState(null, { failed: true })).toBe('error')
    expect(evalRecordState(EVAL_RUNS)).toBe('published')
  })
  it('each score with its interval and n, arms in plain words, per class', () => {
    const rows = evalResultRows(EVAL_RUNS.runs[0])
    expect(rows[0]).toEqual({
      arm: 'Answers from the raw feed', metric: 'Share correct', score: '0.410', interval: '0.300 to 0.520 (95 %)', n: 'n = 36',
      byClass: ['alpha 0.500 (n = 18)', 'beta 0.320 (n = 18)'],
    })
    expect(evalResultRows({ ...EVAL_RUNS.runs[0], results: [{ arm: 'A', metric: 'm', score: 0.5, n: 3 }] })[0]).toMatchObject({
      interval: 'no interval published', byClass: [],
    })
  })
  it('versions: newest first within a run, runs by newest as-of, the opened run first', () => {
    const runs = EVAL_RUNS_VERSIONED.runs
    expect(orderEvalRuns([...runs].reverse()).map((r) => r.id)).toEqual(['run-invented-1-v2', 'run-invented-1', 'run-invented-2'])
    expect(orderEvalRuns(runs, 'run-invented-2').map((r) => r.id)[0]).toBe('run-invented-2')
  })
  it('standing in words: current, correction, superseded, withdrawn', () => {
    const [v2, v1, gone] = EVAL_RUNS_VERSIONED.runs
    const all = EVAL_RUNS_VERSIONED.runs
    expect(evalStanding(EVAL_RUNS.runs[0], EVAL_RUNS.runs)).toBe('Version 1, as first published.')
    expect(evalStanding(v2, all)).toBe('Version 2, correcting version 1: arm B was transcribed from the wrong column.')
    expect(evalStanding(v1, all)).toBe('Version 1, superseded by version 2 on 2026-03-22. Kept as it was published.')
    expect(evalStanding(gone, all)).toMatch(/^Withdrawn on 2026-03-10: its gold set was found to be the practice set\./)
    expect(evalStanding({ ...gone, withdrawn_reason: null }, all)).toMatch(/no reason was recorded/)
  })
  it('the run’s conditions and the arm run twice, in words', () => {
    expect(evalRunFacts(EVAL_RUNS.runs[0])).toEqual([
      'Answered as of 2026-03-05 · published 2026-03-20',
      '36 questions in two classes (alpha 18, beta 18)',
      'Marked by invented-model · answering temperature 1',
      'Gold answers: gold v1',
      `Design version 1.0, registered 2026-03-01 · hash ${'1'.repeat(12)}…`,
    ])
    const gone = EVAL_RUNS_VERSIONED.runs[2]
    expect(armWords(gone.arms[1], gone.arms, 'A-2')).toBe('Answers from the raw feed, again (sample 2 of “Answers from the raw feed”)')
    expect(armWords(gone.arms[0], gone.arms, 'A')).toBe('Answers from the raw feed (sample 1)')
  })
})

describe('the resolution queue (admin)', () => {
  it('open questions with a proposal and no act after it, closing outcomes first', () => {
    const sc = scorecard(0)
    sc.questions![0].proposal = { outcome: 'needs_judgement', recorded_at: '2026-03-21T00:00:00Z', judgements: [{ key: 'k', question: 'Is it the agreed one?' }] }
    sc.questions![1].proposal = { outcome: 'yes', recorded_at: '2026-03-22T00:00:00Z', evidence_count: 3 }
    const q = resolutionQueue(sc)
    expect(q.map((i) => i.label)).toEqual(['Proposed: yes', 'Needs a judgement'])
    expect(q[0].evidenceCount).toBe(3)
    sc.questions![1].latest_act = { outcome: 'yes', recorded_at: '2026-03-22T01:00:00Z' }
    expect(resolutionQueue(sc).map((i) => i.label)).toEqual(['Needs a judgement'])
    expect(resolutionQueue(null)).toEqual([])
  })
  it('evidence lines from either resolver shape', () => {
    expect(evidenceLine({ ref: 'signal:1', title: 'T', publisher: 'p.org', published_at: '2026-03-12T09:00:00Z', url: 'https://example.org/1', clause: 'event' })).toMatchObject({
      title: 'T',
      where: 'p.org',
      when: '2026-03-12',
      url: 'https://example.org/1',
    })
    expect(evidenceLine({ ref: 'observation:1', series_id: 'xs.idx', value: 101.5, unit: 'usd', valid_from: '2026-12-31', source_url: 'https://example.org/s' })).toMatchObject({
      title: 'xs.idx = 101.5 usd',
      when: '2026-12-31',
      url: 'https://example.org/s',
    })
  })
})
