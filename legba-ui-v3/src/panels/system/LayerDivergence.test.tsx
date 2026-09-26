/**
 * Component test for `system.layer_divergence` (7b-v).
 *
 * Mocks the registry at the HTTP boundary. What these hold:
 *   * the five pair states each render with their own badge, and the
 *     `no_fire_reason` is printed BY NAME beside it;
 *   * an aperture-excluded pair shows WHICH layer and the operator's own
 *     reason — a missing layer that did not say why would read as agreement;
 *   * a fired pair links into the Inspector;
 *   * the sparkline draws from the fixture and breaks its z track on unscored
 *     days rather than bridging them;
 *   * "no run yet" is its own empty state, distinct from `measured: false`,
 *     which renders as a loud failed read;
 *   * the SEAMS #60 audit stamp and the instrument's own version/scale stamps
 *     are always on the page.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import LayerDivergencePanel from './LayerDivergence'
import type {
  LayerDivergenceDesk,
  LayerDivergencePair,
  LayerDivergenceResponse,
  LayerDivergenceSeriesPoint,
} from '@/lib/api'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'ld1',
    panel_id: 'system_layer_divergence',
    descriptor_id: '(singleton)',
    descriptor_version: '0'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Layer Divergence',
    mode: 'personal',
    layout_slot: 'main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-09-24T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const VOCAB = [
  'official', 'domestic_press', 'foreign_press', 'social_digest',
  'public_data', 'physical',
]

const AR_OFFICIAL_REASON =
  'curated: absent from our sources — no Argentine government or state-media feed is registered'

function point(
  day: string,
  a: number,
  b: number,
  z: number | null,
): LayerDivergenceSeriesPoint {
  return {
    day, a, b,
    log_ratio: -0.36,
    blank: a === 0 && b === 0,
    z,
    baseline: { n: 14, centre: -0.44, mad: 0.75, scale: 1.12, scale_floored: false },
  }
}

function layer(declared: string, reason = '', sources = 0, thinDays: number | null = null) {
  return {
    declared,
    reason,
    sources_mapped: sources,
    thin_days: thinDays,
    daily: [
      { day: '2026-09-23', raw: 5, kept: 4, folded: 1 },
      { day: '2026-09-24', raw: 6, kept: 6, folded: 0 },
    ],
  }
}

function pairs(): LayerDivergencePair[] {
  return [
    {
      pair_id: 'regime_public_gap',
      layer_a: 'official',
      layer_b: 'social_digest',
      evaluable: false,
      no_fire_reason: 'aperture_excluded',
      excluded_layers: [
        { layer: 'official', state: 'absent', reason: AR_OFFICIAL_REASON },
      ],
    },
    {
      pair_id: 'narrative_control',
      layer_a: 'domestic_press',
      layer_b: 'foreign_press',
      evaluable: true,
      no_fire_reason: 'below_threshold',
      series: [
        point('2026-09-21', 7, 14, null),
        point('2026-09-22', 9, 17, 0.31),
        point('2026-09-23', 11, 12, 0.98),
        point('2026-09-24', 8, 19, 0.07),
      ],
    },
    {
      pair_id: 'credibility_gap',
      layer_a: 'official',
      layer_b: 'public_data',
      evaluable: true,
      no_fire_reason: 'baseline_thin',
      series: [point('2026-09-23', 2, 6, null), point('2026-09-24', 3, 7, null)],
    },
  ]
}

function desk(over: Partial<LayerDivergenceDesk> = {}): LayerDivergenceDesk {
  return {
    target_id: 'country_g20_ar',
    country: 'AR',
    map_version: 'layer_map_ar.v1',
    sources_mapped: 49,
    rows_scanned: 227,
    rows_truncated: false,
    aperture: {
      present: ['domestic_press', 'foreign_press', 'physical', 'public_data'],
      absent: [{ layer: 'official', reason: AR_OFFICIAL_REASON }],
      unmeasured: [{ layer: 'social_digest', reason: 'nobody has looked yet' }],
      undeclared: [],
    },
    counts_suppressed_by_aperture: {},
    layers: {
      official: layer('absent', AR_OFFICIAL_REASON, 0),
      domestic_press: layer('present', '', 21, 0),
      foreign_press: layer('present', '', 22, 0),
      social_digest: layer('unmeasured', 'nobody has looked yet', 0),
      public_data: layer('present', '', 5, 1),
      physical: layer('present', '', 1, 4),
    },
    pairs: pairs(),
    fired: null,
    ...over,
  }
}

function payload(over: Partial<LayerDivergenceResponse> = {}): LayerDivergenceResponse {
  return {
    measured: true,
    generated_at: '2026-09-24T21:00:00Z',
    reader_version: '2026-09/p6-l2',
    unit_sentence:
      'Per country, the same stack of source layers is counted per day and the finding is the CHANGE in that country own layer-to-layer divergence.',
    as_of: '2026-09-24',
    receipt_run_id: '55543970-6c2d-4974-8864-577238ebb59f',
    run_started_at: '2026-09-24T05:40:00Z',
    method_version: 'layer_divergence/2026-09.1',
    payload_schema: 'layer_divergence.v1',
    classification_audit:
      'unaudited: no sampled layer-classification audit has been run over this map (SEAMS #60).',
    window_days: 28,
    baseline_days: 14,
    z_threshold: 2.0,
    mad_floor: 0.2,
    consecutive_days: 2,
    thin_min_per_day: 5,
    layer_vocab: VOCAB,
    pairs_declared: [
      {
        pair_id: 'regime_public_gap', layer_a: 'official', layer_b: 'social_digest',
        meaning: 'the regime-public gap',
      },
      {
        pair_id: 'narrative_control', layer_a: 'domestic_press',
        layer_b: 'foreign_press', meaning: 'narrative control',
      },
      {
        pair_id: 'credibility_gap', layer_a: 'official', layer_b: 'public_data',
        meaning: 'the credibility gap',
      },
    ],
    desks: [desk()],
    desks_unresolved: [],
    warnings: [],
    ...over,
  }
}

let served: LayerDivergenceResponse
const seenUrls: string[] = []

beforeEach(() => {
  seenUrls.length = 0
  served = payload()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      seenUrls.push(String(input))
      return {
        ok: true,
        status: 200,
        json: async () => served,
        text: async () => JSON.stringify(served),
      } as unknown as Response
    }),
  )
})

describe('LayerDivergence panel', () => {
  it('renders the instrument stamps and the SEAMS #60 audit banner', async () => {
    render(wrap(<LayerDivergencePanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('desk-card-country_g20_ar')

    expect(screen.getByTestId('layer-divergence-audit').textContent).toContain(
      'SEAMS #60',
    )
    // The scale/method stamps are on the page, not buried in a tooltip.
    expect(screen.getByText('layer_divergence/2026-09.1')).toBeTruthy()
    expect(screen.getByText('layer_divergence.v1')).toBeTruthy()
    expect(screen.getByText('2026-09-24 (UTC day)')).toBeTruthy()
    expect(
      screen.getByText('|z| ≥ 2σ on 2 consecutive days'),
    ).toBeTruthy()
    expect(screen.getByText('5 items/day')).toBeTruthy()
    // The unit's own sentence comes from the server, never a bundled copy.
    expect(
      screen.getByText(/the same stack of source layers is counted per day/),
    ).toBeTruthy()
  })

  it('renders the six layer chips with their declaration', async () => {
    render(wrap(<LayerDivergencePanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('desk-card-country_g20_ar')

    for (const l of VOCAB) {
      expect(screen.getByTestId(`layer-chip-country_g20_ar-${l}`)).toBeTruthy()
    }
    const official = screen.getByTestId('layer-chip-country_g20_ar-official')
    expect(official.textContent).toContain('absent')
    expect(official.getAttribute('title')).toContain(AR_OFFICIAL_REASON)
    expect(
      screen.getByTestId('layer-chip-country_g20_ar-social_digest').textContent,
    ).toContain('unmeasured')
  })

  it('an aperture-excluded pair names the layer AND the reason', async () => {
    render(wrap(<LayerDivergencePanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('desk-card-country_g20_ar')

    const key = 'country_g20_ar-regime_public_gap'
    expect(screen.getByTestId(`pair-state-${key}`).textContent).toBe('not evaluable')
    const note = screen.getByTestId(`pair-exclusion-${key}`)
    expect(note.textContent).toContain('official')
    expect(note.textContent).toContain('declared absent')
    expect(note.textContent).toContain(AR_OFFICIAL_REASON)
    // The no_fire_reason is printed by name, not folded into a grey "no".
    expect(screen.getByTestId(`pair-row-${key}`).textContent).toContain(
      'aperture_excluded',
    )
    // No sparkline for a pair that was never evaluated.
    expect(screen.queryByTestId(`pair-spark-${key}`)).toBeNull()
  })

  it('an evaluable pair draws its sparkline and its last-day z', async () => {
    render(wrap(<LayerDivergencePanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('desk-card-country_g20_ar')

    const key = 'country_g20_ar-narrative_control'
    expect(screen.getByTestId(`pair-state-${key}`).textContent).toBe('below threshold')
    const svg = screen.getByTestId(`pair-spark-${key}`)
    // One shared count axis over both layers, and one contiguous z run.
    expect(svg.getAttribute('data-count-max')).toBe('19')
    expect(svg.getAttribute('data-z-segments')).toBe('1')
    const last = screen.getByTestId(`pair-lastday-${key}`)
    expect(last.textContent).toContain('z +0.07σ')
    expect(last.textContent).toContain('2026-09-24: 8 / 19 items/day')
    expect(last.textContent).toContain('foreign_press louder')
  })

  it('a pair with no usable baseline shows no z and says why by name', async () => {
    render(wrap(<LayerDivergencePanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('desk-card-country_g20_ar')

    const key = 'country_g20_ar-credibility_gap'
    // `thin` outranks the generic badge: the last day is 3 / 7 against a floor
    // of 5, so the thin side is named before the z is read.
    expect(screen.getByTestId(`pair-state-${key}`).textContent).toBe('thin')
    expect(screen.getByTestId(`pair-row-${key}`).textContent).toContain(
      'baseline_thin',
    )
    // The z renders as absence, never as 0.00.
    const last = screen.getByTestId(`pair-lastday-${key}`)
    expect(last.textContent).toContain('—')
    expect(last.textContent).not.toContain('0.00')
    expect(
      screen.getByTestId(`pair-spark-${key}`).getAttribute('data-z-segments'),
    ).toBe('0')
  })

  it('a fired pair carries its direction and links into the Inspector', async () => {
    served = payload({
      desks: [
        desk({
          fired: {
            finding_id: 'ff-42',
            produced_at: '2026-09-24T05:41:00Z',
            pair_id: 'narrative_control',
            direction: 'widening',
            severity: 'elevated',
            day: '2026-09-24',
            z: -2.77,
            thin: true,
          },
        }),
      ],
    })
    render(wrap(<LayerDivergencePanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('desk-card-country_g20_ar')

    const key = 'country_g20_ar-narrative_control'
    expect(screen.getByTestId(`pair-state-${key}`).textContent).toBe('fired')
    // The thin caveat rides alongside the fire rather than replacing it.
    expect(screen.getByTestId(`pair-thin-${key}`)).toBeTruthy()
    const fired = screen.getByTestId(`pair-fired-${key}`)
    expect(fired.textContent).toContain('widening')
    expect(fired.textContent).toContain('elevated')
    expect(fired.querySelector('[data-testid="record-link"]')).toBeTruthy()
  })

  it('"no run yet" is its own state, not an empty measurement', async () => {
    served = payload({
      receipt_run_id: null, as_of: null, desks: [],
      method_version: null, payload_schema: null, classification_audit: null,
      window_days: null, z_threshold: null, consecutive_days: null,
      thin_min_per_day: null, baseline_days: null,
    })
    render(wrap(<LayerDivergencePanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('layer-divergence-no-run')

    expect(screen.getByTestId('layer-divergence-no-run').textContent).toContain(
      'never written a receipt',
    )
    expect(screen.queryByTestId('layer-divergence-unmeasured')).toBeNull()
    // Absence renders as absence, not as zero.
    expect(screen.queryByText('0')).toBeNull()
  })

  it('measured:false renders as a FAILED READ, loudly', async () => {
    served = payload({ measured: false, desks: [], receipt_run_id: null })
    render(wrap(<LayerDivergencePanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('layer-divergence-unmeasured')

    expect(screen.getByTestId('layer-divergence-unmeasured').textContent).toContain(
      'failed read',
    )
    // A failed read must not ALSO claim the unit has never run: we could not
    // look, which says nothing about whether there is something to look at.
    expect(screen.queryByTestId('layer-divergence-no-run')).toBeNull()
  })

  it('reports the map/declaration disagreement rather than applying it silently', async () => {
    served = payload({
      desks: [desk({ counts_suppressed_by_aperture: { social_digest: 9 } })],
    })
    render(wrap(<LayerDivergencePanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('desk-suppressed-country_g20_ar')

    expect(
      screen.getByTestId('desk-suppressed-country_g20_ar').textContent,
    ).toContain('social_digest: 9 items suppressed')
  })

  it('the fired-window buttons change the query the panel sends', async () => {
    render(wrap(<LayerDivergencePanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('desk-card-country_g20_ar')
    expect(seenUrls.some((u) => u.includes('fired_days=30'))).toBe(true)

    screen.getByTestId('layer-divergence-fired-7').click()
    await waitFor(() =>
      expect(seenUrls.some((u) => u.includes('fired_days=7'))).toBe(true),
    )
  })
})
