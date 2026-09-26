/**
 * REAL-MOUNT tests for `analysis.cross_framing` (wave P lane B).
 *
 * `framingModel.test.ts` proves the derivation. The derivation can be right
 * while the panel still renders a bare cell, so these mount the real component
 * over a stubbed HTTP boundary and hold the five things a reader must see:
 *
 *   * every unit on the roster gets a ROW — framing, silence, or a typed
 *     absence in the route's own words. Never a blank cell, which is the
 *     failure the whole surface exists to end.
 *   * a stale absence says "last known absence, not re-checked" ON THE PAGE.
 *   * the six leans render with their priors named, and a lens with no read
 *     says so rather than leaving a gap.
 *   * a desk the divergence run did not resolve says THAT, not "no divergence".
 *   * "ask about this claim" sets the AMBIENT consult scope pin — the same
 *     `SCOPE_PIN_ORIGIN` pin Consult auto-manages — so the existing session
 *     follows the reader here. The pin is what is asserted, not a modal.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import CrossFramingPanel from './CrossFraming'
import { requestCrossFraming } from '@/lib/crossFramingLink'
import { scopePin, SCOPE_PIN_ORIGIN } from '@/lib/consultContext'
import { resetScope, useScope } from '@/state/scope'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'cf1',
    panel_id: 'analysis_cross_framing',
    descriptor_id: '(singleton)',
    descriptor_version: '0'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Cross-framing',
    mode: 'personal',
    layout_slot: 'main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-09-26T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const DESK = 'country_g20_it'
const SIG = '52c57896-9f53-4865-a56e-e36723573696'
const HEAD = 'ab9cd76f-dbce-4b41-80f6-9156f60018a3'
const RECORD = 'b3edd290-f02d-44bc-84f2-3c695c53c508'

function compositionRow() {
  return {
    id: RECORD,
    analyst_id: 'country_composition',
    target_id: DESK,
    produced_at: '2026-09-26T11:34:57.991139Z',
    verification: null,
    data: {
      data: {
        citations: [
          { marker: '[[ref:1]]', ref_id: HEAD, ref_kind: 'finding', source: 'energy_security', ordinal: 1 },
        ],
        assembly: {
          schema: 'assembly.v1',
          regime: 'assembly',
          tier: 'country',
          as_of: '2026-09-26T11:34:57.966415+00:00',
          lead: null,
          tensions: [],
          tension_checked: {
            pairs_examined: 21,
            pairs_found: 0,
            scope: 'shown_only',
            scope_note: 'BLUF-grain; ambivalent pairs declined',
          },
          drops: { counts: {}, no_head: ['proliferation_watch'], why_classes: [] },
          connectives: { vocabulary_version: 'connective.v1' },
          coverage_roster: [
            'energy_security',
            'escalation',
            'military_posture',
            'economic_coercion',
            'proliferation_watch',
          ],
          coverage: [
            { unit: 'energy_security', status: 'in_basis', read_date: '26 Sep 04:01 UTC', age_h: 7.56 },
            { unit: 'escalation', status: 'in_basis', read_date: '26 Sep 04:18 UTC', age_h: 7.27 },
            { unit: 'military_posture', status: 'in_basis', read_date: '26 Sep 04:19 UTC', age_h: 7.26 },
            { unit: 'economic_coercion', status: 'no_head_in_horizon' },
            { unit: 'proliferation_watch', status: 'no_head_in_horizon' },
          ],
          blocks: [
            {
              ordinal: 1,
              finding_id: HEAD,
              desk: 'energy_security',
              target_id: DESK,
              question: 'Energy security',
              produced_at: '2026-09-26T04:01:15.233314+00:00',
              tier: 'basis',
              severity: 'elevated',
              verify: { overall_score: 0.857, checkable_claims: 7, supported_claims: 6, judge_status: 'unavailable', score_state: 'scored' },
              corroboration: { n_sources: 2, n_desks_sharing: 1 },
              spans: [
                {
                  role: 'bluf',
                  text: 'Italy’s energy-security pressure remains elevated [92].',
                  origin: { head_id: HEAD, start: 0, end: 50, body_sha256: 'x', body_len: 100 },
                  markers: ['[92]'],
                  scope_tokens: [],
                },
              ],
              signals: [
                {
                  marker: '[92]',
                  signal_id: SIG,
                  source_id: 'source.ansa',
                  title: 'fuel prices',
                  url: null,
                  age_h: 3,
                  salience_magnitude: 0.55,
                  also_cited_by: [],
                },
              ],
            },
            {
              ordinal: 2,
              finding_id: 'head-escalation',
              desk: 'escalation',
              target_id: DESK,
              question: 'Escalation',
              produced_at: '2026-09-26T04:18:00Z',
              tier: 'basis',
              severity: 'watch',
              verify: { overall_score: 0.73, judge_status: 'llm', score_state: 'scored' },
              corroboration: { n_sources: 3, n_desks_sharing: 2 },
              spans: [
                {
                  role: 'bluf',
                  text: 'The same fuel shock reads as coercive leverage [4].',
                  origin: { head_id: 'head-escalation', start: 0, end: 40, body_sha256: 'y', body_len: 90 },
                  markers: ['[4]'],
                  scope_tokens: [],
                },
              ],
              signals: [
                {
                  marker: '[4]',
                  signal_id: SIG,
                  source_id: 'source.ansa',
                  title: 'fuel prices',
                  url: null,
                  age_h: 3,
                  salience_magnitude: 0.5,
                  also_cited_by: [],
                },
              ],
            },
            {
              ordinal: 3,
              finding_id: 'head-military',
              desk: 'military_posture',
              target_id: DESK,
              question: 'Military posture',
              produced_at: '2026-09-26T04:19:00Z',
              tier: 'basis',
              severity: 'watch',
              verify: { overall_score: 0.5, judge_status: 'llm', score_state: 'scored' },
              corroboration: { n_sources: 1, n_desks_sharing: 1 },
              spans: [
                {
                  role: 'bluf',
                  text: 'Italy commissions a fifth PPA combat ship [35].',
                  origin: { head_id: 'head-military', start: 0, end: 40, body_sha256: 'z', body_len: 80 },
                  markers: ['[35]'],
                  scope_tokens: [],
                },
              ],
              signals: [
                {
                  marker: '[35]',
                  signal_id: 'another-signal',
                  source_id: 'source.difesa',
                  title: 'a ship',
                  url: null,
                  age_h: 9,
                  salience_magnitude: 0.2,
                  also_cited_by: [],
                },
              ],
            },
          ],
        },
      },
    },
  }
}

const ABSENCE = {
  version: 'absence.v1',
  scope: DESK,
  read_at: '2026-09-26T12:00:00Z',
  kinds: {},
  not_measured: ['no loaded collection names this desk'],
  absences: [
    {
      kind: 'source_stale',
      subject: 'economic_coercion',
      since: '2026-09-19T00:00:00Z',
      window: null,
      reason: 'every feed mapped to this unit last published before the window opened',
      as_of: '2026-09-19T00:00:00Z',
      as_of_basis: 'the source-freshness scan for this desk',
      expires_at: '2026-09-20T00:00:00Z',
      review: null,
      stale: true,
      proof: {
        what_was_checked: 'the desk’s registered feed set',
        checked_at: '2026-09-19T00:00:00Z',
        ref: null,
        ref_kind: null,
      },
    },
  ],
}

const LENSES = {
  consolidation: null,
  next_cursor: null,
  calibration: null,
  entries: [
    {
      id: 'lens-entry-1',
      entry_kind: 'lens',
      title: 'Read',
      body: '',
      claims: [
        {
          text_span: 'the fuel shock is leverage, not scarcity',
          kind: 'fact',
          refs: [{ id: SIG, kind: 'signal', title: 'fuel prices' }],
        },
      ],
      cited_substrate_refs: [],
      honesty_flags: ['forecast_unproven'],
      period_start: '2026-09-25T00:00:00Z',
      period_end: '2026-09-26T00:00:00Z',
      produced_at: '2026-09-26T11:01:11Z',
      analyst_id: 'lens_militarist',
      analyst_version: '1',
      verify_score: 0.5833,
      verify_body: null,
    },
  ],
}

/** A receipt that resolved OTHER desks, so this desk is unresolved. */
const DIVERGENCE = {
  measured: true,
  generated_at: '2026-09-26T12:00:00Z',
  reader_version: 'r1',
  unit_sentence: 'Per country, the same stack of source layers is counted per day…',
  as_of: '2026-09-26',
  receipt_run_id: 'run-1',
  run_started_at: '2026-09-26T00:10:00Z',
  method_version: 'layer_divergence/2026-09.1',
  payload_schema: 'layer_divergence.v1',
  classification_audit: 'the source→layer map is un-audited (SEAMS #60).',
  window_days: 28,
  baseline_days: 28,
  z_threshold: 2,
  mad_floor: 0.1,
  consecutive_days: 2,
  thin_min_per_day: 3,
  layer_vocab: [],
  pairs_declared: [],
  desks: [],
  desks_unresolved: [],
  warnings: [],
}

