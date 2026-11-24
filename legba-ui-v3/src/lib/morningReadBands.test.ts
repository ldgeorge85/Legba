/**
 * morningReadBands — the judgment surface's four derivations, argued directly.
 *
 * Each case names the rule it holds:
 *
 *   * absence is a state, not a zero                        — rule 2
 *   * the visit is the telemetry's, and survives a reload    — rule 3
 *   * a first-ever visit opens on yesterday 06:00Z           — the brief
 *   * "ordered by what moved most" means severity, not volume
 *   * an unsupported claim carries its sentence AND its source
 *   * a gap is the card's own verdict, never a threshold we invented
 *   * an expired forecast outranks one that closed this morning
 */
import { describe, it, expect, beforeEach } from 'vitest'

import {
  ABSENCE_KIND_ORDER,
  type AbsenceItem,
  type AbsenceResponse,
} from './absenceModel'
import {
  BANDS,
  FALLBACK_READ_HOUR_UTC,
  UNSCOPED_DESK,
  VISIT_KEY,
  bandTestId,
  alertMarkText,
  changedDeskCount,
  changedList,
  changedRankSeverity,
  checkedBand,
  confidenceText,
  dueBand,
  forecastMarkLabel,
  formatElapsed,
  deskGapView,
  gapDeskOrder,
  gapsBand,
  groupByDesk,
  resolveVisit,
  visitCursor,
  visitElapsedMs,
  yesterdayReadingHour,
  type ForecastDue,
  type GapRow,
  type JudgmentFindingRow,
  type JudgmentSince,
} from '@/lib/morningReadBands'
import { __resetReadTelemetry, sessionNonce } from '@/lib/readTelemetry'
import type { CountryScorecard, DimensionBand } from '@/lib/evalOps'
import type { SinceAlert, SinceFinding, SituationChange } from '@/lib/wallModel'

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  __resetReadTelemetry()
})

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const T0 = Date.parse('2026-09-24T09:00:00Z')

function section<T>(items: T[]) {
  return { items, total: items.length, truncated: false }
}

function emptySince(): JudgmentSince {
  return {
    cursor: '2026-09-23T06:00:00Z',
    server_now: '2026-09-24T09:00:00Z',
    counts: {},
    new_findings: section([]),
    superseded: section([]),
    band_changes: section([]),
    situations: section([]),
    alerts: section([]),
  }
}

// THE SHARED FIXTURE (7b-v) — mirrors `substrate_reads_api.FindingJudgmentRow`
// field-for-field. `JUDGMENT_ROW_FIELDS` below pins the exact key set this
// produces; the CHECKED tests exercise the SAME `finding()` for behavior. A
// field the route stops sending (or one this fixture invents that the route
// never carries) fails the key-set test rather than silently drifting.
function finding(over: Partial<JudgmentFindingRow> & { id: string }): JudgmentFindingRow {
  return {
    kind: 'finding',
    title: 'a finding',
    analyst_id: 'security_desk',
    analyst_version: null,
    target_id: 'country_g20_br',
    target_version: null,
    produced_at: '2026-09-24T07:00:00Z',
    severity: 'medium',
    confidence: 0.8,
    schema_uri: 'iglu:legba/finding/jsonschema/1-0-0',
    verification: null,
    citations: [],
    ...over,
  }
}

/** The exact key set a `fields=judgment` row carries — see
 *  `findings_projection.JUDGMENT_FIELDS`'s sibling pin on the Python side. */
const JUDGMENT_ROW_FIELDS = [
  'id', 'kind', 'title', 'analyst_id', 'analyst_version', 'target_id',
  'target_version', 'produced_at', 'severity', 'confidence', 'schema_uri',
  'verification', 'citations',
].sort()

function verifyBlock(spans: Array<{ text: string; reason: string; markers: number[] }>) {
  return {
    faithfulness_score: 0.71,
    judge_status: 'llm',
    checkable_claims: 6,
    supported_claims: 6 - spans.length,
    unsupported_spans: spans,
  }
}

function band(over: Partial<DimensionBand> = {}): DimensionBand {
  return {
    band: 'watch',
    basis: ['b1'],
    severity_tag: 'medium',
    effective_confidence: 0.7,
    confidence: 0.8,
    critic_score: 0.9,
    damped: false,
    reason: '',
    produced_at: '2026-09-24T05:00:00Z',
    eval: {} as DimensionBand['eval'],
    ...over,
  }
}

