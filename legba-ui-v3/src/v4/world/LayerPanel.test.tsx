/**
 * The map's layer switcher, and the APERTURE `i` it gained in the wave-P
 * design pass.
 *
 * The draw layers (signals / findings / situations / events / entities) are one
 * thing; the six SOURCE layers of `docs/LAYERS.md` are another, and only the
 * second has a per-country declaration behind it. What is pinned here is that
 * the second section exists on the map at all, that each of the six carries an
 * `i` stating what the operator declared for the country in scope, and that a
 * desk with no loaded layer map says so rather than rendering six blanks.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import LayerPanel from './LayerPanel'
import { LAYER_VOCAB } from '@/lib/layerAperture'
import { emptyMembers, resetScope, useScope } from '@/state/scope'

const RECEIPT = {
  measured: true,
  generated_at: '2026-09-26T16:13:41.035612Z',
  reader_version: '2026-09/p6-l2',
  unit_sentence: '',
  as_of: '2026-09-26',
  receipt_run_id: 'run-1',
  run_started_at: null,
  method_version: 'layer_divergence/2026-09.1',
  payload_schema: null,
  classification_audit: null,
  window_days: 30,
  baseline_days: 14,
  z_threshold: 2,
  mad_floor: null,
  consecutive_days: 2,
  thin_min_per_day: 3,
  layer_vocab: [...LAYER_VOCAB],
  pairs_declared: [],
  desks: [
    {
      target_id: 'country_g20_ar',
      country: 'AR',
      map_version: 'layer_map_ar.v1',
      sources_mapped: 49,
      rows_scanned: 100,
      rows_truncated: false,
      aperture: {},
      counts_suppressed_by_aperture: { social_digest: 10 },
      layers: {
        official: {
          declared: 'absent',
          reason: 'curated: no Argentine government feed is registered',
          sources_mapped: 0,
          thin_days: null,
          daily: [],
        },
        domestic_press: { declared: 'present', reason: '', sources_mapped: 12, thin_days: 3, daily: [] },
        foreign_press: { declared: 'present', reason: '', sources_mapped: 30, thin_days: 0, daily: [] },
        social_digest: {
          declared: 'absent',
          reason: 'curated: the Telegram list carries no Argentine channels',
          sources_mapped: 2,
          thin_days: null,
          daily: [],
        },
        public_data: { declared: 'unmeasured', reason: '', sources_mapped: 5, thin_days: null, daily: [] },
        physical: { declared: 'present', reason: '', sources_mapped: 2, thin_days: 1, daily: [] },
      },
      pairs: [],
      fired: null,
    },
  ],
  desks_unresolved: [],
  warnings: [],
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function stubFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string) => {
      const body = String(url).includes('/v3/layers/divergence') ? RECEIPT : {}
      return {
        ok: true,
        status: 200,
        json: async () => body,
        text: async () => JSON.stringify(body),
      } as unknown as Response
    }),
  )
}

/** The switcher is collapsed by default; the aperture section rides inside it. */
function expand() {
  fireEvent.click(screen.getByLabelText('Expand layers panel'))
}

beforeEach(() => {
  resetScope()
  stubFetch()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('LayerPanel — the aperture section', () => {
  it('is not mounted (and reads nothing) while the switcher is collapsed', () => {
    render(wrap(<LayerPanel />))
    expect(screen.queryByTestId('aperture-section')).toBeNull()
    const calls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
    expect(calls.some((c) => String(c[0]).includes('/v3/layers/divergence'))).toBe(false)
  })

  it('lists all six source layers, each with its own `i`', async () => {
    useScope.getState().setScope({
      kind: 'target',
      id: 'country_g20_ar',
      label: 'Argentina',
      members: emptyMembers(),
      origin: 'test',
    })
    render(wrap(<LayerPanel />))
    expand()
    await waitFor(() =>
      expect(screen.getByTestId('aperture-state-official')).toHaveTextContent('absent'),
    )
    for (const layer of LAYER_VOCAB) {
      expect(screen.getByTestId(`aperture-row-${layer}`)).toBeInTheDocument()
      expect(screen.getByTestId(`aperture-info-${layer}`)).toBeInTheDocument()
    }
    expect(screen.getByTestId('aperture-summary')).toHaveTextContent(
      'AR: 3 of 6 present · 2 declared absent · 1 unmeasured',
    )
  })

  it("the `i` carries the operator's own reason and the map version", async () => {
    useScope.getState().setScope({
      kind: 'target',
      id: 'country_g20_ar',
      label: 'Argentina',
      members: emptyMembers(),
      origin: 'test',
    })
    render(wrap(<LayerPanel />))
    expand()
    await waitFor(() =>
      expect(screen.getByTestId('aperture-info-official-popover')).toHaveTextContent(
        'no Argentine government feed is registered',
      ),
    )
    const official = screen.getByTestId('aperture-info-official-popover')
    expect(official).toHaveTextContent('nothing is ingested on it')
    expect(official).toHaveTextContent('layer_map_ar.v1')

    // The other kind of absence — ingested, and suppressed by the declaration.
    const social = screen.getByTestId('aperture-info-social_digest-popover')
    expect(social).toHaveTextContent('even though rows ARE arriving')
    expect(social).toHaveTextContent('10 rows on this layer are suppressed')
  })

  it('a desk with no loaded layer map says so rather than showing six blanks', async () => {
    useScope.getState().setScope({
      kind: 'target',
      id: 'country_g20_br',
      label: 'Brazil',
      members: emptyMembers(),
      origin: 'test',
    })
    render(wrap(<LayerPanel />))
    expand()
    await waitFor(() =>
      expect(screen.getByTestId('aperture-summary')).toHaveTextContent(
        'no layer map is loaded for this desk',
      ),
    )
    expect(screen.getByTestId('aperture-state-official')).toHaveTextContent('no map')
  })

  it('with no country in scope it states that, not an empty table', async () => {
    render(wrap(<LayerPanel />))
    expand()
    await waitFor(() =>
      expect(screen.getByTestId('aperture-summary')).toHaveTextContent('no country is in scope'),
    )
    expect(screen.getAllByTestId(/^aperture-row-/)).toHaveLength(LAYER_VOCAB.length)
  })
})
