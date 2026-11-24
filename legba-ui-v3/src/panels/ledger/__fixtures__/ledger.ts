/**
 * Invented Ledger rows for the U-11 tests: fictional questions, fictional
 * numbers, fictional anchors. Nothing here is the owner's data.
 */
import type { LedgerForecast, LedgerForecastsResponse } from '@/lib/ledgerModel'
import type { ScorecardQuestion, ScorecardResponse } from '@/lib/scorecardModel'
import type {
  AnchorsResponse,
  EvalRunsResponse,
  LeafProof,
  QueueEntry,
  QueueResponse,
  ResolutionRecordsResponse,
  SealProofResponse,
} from '@/lib/ledgerProof'

const SHA = (c: string) => c.repeat(64)

export function forecast(i: number, over: Partial<LedgerForecast> = {}): LedgerForecast {
  return {
    id: `0000000${i}-aaaa-4bbb-8ccc-00000000000${i}`,
    slug: `FX${i}-invented`,
    question: `Invented question number ${i}: a fictional ministry publishes a fictional figure.`,
    resolver_kind: 'event_reported',
    resolver_spec: {
      class: 'fictional ceasefire announcement',
      actors: ['AA', 'BB'],
      window: ['2026-03-01', '2026-03-20'],
      confirmation: 'an official statement of either government',
    },
    due_at: '2026-03-20T23:59:59+00:00',
    grace_hours: 72,
    void_rules: 'Neither government says anything by due + grace.',
    condition: null,
    status: 'open',
    sealed_at: '2026-02-27T06:00:00+00:00',
    content_sha256: SHA(String(i % 10)),
    numbers: [
      { issuer_kind: 'operator', issuer_ref: 'owner', p: 0.7, sealed: true },
      { issuer_kind: 'tower', issuer_ref: 'tower', p: null, absent_reason: 'no number was stated in time', sealed: true },
      { issuer_kind: 'frontier_arm', issuer_ref: 'frontier', p: 0.55, sealed: true },
    ],
    resolution: null,
    ...over,
  }
}

export function forecasts(list: LedgerForecast[]): LedgerForecastsResponse {
  return { version: 'test', available: true, forecasts: list }
}

/** A scorecard question; resolved ones carry Brier only when `scored`. */
export function question(i: number, outcome: 'yes' | 'no' | null, p: number, scored: boolean): ScorecardQuestion {
  const f = forecast(i)
  const b = (q: number) => (outcome == null || !scored ? null : (q - (outcome === 'yes' ? 1 : 0)) ** 2)
  return {
    id: f.id,
    slug: f.slug,
    question: f.question,
    status: outcome ? 'resolved' : 'open',
    resolver_kind: 'event_reported',
    due_at: f.due_at,
    sealed_at: f.sealed_at,
    numbers: [
      { issuer_kind: 'operator', issuer_ref: 'owner', p, brier: b(p) },
      { issuer_kind: 'frontier_arm', issuer_ref: 'frontier', p: 0.5, brier: b(0.5) },
    ],
    baselines: [{ name: 'climatological', p: 0.5, brier: b(0.5) }],
    outcome,
    resolution: outcome ? { id: `res-${i}`, outcome, resolved_at: `2026-03-2${i % 10}T12:00:00+00:00` } : null,
    proposal: null,
  }
}

/**
 * A scorecard with `n` resolved questions (alternating yes / no, the owner at
 * 0.8 on the yes ones and 0.6 on the no ones, so some are misses), plus two open.
 * `withheld` mirrors the route: true below five unless forced.
 */