function card(over: Partial<CountryScorecard> & { target_id: string }): CountryScorecard {
  return {
    id: `card-${over.target_id}`,
    produced_at: '2026-09-24T05:00:00Z',
    generated_at: '2026-09-24T05:00:00Z',
    floors: { faith_floor: 0.5 },
    dimensions: {},
    composition: { present: true, basis: ['x'] },
    ...over,
  }
}

function forecast(over: Partial<ForecastDue> & { id: string; region: string }): ForecastDue {
  return {
    event_class: 'hazard_severe',
    window_start: '2026-09-14T00:00:00Z',
    window_end: '2026-09-21T00:00:00Z',
    p: 0.31,
    p_base: 0.22,
    method: 'recent_rate_poisson',
    method_version: 'forecast_acute/2026-09.1',
    scale_version: 'acute_probability/2026-07',
    resolution_test: 'class=hazard_severe; window=7d weekly; resolver grace=1d',
    resolved_by: null,
    issued_at: '2026-09-14T00:00:00Z',
    days_overdue: 3,
    mark: 'awaiting',
    ...over,
  }
}

// ---------------------------------------------------------------------------
// The visit clock (rule 3)
// ---------------------------------------------------------------------------

describe('the visit clock', () => {
  it('opens a first-ever visit on YESTERDAY 06:00Z, not a rolling 24h', () => {
    const visit = resolveVisit(T0)
    expect(visit.firstVisit).toBe(true)
    expect(visit.previousVisitAt).toBeNull()

    const cursor = visitCursor(visit, T0)
    const at = new Date(cursor)
    expect(at.getUTCHours()).toBe(FALLBACK_READ_HOUR_UTC)
    expect(at.toISOString()).toBe('2026-09-23T06:00:00.000Z')
    expect(yesterdayReadingHour(T0)).toBe(Date.parse('2026-09-23T06:00:00Z'))
  })

  it('is the TELEMETRY session, so a reload continues one morning', () => {
    const first = resolveVisit(T0)
    expect(sessionStorage.getItem('legba_read_session')).toBeTruthy()

    // A reload inside the same tab: same nonce, so the same visit start and
    // the same previous-visit cursor — not a diff reset to "nothing new".
    const again = resolveVisit(T0 + 90_000)
    expect(again.startedAt).toBe(first.startedAt)
    expect(again.firstVisit).toBe(false)
    expect(again.previousVisitAt).toBe(first.previousVisitAt)
  })

  it('rolls the previous visit forward into the cursor when the session turns over', () => {
    resolveVisit(T0)
    // A new tab mints a new nonce; the stored visit becomes the diff boundary.
    __resetReadTelemetry()
    sessionStorage.clear()
    expect(sessionNonce()).toBeTruthy()

    const tomorrow = T0 + 86_400_000
    const next = resolveVisit(tomorrow)
    expect(next.firstVisit).toBe(false)
    expect(next.previousVisitAt).toBe(T0)
    expect(visitCursor(next, tomorrow)).toBe(new Date(T0).toISOString())
  })

  it('clamps a cursor past the route 90-day bound rather than letting it 400', () => {
    localStorage.setItem(
      VISIT_KEY,
      JSON.stringify({ nonce: 'stale', startedAt: T0 - 400 * 86_400_000, previousVisitAt: null }),
    )
    const visit = resolveVisit(T0)
    const cursor = Date.parse(visitCursor(visit, T0))
    expect(T0 - cursor).toBeLessThan(90 * 86_400_000)
  })

  it('measures elapsed from the visit start and never goes negative', () => {
    const visit = resolveVisit(T0)
    expect(visitElapsedMs(visit, T0 + 65_000)).toBe(65_000)
    expect(visitElapsedMs(visit, T0 - 5_000)).toBe(0)
    expect(formatElapsed(65_000)).toBe('1:05')
    expect(formatElapsed(0)).toBe('0:00')
  })
})

// ---------------------------------------------------------------------------
// Band 1 — CHANGED
// ---------------------------------------------------------------------------