const CONTENTIONS = { version: 'v1', note: '', scope: DESK, stances: [], contentions: [] }

let records: unknown[]

beforeEach(() => {
  resetScope()
  records = [compositionRow()]
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const body = url.includes('/v3/absence')
        ? ABSENCE
        : url.includes('/v3/contentions')
          ? CONTENTIONS
          : url.includes('/v3/layers/divergence')
            ? DIVERGENCE
            : url.includes('/journal')
              ? LENSES
              : { data: records }
      return {
        ok: true,
        status: 200,
        json: async () => body,
        text: async () => JSON.stringify(body),
      } as unknown as Response
    }),
  )
  requestCrossFraming({ targetId: DESK, origin: 'test' })
})

function mount() {
  return render(wrap(<CrossFramingPanel registration={reg()} scope={{}} mode="personal" />))
}

describe('Cross-framing panel', () => {
  it('leads with the claim verbatim, its verdict, and the count sentence', async () => {
    mount()
    expect(await screen.findByTestId('cross-framing-claim')).toHaveTextContent(
      'Italy’s energy-security pressure remains elevated [92].',
    )
    // No verification block on the record — an honest absence, not a failure.
    expect(screen.getByTestId('cross-framing-verdict')).toHaveTextContent(
      'claim-level verdict not recorded',
    )
    expect(screen.getByTestId('cross-framing-counts')).toHaveTextContent(
      'carried by 2 of 5 units · 2 with no read · 1 read this desk but not this claim',
    )
  })

  it('gives every unit on the roster a row — and never a blank one', async () => {
    mount()
    await screen.findByTestId('cross-framing-claim')
    for (const unit of [
      'energy_security',
      'escalation',
      'military_posture',
      'economic_coercion',
      'proliferation_watch',
    ]) {
      expect(screen.getByTestId(`cross-framing-unit-${unit}`)).toBeTruthy()
      const cell = screen.getByTestId(`cross-framing-unit-${unit}`)
      expect(cell.textContent?.trim().length ?? 0).toBeGreaterThan(0)
    }
  })

  it('shows the framing unit’s OWN sentence, and says the silent one is absent, not disagreeing', async () => {
    mount()
    await screen.findByTestId('cross-framing-claim')
    expect(screen.getByTestId('cross-framing-framing-escalation')).toHaveTextContent(
      'The same fuel shock reads as coercive leverage [4].',
    )
    expect(screen.getByTestId('cross-framing-state-escalation')).toHaveTextContent('frames it')
    const silent = screen.getByTestId('cross-framing-unit-military_posture')
    expect(silent).toHaveTextContent('cites none of this claim’s evidence')
    expect(silent).toHaveTextContent('absent on this matter, not contradicting it')
  })

  it('renders a STALE typed absence in the route’s own words, with its proof', async () => {
    mount()
    await screen.findByTestId('cross-framing-claim')
    const row = screen.getByTestId('cross-framing-absence-economic_coercion')
    expect(row).toHaveTextContent('source stale')
    expect(row).toHaveTextContent('every feed mapped to this unit last published')
    expect(row).toHaveTextContent('last known absence, not re-checked')
    expect(row).toHaveTextContent('proof: the desk’s registered feed set')
  })

  it('states a unit with no read and NO typed absence rather than leaving a gap', async () => {
    mount()
    await screen.findByTestId('cross-framing-claim')
    const row = screen.getByTestId('cross-framing-absence-proliferation_watch')
    expect(row).toHaveTextContent('no typed absence is recorded')
    expect(row).toHaveTextContent('no_head_in_horizon')
  })

  it('never prints an unmeasured figure as zero', async () => {
    mount()
    await screen.findByTestId('cross-framing-claim')
    expect(screen.getByTestId('cross-framing-unit-proliferation_watch')).toHaveTextContent(
      '— no admitted read',
    )
  })

  it('renders the six leans with their priors, framings, and honest silences', async () => {
    mount()
    await screen.findByTestId('cross-framing-claim')
    for (const id of [
      'lens_left',
      'lens_right',
      'lens_centre',
      'lens_pragmatist',
      'lens_militarist',
      'lens_isolationist',
    ]) {
      expect(screen.getByTestId(`cross-framing-lens-${id}`)).toBeTruthy()
    }
    expect(screen.getByTestId('cross-framing-lens-framing-lens_militarist')).toHaveTextContent(
      'the fuel shock is leverage, not scarcity',
    )
    expect(screen.getByTestId('cross-framing-lens-lens_militarist')).toHaveTextContent(
      'who holds the next rung',
    )
    expect(screen.getByTestId('cross-framing-lens-lens_left')).toHaveTextContent(
      'no lens read in the window',
    )
  })

  it('says the desk was NOT MEASURED rather than implying the layers agree', async () => {
    mount()
    await screen.findByTestId('cross-framing-claim')
    expect(screen.getByTestId('cross-framing-divergence-absent')).toHaveTextContent(
      'does not resolve this desk',
    )
    expect(screen.getByTestId('cross-framing-divergence-audit')).toHaveTextContent('SEAMS #60')
  })

  it('names what no unit says — the units, the unread kinds, and the drop ledger', async () => {
    mount()
    await screen.findByTestId('cross-framing-claim')
    const s = screen.getByTestId('cross-framing-silence')
    expect(s).toHaveTextContent('named by 0 of 5 units')
    expect(s).toHaveTextContent('absent, not contradicted')
    expect(screen.getByTestId('cross-framing-not-measured')).toHaveTextContent(
      'no loaded collection names this desk',
    )
    expect(screen.getByTestId('cross-framing-drop-no_head_in_horizon')).toHaveTextContent(
      'considered and did not carry 1 read',
    )
    expect(screen.getByTestId('cross-framing-tension-check')).toHaveTextContent(
      'examined 21 unit pairs for tension and found 0',
    )
  })

  it('ASK ABOUT THIS CLAIM sets the ambient consult scope pin — not a side panel', async () => {
    mount()
    await screen.findByTestId('cross-framing-claim')
    expect(useScope.getState().scope).toBeNull()

    fireEvent.click(screen.getByTestId('cross-framing-ask'))

    const scope = useScope.getState().scope
    expect(scope).toBeTruthy()
    expect(scope!.id).toBe(RECORD)
    expect(scope!.origin).toBe('cross-framing')
    expect(scope!.label).toContain('Italy’s energy-security pressure remains elevated')
    expect(scope!.members.signalIds).toEqual([SIG])
    // …and it projects into the ONE ambient pin Consult replaces on every
    // scope change, so the existing session follows the reader here.
    expect(scopePin(scope!).origin).toBe(SCOPE_PIN_ORIGIN)
    await waitFor(() =>
      expect(screen.getByText(/scope pin set/)).toBeTruthy(),
    )
  })

  it('asks for a claim rather than guessing when nothing is in hand', async () => {
    // Drain the parked subject the beforeEach left, and mount with no scope.
    const { unmount } = mount()
    await screen.findByTestId('cross-framing-claim')
    unmount()
    render(wrap(<CrossFramingPanel registration={reg()} scope={{}} mode="personal" />))
    expect(await screen.findByTestId('cross-framing-no-subject')).toHaveTextContent(
      'No claim in hand',
    )
  })

  it('says the desk has no composition rather than showing an empty table', async () => {
    records = []
    mount()
    expect(await screen.findByTestId('cross-framing-no-record')).toHaveTextContent(
      'has no published',
    )
  })
})