export function scorecard(n: number, opts: { withheld?: boolean; voids?: number } = {}): ScorecardResponse {
  const withheld = opts.withheld ?? n < 5
  const qs: ScorecardQuestion[] = []
  for (let i = 1; i <= n; i++) qs.push(question(i, i % 2 ? 'yes' : 'no', i % 2 ? 0.8 : 0.6, !withheld))
  qs.push(question(n + 1, null, 0.4, false), question(n + 2, null, 0.3, false))
  const ops = qs.filter((q) => q.outcome).map((q) => q.numbers[0].brier ?? 0)
  const mean = ops.length ? ops.reduce((a, b) => a + b, 0) / ops.length : null
  return {
    version: 'test',
    available: true,
    scores_withheld: withheld,
    scores_min_resolved: 5,
    header: {
      n_questions: qs.length,
      n_sealed: qs.length,
      n_open: 2,
      n_resolved: n,
      n_void: opts.voids ?? 0,
      n_overdue: 0,
      min_sample: 5,
      calibration_min_sample: 30,
      skill_claim: !withheld,
      statement: 'invented',
      scores_withheld: withheld,
      scores_min_resolved: 5,
    },
    questions: qs,
    arms: [
      {
        issuer_kind: 'operator',
        n,
        mean_brier: withheld ? null : mean,
        baseline_mean_brier: withheld ? null : 0.25,
        skill_vs_climatology: withheld || mean == null ? null : 1 - mean / 0.25,
        below_min_sample: n < 5,
      },
      { issuer_kind: 'frontier_arm', n: 3, mean_brier: null, baseline_mean_brier: null, skill_vs_climatology: null, below_min_sample: true },
    ],
    calibration: { available: false, min_sample: 30, n, reason: `n too small (${n} < 30)`, bins: [], bins_arm: null },
  }
}

export const ANCHORS: AnchorsResponse = {
  version: 'test',
  anchors: [
    { id: 'a2', day: '2026-02-28', root_hash: SHA('e'), leaf_count: 40, calendar: 'https://cal-a.example.org', status: 'submitted', submitted_at: '2026-02-28 00:10:01.1+00' },
    { id: 'a3', day: '2026-02-28', root_hash: SHA('e'), leaf_count: 40, calendar: 'https://cal-b.example.org', status: 'submitted', submitted_at: '2026-02-28 00:10:02.1+00' },
    { id: 'a1', day: '2026-02-27', root_hash: SHA('d'), leaf_count: 38, calendar: 'https://cal-a.example.org', status: 'submitted', submitted_at: '2026-02-27 00:10:01.1+00' },
  ],
}

export const EVAL_RUNS: EvalRunsResponse = {
  version: 'test',
  available: true,
  runs: [
    {
      id: 'run-invented-1',
      run_key: 'inv-blind-1',
      version: 1,
      status: 'current',
      title: 'Invented blind test, first run',
      question: 'Does answering from the checked record beat answering from the raw feed?',
      as_of: '2026-03-05T00:00:00Z',
      published_at: '2026-03-20T00:00:00Z',
      design: { preregistered_at: '2026-03-01T00:00:00Z', prereg_sha256: SHA('1'), version: '1.0' },
      question_count: 36,
      question_classes: [
        { class: 'ALPHA', n: 18 },
        { class: 'BETA', n: 18 },
      ],
      grader: 'invented-grader',
      temperature: 1,
      gold_versions: ['gold v1'],
      arms: [
        { id: 'A', label: 'Answers from the raw feed' },
        { id: 'B', label: 'Answers from the checked record' },
      ],
      results: [
        {
          arm: 'A', metric: 'correct', metric_label: 'Share correct', score: 0.41, ci_low: 0.3, ci_high: 0.52, ci_level: 0.95, n: 36,
          by_class: [{ class: 'ALPHA', score: 0.5, n: 18 }, { class: 'BETA', score: 0.32, n: 18 }],
        },
        {
          arm: 'B', metric: 'correct', metric_label: 'Share correct', score: 0.38, ci_low: 0.27, ci_high: 0.49, ci_level: 0.95, n: 36,
          by_class: [{ class: 'ALPHA', score: 0.4, n: 18 }, { class: 'BETA', score: 0.36, n: 18 }],
        },
      ],
      caveats: ['The gold answers were drafted by a model and signed by one person.'],
      deviations: ['Two questions were voided after the run because their gold source could not be met.'],
      content_sha256: SHA('2'),
    },
  ],
}

