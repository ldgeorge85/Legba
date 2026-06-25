/**
 * Component test for the DiscoveryPipelinePanel (D3).
 *
 * Asserts:
 *  - discovery descriptors (rows carrying a `discovery` block) render, and
 *    non-discovery rows are excluded;
 *  - candidates (children whose `inherits` carries a discovery id) fall into
 *    the correct pipeline column per their descriptor state;
 *  - the family filter narrows the descriptor set;
 *  - DLQ rows tagged as discovery rejections surface in the rejected lane.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import DiscoveryPipelinePanel from './DiscoveryPipeline'
import type { PanelRegistration } from '@/types'
import {
  buildCandidates,
  liftDiscovery,
  stageForState,
  isDiscoveryRejection,
  type DescriptorRowOut,
} from './discoveryTypes'

function reg(): PanelRegistration {
  return {
    id: 'd3',
    panel_id: 'registry_discovery',
    descriptor_id: '(singleton)',
    descriptor_version: 'v0000000',
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Discovery Pipeline',
    mode: 'personal',
    layout_slot: 'registry.discovery',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-05-20T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function row(over: Partial<DescriptorRowOut>): DescriptorRowOut {
  return {
    descriptor_id: 'x',
    version: 'v'.repeat(16),
    schema_uri: 'legba/target/2.0.0',
    is_head: true,
    state: 'active',
    owner: 'lewis',
    name: 'X',
    family: 'target',
    body: {},
    created_at: '2026-05-20T00:00:00Z',
    abstraction_level: 'L1',
    inherits: [],
    retire_after: null,
    kind: null,
    type_signature: null,
    ...over,
  }
}

const DISCOVERY_ROW = row({
  descriptor_id: 'discovery_geopolitical_countries',
  name: 'Geopolitical Per-Country Discovery',
  state: 'configured',
  abstraction_level: 'L2',
  inherits: ['template_country'],
  body: {
    discovery: { kind: 'country_list', list_source: 'iso3166', relabel: [{}, {}] },
  },
})

const PLAIN_TARGET = row({
  descriptor_id: 'standalone_brazil',
  name: 'Brazil (standalone)',
  state: 'active',
  inherits: [],
  body: { scope: { geo: ['BR'] } },
})

// children of the discovery descriptor (inherits carries its id)
const CAND_PROPOSED = row({
  descriptor_id: 'country_geopolitical_br',
  name: 'Brazil',
  state: 'draft',
  inherits: ['template_country', 'discovery_geopolitical_countries'],
  body: { scope: { geo: ['BR'] } },
})
const CAND_REGISTERED = row({
  descriptor_id: 'country_geopolitical_ng',
  name: 'Nigeria',
  state: 'active',
  inherits: ['template_country', 'discovery_geopolitical_countries'],
  body: { scope: { geo: ['NG'] } },
})

function mockFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string) => {
      let payload: unknown = []
      if (url.includes('family=target')) {
        payload = [DISCOVERY_ROW, PLAIN_TARGET, CAND_PROPOSED, CAND_REGISTERED]
      } else if (url.includes('family=source')) {
        payload = []
      } else if (url.includes('dead_letter')) {
        payload = [
          {
            id: 'dlq1',
            attempted_at: '2026-05-21T00:00:00Z',
            actor: 'source_discovery.materializer',
            namespace: 'discovery',
            declared_schema_uri: null,
            validation_error: { reason: 'trial pull failed' },
            resolution: 'rejected: liveness',
            attempted_payload: { natural_key: 'badhost.example' },
          },
          {
            id: 'dlq2',
            attempted_at: '2026-05-21T00:00:00Z',
            actor: 'some_other_actor',
            namespace: 'ingest',
            declared_schema_uri: null,
            validation_error: {},
            resolution: 'unrelated',
            attempted_payload: {},
          },
        ]
      }
      return Promise.resolve({ ok: true, json: async () => payload })
    }),
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('discoveryTypes helpers', () => {
  it('lifts only rows with a discovery block', () => {
    expect(liftDiscovery(DISCOVERY_ROW, 'target')?.block.kind).toBe('country_list')
    expect(liftDiscovery(PLAIN_TARGET, 'target')).toBeNull()
  })

  it('maps descriptor state onto pipeline stages', () => {
    expect(stageForState('draft')).toBe('proposed')
    expect(stageForState('configured')).toBe('validated')
    expect(stageForState('active')).toBe('registered')
    expect(stageForState('paused')).toBe('registered')
    expect(stageForState('retired')).toBe('rejected')
  })

  it('builds candidates only for children inheriting a discovery id', () => {
    const ids = new Set(['discovery_geopolitical_countries'])
    const cands = buildCandidates(
      [DISCOVERY_ROW, PLAIN_TARGET, CAND_PROPOSED, CAND_REGISTERED],
      ids,
      'target',
    )
    expect(cands.map((c) => c.descriptorId).sort()).toEqual([
      'country_geopolitical_br',
      'country_geopolitical_ng',
    ])
    // natural key read from scope.geo[0]
    expect(cands.find((c) => c.descriptorId === 'country_geopolitical_br')?.naturalKey).toBe('BR')
  })

  it('detects discovery rejections in the DLQ', () => {
    expect(
      isDiscoveryRejection({
        id: 'a',
        attempted_at: '',
        actor: 'source_discovery.x',
        namespace: 'discovery',
        declared_schema_uri: null,
        validation_error: {},
        resolution: null,
        attempted_payload: null,
      }),
    ).toBe(true)
    expect(
      isDiscoveryRejection({
        id: 'b',
        attempted_at: '',
        actor: 'ingest',
        namespace: 'ingest',
        declared_schema_uri: null,
        validation_error: {},
        resolution: null,
        attempted_payload: {},
      }),
    ).toBe(false)
  })
})

describe('DiscoveryPipelinePanel', () => {
  it('renders discovery descriptors, the candidate pipeline, and the DLQ lane', async () => {
    mockFetch()
    render(wrap(<DiscoveryPipelinePanel registration={reg()} scope={{}} mode="personal" />))

    // discovery descriptor shows; the plain target does not.
    await waitFor(() => {
      expect(screen.getByTestId('discovery-row-discovery_geopolitical_countries')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('discovery-row-standalone_brazil')).toBeNull()

    // pipeline columns: proposed has BR (draft), registered has NG (active).
    const proposed = screen.getByTestId('pipeline-col-proposed')
    const registered = screen.getByTestId('pipeline-col-registered')
    await waitFor(() => {
      expect(within(proposed).getByTestId('candidate-country_geopolitical_br')).toBeInTheDocument()
    })
    expect(within(registered).getByTestId('candidate-country_geopolitical_ng')).toBeInTheDocument()
    expect(screen.getByTestId('pipeline-count-proposed')).toHaveTextContent('1')
    expect(screen.getByTestId('pipeline-count-registered')).toHaveTextContent('1')

    // only the discovery-tagged DLQ row surfaces.
    const dlq = screen.getByTestId('discovery-dlq')
    expect(within(dlq).getByTestId('dlq-dlq1')).toBeInTheDocument()
    expect(within(dlq).queryByTestId('dlq-dlq2')).toBeNull()
  })

  it('narrows descriptors with the family filter', async () => {
    mockFetch()
    render(wrap(<DiscoveryPipelinePanel registration={reg()} scope={{}} mode="personal" />))

    await waitFor(() => {
      expect(screen.getByTestId('discovery-row-discovery_geopolitical_countries')).toBeInTheDocument()
    })

    // switch to source-only: the target-family discovery descriptor disappears.
    fireEvent.change(screen.getByTestId('discovery-family-filter'), {
      target: { value: 'source' },
    })
    await waitFor(() => {
      expect(
        screen.queryByTestId('discovery-row-discovery_geopolitical_countries'),
      ).toBeNull()
    })
  })
})