describe('CHANGED', () => {
  const f = (over: Partial<SinceFinding> & { id: string }): SinceFinding => ({
    analyst_id: 'security_desk',
    target_id: 'desk_alpha',
    title: 'a finding',
    severity: 'medium',
    confidence: 0.8,
    faithfulness_score: 0.9,
    effective_confidence: 0.8,
    produced_at: '2026-09-24T07:00:00Z',
    ...over,
  })
  const alert = (over: Partial<SinceAlert> & { id: string }): SinceAlert => ({
    severity: 'high',
    channel: 'verified_finding',
    summary: 'an alert',
    target_id: 'desk_alpha',
    produced_at: '2026-09-24T08:00:00Z',
    ...over,
  })
  const sit = (over: Partial<SituationChange> & { id: string }): SituationChange => ({
    name: 'a situation',
    target_id: 'desk_alpha',
    category: 'hazard',
    change: 'escalating',
    from_status: null,
    to_status: 'active',
    status: 'active',
    last_event_at: '2026-09-24T06:00:00Z',
    updated_at: '2026-09-24T06:10:00Z',
    intensity_score: 0.4,
    ...over,
  })

  it('is empty — and honestly so — for a desk with nothing in the window', () => {
    expect(changedList(emptySince())).toEqual([])
    expect(changedList(null)).toEqual([])
    // A response missing the sections entirely (an older server) is empty, not
    // a crash.
    expect(changedList({} as JudgmentSince)).toEqual([])
  })

  it('folds four row families into ONE list ranked by severity, then recency — not grouped by desk', () => {
    const since: JudgmentSince = {
      ...emptySince(),
      new_findings: section([f({ id: 'f1', target_id: 'country_g20_br', title: 'Border incident', severity: 'high' })]),
      alerts: section([
        alert({ id: 'a1', severity: 'critical', channel: 'band_crossing', summary: 'Band deterioration', target_id: 'country_g20_br' }),
      ]),
      situations: section([
        sit({ id: 's1', name: 'Flood cluster', target_id: 'country_g20_in', change: 'appeared' }),
      ]),
      band_changes: section([
        {
          target_id: 'country_g20_in',
          dimension: 'security',
          from_band: 'watch',
          to_band: 'elevated',
          direction: 'deterioration',
          severity: 'high',
          from_scorecard_row_id: 'sc0',
          to_scorecard_row_id: 'sc1',
          changed_at: '2026-09-24T08:30:00Z',
        },
      ]),
    }

    const rows = changedList(since)
    // critical alert, then the two highs most-recent first (the band at 08:30
    // before the finding at 07:00 — across desks), then the unscored situation.
    expect(rows.map((r) => r.id)).toEqual(['a1', 'sc1', 'f1', 's1'])
    expect(rows.map((r) => r.deskId)).toEqual(['country_g20_br', 'country_g20_in', 'country_g20_br', 'country_g20_in'])
    expect(rows.every((r) => !r.deskLabel.includes('_'))).toBe(true)
    // Every row is citable: a substrate kind and a real id.
    expect(rows.every((i) => i.id !== '' && i.rowKind !== '')).toBe(true)
    expect(rows[1].headline).toBe('security: watch → elevated')
    expect(rows[3].note).toBe('situation appeared · now active')
    // A situation carries intensity, not severity — the two scales stay apart.
    expect(rows[3].severity).toBeNull()
    expect(changedDeskCount(rows)).toBe(2)
  })

  it('an alert that points at a listed finding collapses INTO it: one row, the alert as a mark', () => {
    const since: JudgmentSince = {
      ...emptySince(),
      new_findings: section([
        f({ id: 'f1', title: 'Port closed', severity: 'medium' }),
        f({ id: 'f2', title: 'Other thing', severity: 'medium', produced_at: '2026-09-24T07:30:00Z' }),
      ]),
      alerts: section([alert({ id: 'a1', finding_id: 'f1', summary: 'Verified high-severity finding: Port closed' })]),
    }
    const rows = changedList(since)
    expect(rows.map((r) => r.id)).toEqual(['f1', 'f2'])
    expect(rows[0].kind).toBe('finding')
    expect(rows[0].alerts.map((a) => a.id)).toEqual(['a1'])
    expect(rows[1].alerts).toEqual([])
    // The alert's severity raises its item's rank (f1 now outranks the newer f2).
    expect(changedRankSeverity(rows[0])).toBeGreaterThan(changedRankSeverity(rows[1]))
    expect(alertMarkText(rows[0].alerts[0].channel)).toBe('alerted: verified')
  })

  it('without a pointer (an older registry), an alert folds by its own title shape on the same desk only', () => {
    const since: JudgmentSince = {
      ...emptySince(),
      new_findings: section([
        f({ id: 'f1', title: 'Port closed after strike on the quay', target_id: 'desk_alpha' }),
        f({ id: 'f9', title: 'Port closed after strike on the quay', target_id: 'desk_beta' }),
      ]),
      situations: section([sit({ id: 's1', name: 'Quay strikes', from_status: 'active', to_status: 'escalating' })]),
      alerts: section([
        alert({ id: 'a1', summary: 'Verified high-severity finding: Port closed after strike' }),
        alert({ id: 'a2', channel: 'situation_escalation', summary: 'Situation escalated: Quay strikes (active -> escalating)' }),
      ]),
    }
    const rows = changedList(since)
    expect(rows.filter((r) => r.kind === 'alert')).toEqual([])
    expect(rows.find((r) => r.id === 'f1')!.alerts.map((a) => a.id)).toEqual(['a1'])
    expect(rows.find((r) => r.id === 'f9')!.alerts).toEqual([])
    expect(rows.find((r) => r.id === 's1')!.alerts.map((a) => a.id)).toEqual(['a2'])
  })

  it('an alert whose item is not in the list stays a row; identical alerts on one desk are one row', () => {
    const since: JudgmentSince = {
      ...emptySince(),
      alerts: section([
        alert({ id: 'a1', finding_id: 'not-listed', summary: 'Verified high-severity finding: Elsewhere' }),
        alert({ id: 'a2', channel: 'band_crossing', summary: 'Band moved' }),
        alert({ id: 'a3', channel: 'band_crossing', summary: 'Band moved', produced_at: '2026-09-24T08:05:00Z' }),
      ]),
    }
    const rows = changedList(since)
    expect(rows.map((r) => r.kind)).toEqual(['alert', 'alert'])
    const moved = rows.find((r) => r.headline === 'Band moved')!
    expect(moved.alerts).toHaveLength(1)
  })

  it('a transition from a state to the same state is not a change', () => {
    const since: JudgmentSince = {
      ...emptySince(),
      situations: section([
        sit({ id: 's-same', from_status: 'escalating', to_status: 'escalating' }),
        sit({ id: 's-new', from_status: null, to_status: 'active' }),
      ]),
      band_changes: section([
        {
          target_id: 'desk_alpha',
          dimension: 'economy',
          from_band: 'watch',
          to_band: 'watch',
          direction: 'indeterminate',
          severity: 'low',
          from_scorecard_row_id: 'b0',
          to_scorecard_row_id: 'b1',
          changed_at: '2026-09-24T05:00:00Z',
        },
      ]),
      alerts: section([
        // By the route's fields …
        alert({ id: 'a-fields', channel: 'situation_escalation', summary: 'x', state_from: 'escalating', state_to: 'escalating' }),
        // … and by the title alone (an older registry).
        alert({ id: 'a-title', channel: 'situation_escalation', summary: 'Situation escalated: Something (escalating -> escalating)' }),
      ]),
    }
    const rows = changedList(since)
    expect(rows.map((r) => r.id)).toEqual(['s-new'])
  })

  it('buckets a row with no desk rather than dropping it', () => {
    const since: JudgmentSince = {
      ...emptySince(),
      new_findings: section([f({ id: 'f9', analyst_id: 'world_assessor', target_id: null, title: 'World read', severity: 'low' })]),
    }
    const rows = changedList(since)
    expect(rows).toHaveLength(1)
    expect(rows[0].deskId).toBe(UNSCOPED_DESK)
    expect(rows[0].deskLabel).toContain('No desk')
  })
})