/** A corrected run (version 2 over version 1) and a withdrawn run — every one invented. */
export const EVAL_RUNS_VERSIONED: EvalRunsResponse = {
  version: 'test',
  available: true,
  runs: [
    {
      ...EVAL_RUNS.runs[0],
      id: 'run-invented-1-v2',
      version: 2,
      status: 'current',
      supersedes: 'run-invented-1',
      correction_reason: 'arm B was transcribed from the wrong column',
      published_at: '2026-03-22T00:00:00Z',
      results: EVAL_RUNS.runs[0].results.map((r) => (r.arm === 'B' ? { ...r, score: 0.44, ci_low: 0.33, ci_high: 0.55 } : r)),
    },
    { ...EVAL_RUNS.runs[0], status: 'superseded', superseded_by: 'run-invented-1-v2' },
    {
      ...EVAL_RUNS.runs[0],
      id: 'run-invented-2',
      run_key: 'inv-blind-0',
      title: 'Invented practice run',
      as_of: '2026-02-01T00:00:00Z',
      status: 'withdrawn',
      withdrawn_at: '2026-03-10T00:00:00Z',
      withdrawn_reason: 'its gold set was found to be the practice set',
      arms: [
        { id: 'A', label: 'Answers from the raw feed', sample_of: 'A', sample_index: 1 },
        { id: 'A-2', label: 'Answers from the raw feed, again', sample_of: 'A', sample_index: 2 },
      ],
      results: [
        { arm: 'A', metric: 'correct', metric_label: 'Share correct', score: 0.3, n: 10 },
        { arm: 'A-2', metric: 'correct', metric_label: 'Share correct', score: 0.25, n: 10 },
      ],
    },
  ],
}

export function records(qid: string): ResolutionRecordsResponse {
  return {
    version: 'test',
    available: true,
    question_id: qid,
    confirm_route: null,
    records: [
      {
        id: 'rec-invented-1',
        question_id: qid,
        record_kind: 'proposal',
        outcome: 'yes',
        resolver_kind: 'event_reported',
        resolver_version: 'rv-test',
        as_of: '2026-03-23T06:00:00+00:00',
        recorded_at: '2026-03-23T06:00:00+00:00',
        actor: 'resolver:test',
        evidence: [
          {
            ref: 'signal:invented-1',
            title: 'Invented wire report: both sides announce a ceasefire',
            publisher: 'example.org',
            url: 'https://example.org/r/1',
            published_at: '2026-03-12T09:00:00+00:00',
            clause: 'event',
            span: 'announced a ceasefire',
          },
        ],
        evidence_total: 1,
        clauses: [{ id: 'main:event', text: 'a fictional ceasefire announcement by AA and BB', status: 'met' }],
        judgements: [],
        searched: { corpus: 'signals', rows_matched: 12 },
        void_rule: null,
        reason: null,
        content_sha256: SHA('f'),
      },
    ],
  }
}

type Route = { status?: number; body?: unknown } | 'pending'

/** A fetch stub answering by route prefix (the first matching key wins). */
export function stubFetch(routes: Record<string, Route>) {
  const calls: string[] = []
  const fn = (url: string) => {
    const u = String(url)
    calls.push(u)
    const key = Object.keys(routes).find((k) => u.includes(k))
    const r = key ? routes[key] : { status: 404, body: { detail: 'Not Found' } }
    if (r === 'pending') return new Promise(() => {})
    const status = r.status ?? 200
    return Promise.resolve({
      ok: status < 400,
      status,
      json: async () => r.body,
      text: async () => JSON.stringify(r.body ?? {}),
      headers: new Headers({ 'content-type': 'application/json' }),
      clone() {
        return this
      },
    } as unknown as Response)
  }
  return { fn, calls }
}

// ---------------------------------------------------------------------------
// F-1 — invented proofs and an invented queue.
// ---------------------------------------------------------------------------

/**
 * An invented seven-leaf anchor tree (four chain heads, one number, the
 * question of `forecast(1)`, the audit head) and the proofs of the question
 * and its number, COMPUTED BY THE SERVER'S OWN MODULE
 * (`legba.data.anchor_proof`), so the browser's fold is checked against the
 * server's. The root is listed for 2026-03-02 in {@link PROOF_ANCHORS}.
 */