describe('CHANGED — W11-F73 guard', () => {
  it('renders `—` for a since-finding without effective_confidence, and never throws', () => {
    const row = {
      id: 'f-no-ec',
      analyst_id: 'escalation',
      target_id: 'country_g20_jp',
      title: 'Escalation read',
      severity: 'elevated',
      confidence: 0.87,
      faithfulness_score: 0.9,
      produced_at: '2026-09-24T07:00:00Z',
    } as unknown as JudgmentSince['new_findings']['items'][number]
    const nan = { ...row, id: 'f-nan', effective_confidence: Number.NaN }
    const rows = changedList({ ...emptySince(), new_findings: section([row, nan]) })
    const notes = rows.map((i) => i.note)
    expect(notes).toEqual([
      'Escalation · effective confidence —',
      'Escalation · effective confidence —',
    ])
    expect(confidenceText(0.5)).toBe('0.50')
    expect(confidenceText(undefined)).toBe('—')
    expect(confidenceText(null)).toBe('—')
  })
})

describe('CHECKED — the registry\'s unverified state (X4 READPATH)', () => {
  const PENDING = { score_state: 'pending', since: '2026-09-24T08:59:00+00:00', judge_window_s: 900, reason: 'awaiting_verify' }
  const ABSENT = { ...PENDING, score_state: 'absent', reason: 'no_verdict' }
  const EXEMPT = { ...ABSENT, reason: 'verify_exempt' }

  it('a state block is never counted as verified, and splits pending from not judged', () => {
    const desks = checkedBand([
      finding({ id: 'v1', verification: verifyBlock([]) }),
      finding({ id: 'p1', title: 'fresh read', verification: PENDING }),
      finding({ id: 'a1', title: 'lost read', verification: ABSENT }),
      finding({ id: 'x1', analyst_id: 'graph_mining', verification: EXEMPT }),
    ])
    expect(desks).toHaveLength(1)
    expect(desks[0]).toMatchObject({ verified: 1, unsupported: 0, unchecked: 3, pending: 1, notJudged: 2 })
    // The exempt producer is counted, not listed — it never enters the pass.
    expect(desks[0].unverified.map((u) => [u.findingId, u.info.state])).toEqual([
      ['p1', 'pending'],
      ['a1', 'absent'],
    ])
  })

  it('a null block from an older registry is `not judged`, never pending and never a pass', () => {
    const [desk] = checkedBand([finding({ id: 'n1', verification: null })])
    expect(desk).toMatchObject({ verified: 0, pending: 0, notJudged: 1, unchecked: 1 })
    expect(desk.unverified[0].info).toMatchObject({ state: 'absent', basis: 'unstated' })
  })
})

describe('CHECKED', () => {
  it('the shared fixture carries exactly the fields fields=judgment returns', () => {
    // The route (`test_findings_judgment_shape`, Python) and this fixture
    // must never drift apart on the key set — this is the TS half of that
    // lockstep.
    expect(Object.keys(finding({ id: 'x' })).sort()).toEqual(JUDGMENT_ROW_FIELDS)
  })

  it('returns nothing for an empty day, and never guesses a shape', () => {
    expect(checkedBand([])).toEqual([])
    expect(checkedBand(null)).toEqual([])
    expect(checkedBand(undefined)).toEqual([])
  })

  it('separates verified / flagged / UNCHECKED — absence is not a pass', () => {
    const desks = checkedBand([
      finding({ id: 'v1', verification: verifyBlock([]) }),
      finding({ id: 'u1', verification: null }),
      finding({ id: 'u2' }),
    ])
    expect(desks).toHaveLength(1)
    expect(desks[0]).toMatchObject({ verified: 1, unsupported: 0, unchecked: 2 })
    expect(desks[0].claims).toEqual([])
  })

  it('lists a flagged claim with its SENTENCE and its SOURCE', () => {
    const desks = checkedBand([
      finding({
        id: 'f-bad',
        title: 'Security read for Brazil',
        analyst_id: 'security_desk',
        verification: verifyBlock([
          { text: 'Troop numbers doubled overnight.', reason: 'no_citation', markers: [] },
          { text: 'The port remains closed.', reason: 'judge_contradicted', markers: [3, 4] },
        ]),
      }),
      finding({ id: 'f-ok', verification: verifyBlock([]) }),
    ])

    const desk = desks[0]
    expect(desk).toMatchObject({ verified: 1, unsupported: 1, unchecked: 0 })
    expect(desk.claims).toHaveLength(2)

    const [first, second] = desk.claims
    expect(first.text).toBe('Troop numbers doubled overnight.')
    expect(first.reasonLabel).toBe('no citation')
    expect(first.markers).toEqual([])
    // The SOURCE: which finding said it, and who produced that finding.
    expect(first.findingId).toBe('f-bad')
    expect(first.findingTitle).toBe('Security read for Brazil')
    expect(first.analystId).toBe('security_desk')
    expect(second.reasonLabel).toBe('contradicted by the verify judge')
    expect(second.markers).toEqual([3, 4])
  })

  it('ranks the desk that FAILED most first, not the one that produced most', () => {
    const desks = checkedBand([
      // A busy, clean desk.
      ...Array.from({ length: 8 }, (_, i) =>
        finding({ id: `ok${i}`, target_id: 'country_g20_us', verification: verifyBlock([]) }),
      ),
      // A quiet desk with two flagged sentences.
      finding({
        id: 'bad1',
        target_id: 'country_g20_br',
        verification: verifyBlock([
          { text: 'one', reason: 'no_citation', markers: [] },
          { text: 'two', reason: 'judge_unsupported', markers: [1] },
        ]),
      }),
    ])
    expect(desks.map((d) => d.targetId)).toEqual(['country_g20_br', 'country_g20_us'])
    expect(desks[0].claims).toHaveLength(2)
    expect(desks[1].claims).toHaveLength(0)
  })
})