export const PROOF_ROOT = '509b3dc83cf22e3a9eab30839f34c7bd7ed99f244c17fcf2e29dd6a343af74e0'
export const QUESTION_PROOF: LeafProof = {
  kind: 'forecast_question',
  id: '00000001-aaaa-4bbb-8ccc-000000000001',
  content_sha256: SHA('1'),
  leaf_input: `forecast_question:00000001-aaaa-4bbb-8ccc-000000000001${SHA('1')}`,
  included: true,
  leaf: '8a0c3a21ad76a0828b2a66d1fb9741c915a48725a2ab679d32f25f096ae81abc',
  day: '2026-03-02',
  position: 5,
  leaf_count: 7,
  path: [
    { side: 'left', hash: '7fb24fabb71cf3e27d761438d5335f07dc5d10cfa85fdff9068189a4fa29606e' },
    { side: 'right', hash: '5c2e00d6001b67d8ee2d79c9915d69c7dc137af984c2e971139336f44033e496' },
    { side: 'left', hash: '3e7bbf379c28c0627b381f36cf6cae3dafd6e4820cd66d658517718edacbd19f' },
  ],
  root: PROOF_ROOT,
  anchor_version: '2026-11/f1-leaves',
}
export const NUMBER_PROOF: LeafProof = {
  kind: 'forecast_number',
  id: '00000001-aaaa-4bbb-8ccc-0000000000aa',
  content_sha256: SHA('2'),
  leaf_input: `forecast_number:00000001-aaaa-4bbb-8ccc-0000000000aa${SHA('2')}`,
  included: true,
  leaf: '7fb24fabb71cf3e27d761438d5335f07dc5d10cfa85fdff9068189a4fa29606e',
  day: '2026-03-02',
  position: 4,
  leaf_count: 7,
  path: [
    { side: 'right', hash: '8a0c3a21ad76a0828b2a66d1fb9741c915a48725a2ab679d32f25f096ae81abc' },
    { side: 'right', hash: '5c2e00d6001b67d8ee2d79c9915d69c7dc137af984c2e971139336f44033e496' },
    { side: 'left', hash: '3e7bbf379c28c0627b381f36cf6cae3dafd6e4820cd66d658517718edacbd19f' },
  ],
  root: PROOF_ROOT,
  anchor_version: '2026-11/f1-leaves',
  issuer_kind: 'operator',
}
export const SEAL_PROOF: SealProofResponse = {
  version: 'test',
  available: true,
  question_id: QUESTION_PROOF.id,
  sealed_at: '2026-02-27T06:00:00+00:00',
  question: QUESTION_PROOF,
  numbers: [NUMBER_PROOF],
  first_stored_day: null,
}
export const PROOF_ANCHORS: AnchorsResponse = {
  version: 'test',
  anchors: [
    { id: 'p1', day: '2026-03-02', root_hash: PROOF_ROOT, leaf_count: 7, calendar: 'https://cal-a.example.org', status: 'submitted', submitted_at: '2026-03-02 00:10:01.1+00' },
    { id: 'p2', day: '2026-03-02', root_hash: PROOF_ROOT, leaf_count: 7, calendar: 'https://cal-b.example.org', status: 'pending', submitted_at: null },
    ...ANCHORS.anchors,
  ],
}

/** One queue entry for `forecast(i)`, the proposal reading `outcome`. */
export function queueEntry(i: number, outcome: string, over: Partial<QueueEntry> = {}): QueueEntry {
  const f = forecast(i)
  const closing = ['yes', 'no', 'void'].includes(outcome)
  const rec = records(f.id).records[0]
  return {
    question_id: f.id,
    slug: f.slug,
    question: f.question,
    resolver_kind: f.resolver_kind,
    resolver_spec: f.resolver_spec as Record<string, unknown>,
    due_at: f.due_at,
    grace_hours: 72,
    condition: null,
    void_rules: f.void_rules,
    proposal: {
      record_id: `rec-invented-${i}`,
      outcome,
      recorded_at: '2026-03-23T06:00:00+00:00',
      evidence: rec.evidence,
      evidence_total: 1,
      clauses: rec.clauses,
      judgements: outcome === 'needs_judgement' ? [{ key: 'attribution', question: 'Is the announcement attributed to both governments?' }] : [],
      void_rule: null,
    },
    may: { confirm: closing, reject: closing || outcome === 'needs_judgement', judgement: outcome === 'needs_judgement' },
    ...over,
  }
}

export function queue(items: QueueEntry[], over: Partial<QueueResponse> = {}): QueueResponse {
  return { version: 'test', available: true, role: 'admin', may_correct: true, items, hidden: 0, hidden_reason: null, ...over }
}