// ---------------------------------------------------------------------------
// Band 3 — GAPS
// ---------------------------------------------------------------------------

describe('GAPS', () => {
  it('reads the card’s own verdict — a banded dimension and an absent composition', () => {
    const groups = gapsBand([
      card({
        target_id: 'country_g20_br',
        dimensions: {
          security: band(),
          economy: band({ band: 'insufficient-evidence', basis: [], reason: 'no-finding' }),
        },
      }),
      card({
        target_id: 'country_g20_in',
        composition: { present: false, basis: [] },
        dimensions: { security: band() },
      }),
      // Fully covered: contributes no group at all.
      card({ target_id: 'country_g20_us', dimensions: { security: band() } }),
    ])

    // The desk with NO CURRENT READ outranks the one missing one dimension.
    expect(groups.map((g) => g.targetId)).toEqual(['country_g20_in', 'country_g20_br'])
    expect(groups[0].items[0]).toMatchObject({
      kind: 'no-current-read',
      dimension: null,
      cardId: 'card-country_g20_in',
    })
    expect(groups[1].items[0]).toMatchObject({
      kind: 'insufficient-evidence',
      dimension: 'economy',
      reason: 'no unit finding yet',
    })
  })

  it('is empty for a covered fleet and for a response that is not a list', () => {
    expect(gapsBand([card({ target_id: 'country_g20_us', dimensions: { a: band() } })])).toEqual([])
    expect(gapsBand(null)).toEqual([])
    // The stub shape a failing/absent route can hand back.
    expect(gapsBand({ data: [] } as unknown as CountryScorecard[])).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// Band 4 — DUE
// ---------------------------------------------------------------------------

describe('DUE', () => {
  it('groups by desk and puts the EXPIRED, longest-overdue desk first', () => {
    const groups = dueBand(
      section([
        forecast({
          id: 'due-expired',
          region: 'country_g20_br',
          days_overdue: 9,
          mark: 'expired',
          resolved_by: 'unresolved:expired',
          resolution_test: 'retro: class=hazard_severe; window=7d weekly; resolver grace=1d',
        }),
        forecast({ id: 'due-awaiting', region: 'country_g20_us', days_overdue: 3 }),
        forecast({ id: 'due-grace', region: 'country_g20_in', days_overdue: 0.1, mark: 'in_grace' }),
      ]),
    )
    expect(groups.map((g) => g.targetId)).toEqual([
      'country_g20_br',
      'country_g20_us',
      'country_g20_in',
    ])
    // The frozen test rides the row verbatim, retro prefix intact.
    expect(groups[0].items[0].resolution_test.startsWith('retro: ')).toBe(true)
    expect(groups[0].items[0].mark).toBe('expired')
  })

  it('renders each mark as a sentence, and an unknown one without inventing meaning', () => {
    expect(forecastMarkLabel('expired')).toContain('could not grade')
    expect(forecastMarkLabel('in_grace')).toContain('resolver has not run')
    expect(forecastMarkLabel('awaiting')).toContain('still unanswered')
    expect(forecastMarkLabel('some_new_mark')).toBe('some new mark')
  })

  it('is empty when the section is absent (a server without it) — never a crash', () => {
    expect(dueBand(undefined)).toEqual([])
    expect(dueBand(null)).toEqual([])
    expect(dueBand(section([]))).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// The rail + the grouping primitive
// ---------------------------------------------------------------------------

describe('the reading order', () => {
  it('names the four bands in the order the brief argues for', () => {
    expect(BANDS.map((b) => b.id)).toEqual(['changed', 'checked', 'gaps', 'due'])
    expect(BANDS.map((b) => bandTestId(b.id))).toEqual([
      'morning-read-changed',
      'morning-read-checked',
      'morning-read-gaps',
      'morning-read-due',
    ])
    // Every band states the question it answers — the rail's own copy.
    expect(BANDS.every((b) => b.question.endsWith('?'))).toBe(true)
  })

  it('breaks weight ties on the LABEL so a refetch cannot move a desk', () => {
    const rows = [
      { t: 'country_g20_us' },
      { t: 'country_g20_br' },
      { t: 'country_g20_in' },
    ]
    const once = groupByDesk(rows, (r) => r.t).map((g) => g.targetId)
    const twice = groupByDesk([...rows].reverse(), (r) => r.t).map((g) => g.targetId)
    expect(once).toEqual(twice)
  })
})

// ---------------------------------------------------------------------------
// GAPS, read off `/v3/absence` (k5b)
// ---------------------------------------------------------------------------

function absItem(over: Partial<AbsenceItem> = {}): AbsenceItem {
  return {
    kind: 'collected_but_silent',
    subject: 'source.example.headlines',
    since: '2026-09-20T10:00:00Z',
    window: null,
    reason: 'this source is healthy and carried nothing for this desk',
    as_of: '2026-09-20T10:00:00Z',
    as_of_basis: "this source's most recent signal in this desk's scope",
    expires_at: '2026-09-20T14:00:00Z',
    review: null,
    stale: true,
    proof: {
      what_was_checked: 'signals whose geo overlaps [XX] in the last 30d',
      checked_at: '2026-09-20T10:00:00Z',
      ref: 'source.example.headlines',
      ref_kind: 'source',
    },
    ...over,
  }
}

function absResp(over: Partial<AbsenceResponse> = {}): AbsenceResponse {
  return {
    version: '2026-09/k5',
    scope: 'country_g20_br',
    read_at: '2026-09-24T09:00:00Z',
    kinds: { collected_but_silent: 'a covering source exists and is healthy…' },
    absences: [],
    not_measured: [],
    ...over,
  }
}

const ABSENCE_DESK = { targetId: 'country_g20_br', label: 'Brazil' }

function gapCard(target: string, dims: Record<string, unknown> = {}): CountryScorecard {
  return {
    id: `card-${target}`,
    target_id: target,
    produced_at: '2026-09-24T06:00:00Z',
    dimensions: dims,
  } as unknown as CountryScorecard
}

describe('the gaps band reads typed absence', () => {
  it('always carries all nine kinds, in the route’s declaration order', () => {
    const view = deskGapView(ABSENCE_DESK, [], absResp(), 'ready', 2)
    expect(view.kinds.map((k) => k.kind)).toEqual([...ABSENCE_KIND_ORDER])
    // 7g-2 added `history_gap` as the eighth, W11-F14 `carried_unsupported` as
    // the ninth. The derivation is kind-agnostic — it drives entirely off
    // ABSENCE_KIND_ORDER — so the only thing this number pins is that the
    // vocabulary and the reader stayed in step.
    expect(view.kinds).toHaveLength(9)
  })

  it('renders an eighth kind the reader has never seen before', () => {
    // The GUARD that matters for a NINTH kind: the band must not need a code
    // change to show one. This drives `history_gap` — added to the order in
    // 7g-2 — end to end through the derivation with an item of its own, and
    // asserts the route's own meaning is what gets carried, not a label the
    // reader invented.
    const view = deskGapView(
      ABSENCE_DESK,
      [],
      absResp({
        kinds: { history_gap: 'a curated collection declares this and the table lacks it' },
        absences: [
          absItem({
            kind: 'history_gap',
            subject: 'wb.gdp_growth_annual_pct:US',
            proof: {
              what_was_checked: 'observations rows for this series and subject',
              checked_at: '2026-09-25T20:40:31Z',
              ref: 'd4698548-a54b-445c-ac84-5f7cb632bfc8',
              ref_kind: 'collection_load',
            },
          }),
        ],
      }),
      'ready',
      2,
    )
    const group = view.kinds.find((k) => k.kind === 'history_gap')
    expect(group).toBeDefined()
    expect(group?.items).toHaveLength(1)
    expect(group?.meaning).toBe(
      'a curated collection declares this and the table lacks it',
    )
    expect(view.absenceCount).toBe(1)
  })

  it('keeps the route’s three answers apart: items, clear, NOT MEASURED', () => {
    const view = deskGapView(
      ABSENCE_DESK,
      [],
      absResp({
        absences: [absItem()],
        // One entry, two kinds — BOTH must read as not measured, not only the
        // first one named.
        not_measured: [
          'searched_found_nothing / search_failed: external_grades could not be read',
        ],
      }),
      'ready',
      2,
    )
    const state = (k: string) => view.kinds.find((g) => g.kind === k)!
    expect(state('collected_but_silent').state).toBe('absent')
    expect(state('searched_found_nothing').state).toBe('not_measured')
    expect(state('search_failed').state).toBe('not_measured')
    expect(state('search_failed').notMeasured).toContain('could not be read')
    // Read cleanly and found nothing is its OWN answer, never "not measured".
    expect(state('below_floor').state).toBe('clear')
    expect(state('below_floor').notMeasured).toBeNull()
  })

  it('matches a qualified kind inside a compound not_measured entry', () => {
    const view = deskGapView(
      ABSENCE_DESK,
      [],
      absResp({
        not_measured: [
          'collected_but_silent / source_stale (sources): this desk declares no scope.geo',
        ],
      }),
      'ready',
      2,
    )
    const state = (k: string) => view.kinds.find((g) => g.kind === k)!
    expect(state('source_stale').state).toBe('not_measured')
    expect(state('collected_but_silent').state).toBe('not_measured')
  })

  it('carries the route’s own meaning for each kind, never a local label', () => {
    const view = deskGapView(
      ABSENCE_DESK,
      [],
      absResp({ kinds: { below_floor: 'the card refused to band it' } }),
      'ready',
      2,
    )
    expect(view.kinds.find((k) => k.kind === 'below_floor')!.meaning).toBe(
      'the card refused to band it',
    )
  })

  it('caps each kind and states what it is holding back', () => {
    const many = [1, 2, 3, 4, 5].map((n) => absItem({ subject: `source.s${n}` }))
    const group = deskGapView(
      ABSENCE_DESK,
      [],
      absResp({ absences: many }),
      'ready',
      2,
    ).kinds.find((k) => k.kind === 'collected_but_silent')!
    expect(group.items).toHaveLength(2)
    expect(group.count).toBe(5)
    expect(group.heldBack).toBe(3)
  })

  it('never double-counts a unit that is both a typed absence and a card gap', () => {
    const derived: GapRow[] = [
      {
        kind: 'insufficient-evidence',
        dimension: 'energy_security',
        reason: 'no qualifying verified claim',
        cardId: 'card-1',
        producedAt: '2026-09-24T06:00:00Z',
      },
      {
        kind: 'insufficient-evidence',
        dimension: 'military_posture',
        reason: 'no qualifying verified claim',
        cardId: 'card-1',
        producedAt: '2026-09-24T06:00:00Z',
      },
    ]
    const view = deskGapView(
      ABSENCE_DESK,
      derived,
      absResp({
        absences: [absItem({ kind: 'source_stale', subject: 'energy_security' })],
      }),
      'ready',
      2,
    )
    // The typed row owns `energy_security` — it is the one carrying a proof
    // and a clock — so the card row for the same subject is dropped and
    // COUNTED, never silently lost and never printed twice.
    expect(view.derived.map((g) => g.dimension)).toEqual(['military_posture'])
    expect(view.coveredByKind).toBe(1)
  })

  it('keeps a whole-desk card gap, which no kind can name', () => {
    const derived: GapRow[] = [
      {
        kind: 'no-current-read',
        dimension: null,
        reason: 'no current composition on this desk',
        cardId: 'card-1',
        producedAt: '2026-09-24T06:00:00Z',
      },
    ]
    const view = deskGapView(ABSENCE_DESK, derived, absResp(), 'ready', 2)
    expect(view.derived).toHaveLength(1)
    expect(view.coveredByKind).toBe(0)
  })

  it('an empty answer is a real answer, not a missing one', () => {
    const view = deskGapView(ABSENCE_DESK, [], absResp(), 'ready', 2)
    expect(view.absenceCount).toBe(0)
    expect(view.kinds.every((k) => k.state === 'clear')).toBe(true)
    // The read's OWN instant, never the page's clock.
    expect(view.readAt).toBe('2026-09-24T09:00:00Z')
  })

  it('a desk whose read has not answered carries no absence and says so', () => {
    const loading = deskGapView(ABSENCE_DESK, [], undefined, 'loading', 2)
    expect(loading.state).toBe('loading')
    expect(loading.absenceCount).toBe(0)
    expect(loading.readAt).toBeNull()
  })

  it('reads every carded desk, card-gap desks first, then by label', () => {
    const cards = [
      gapCard('country_g20_us'),
      gapCard('country_g20_br'),
      gapCard('country_g20_ar'),
    ]
    const groups = gapsBand([
      gapCard('country_g20_br', {
        energy_security: { band: 'insufficient-evidence', reason: 'below-floor' },
      }),
    ])
    const order = gapDeskOrder(groups, cards).map((d) => d.targetId)
    // The desk with a card gap leads; the rest follow in label order, and no
    // desk is read twice.
    expect(order[0]).toBe('country_g20_br')
    expect(order).toEqual(['country_g20_br', 'country_g20_ar', 'country_g20_us'])
  })

  it('has no desk set when no desk has a card', () => {
    expect(gapDeskOrder([], [])).toEqual([])
    expect(gapDeskOrder([], undefined)).toEqual([])
  })
})
