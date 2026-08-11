/**
 * Component test for the K-G4 Graph Walk panel.
 *
 * cytoscape needs a canvas jsdom cannot render, so `react-cytoscapejs` is
 * mocked to a sentinel that also exposes the elements it was handed — that
 * lets the interaction tests assert on what WOULD be drawn without a canvas.
 * The projection itself (family styling, accumulation, disclosure) is covered
 * by `lib/graphWalkModel.test.ts`; this file covers the panel's chrome, its
 * fetch wiring, and the honesty affordances that only exist in the markup.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

const lastElements: { current: unknown[] } = { current: [] }
/** The panel's `cy` callback, captured so a tap can be dispatched at it. */
const lastCy: { current: ((cy: unknown) => void) | null } = { current: null }

vi.mock('react-cytoscapejs', () => ({
  default: (props: { elements: unknown[]; cy?: (cy: unknown) => void }) => {
    lastElements.current = props.elements
    lastCy.current = props.cy ?? null
    return <div data-testid="cytoscape-mock" data-count={props.elements.length} />
  },
}))

// The visibility gate observes real layout boxes, which jsdom reports as 0×0;
// force it open so the canvas branch renders.
vi.mock('@/lib/cytoscapeFit', () => ({
  useVisibleSize: () => ({ ref: () => {}, visible: true }),
  attachFitOnResize: () => () => {},
}))

import GraphWalkPanel, { EvidenceBody } from './GraphWalk'
import type { PanelRegistration } from '@/types'
import type { EdgeEvidence } from '@/lib/graphWalkModel'

const US = '66c795b7-73ba-44a3-9cfe-60cc2b7dfbb9'
const IRAN = '8e7c0a9e-4950-40af-8ced-06c55c4923ae'

function reg(): PanelRegistration {
  return {
    id: 'gw1',
    panel_id: 'system_graph_walk',
    descriptor_id: 'system',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'analyst',
    analyst_id: null,
    title: 'Graph Walk',
    mode: 'personal',
    layout_slot: 'system.graph_walk.main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-08-03T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function egoBody(over: Record<string, unknown> = {}) {
  return {
    anchor: {
      id: US,
      canonical_name: 'United States',
      entity_class: 'country',
      entity_type: 'country',
      geo_country: 'US',
      degree: 693,
      resolved: true,
    },
    nodes: [
      {
        id: IRAN,
        canonical_name: 'Iran',
        entity_class: 'country',
        entity_type: 'country',
        geo_country: null,
        degree: 595,
        resolved: true,
      },
    ],
    edges: [
      {
        id: 'e1',
        src_id: US,
        dst_id: IRAN,
        direction: 'out',
        edge_family: 'relation',
        edge_type: 'hostile to',
        polarity: -1,
        confidence: 0.9,
        observed_count: 2,
        intent: 'hostile',
        channel: 'direct',
        source_type: 'agent',
        valid_from: null,
        first_seen_at: null,
        last_seen_at: null,
        has_evidence: true,
        signal_count: 3,
      },
    ],
    stitch_edges: [],
    facets: [
      {
        edge_family: 'cooccurrence',
        edge_type: 'co occurs with',
        count: 583,
        negative: 0,
        neutral: 583,
        positive: 0,
      },
      {
        edge_family: 'relation',
        edge_type: 'hostile to',
        count: 31,
        negative: 30,
        neutral: 1,
        positive: 0,
      },
    ],
    degree_total: 693,
    degree_matched: 1,
    truncated: false,
    filters: {
      family: ['relation', 'reference'],
      edge_type: [],
      polarity: [],
      min_confidence: 0,
      since: null,
      until: null,
      direction: 'both',
      limit: 80,
    },
    ...over,
  }
}

/** Route by URL substring; records every requested URL for assertions. */
function routedFetch(routes: Record<string, unknown>, seen: string[] = []) {
  return vi.fn(async (url: string) => {
    seen.push(url)
    const key = Object.keys(routes).find((k) => url.includes(k))
    if (key === undefined) {
      return { ok: false, status: 404, text: async () => '{"detail":"not found"}' }
    }
    const body = routes[key]
    if (body && typeof body === 'object' && '__status' in (body as object)) {
      const b = body as { __status: number; detail?: string }
      return {
        ok: false,
        status: b.__status,
        text: async () => JSON.stringify({ detail: b.detail ?? 'err' }),
      }
    }
    return { ok: true, json: async () => body }
  })
}

function panel() {
  return wrap(<GraphWalkPanel registration={reg()} scope={{}} mode="personal" />)
}

/**
 * A stand-in for the cytoscape `Core` the panel wires its tap handlers onto.
 *
 * Expand-on-click is a canvas gesture, so without this the panel's single most
 * important interaction — and every disclosure that has to survive it — is
 * unreachable from a test.
 */
type TapHandler = (evt: { target: { id: () => string } }) => void

function fakeCore() {
  const handlers: Record<string, TapHandler> = {}
  return {
    handlers,
    removeListener: () => {},
    on: (event: string, selector: string, fn: TapHandler) => {
      handlers[`${event}:${selector}`] = fn
    },
    zoom: () => 1,
    fit: () => {},
  }
}

/** Mount the fake core and tap a node, exactly as the canvas would. */
async function tapNode(id: string) {
  const core = fakeCore()
  act(() => lastCy.current?.(core))
  await act(async () => {
    core.handlers['tap:node']?.({ target: { id: () => id } })
  })
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  lastElements.current = []
  lastCy.current = null
})

describe('GraphWalk — starting a walk', () => {
  it('invites the operator to pick an actor before anything is anchored', () => {
    vi.stubGlobal('fetch', routedFetch({}))
    render(panel())
    expect(screen.getByTestId('graph-walk-empty')).toBeInTheDocument()
    expect(screen.getByTestId('graph-walk-search')).toBeInTheDocument()
  })

  it('searches entities and anchors the walk on the chosen one', async () => {
    const seen: string[] = []
    vi.stubGlobal(
      'fetch',
      routedFetch(
        {
          '/entities?q=': {
            data: [{ id: US, canonical_name: 'United States', entity_class: 'country' }],
          },
          '/graph/ego': egoBody(),
        },
        seen,
      ),
    )
    render(panel())

    fireEvent.change(screen.getByTestId('graph-walk-search'), {
      target: { value: 'United' },
    })
    await waitFor(() =>
      expect(screen.getByTestId('graph-walk-search-results')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByText(/United States/))

    await waitFor(() => expect(screen.getByTestId('cytoscape-mock')).toBeInTheDocument())
    expect(seen.some((u) => u.includes(`entity_id=${US}`))).toBe(true)
  })
})

describe('GraphWalk — the default view is not a hairball', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        '/entities?q=': { data: [{ id: US, canonical_name: 'United States', entity_class: 'country' }] },
        '/graph/ego': egoBody(),
      }),
    )
  })

  async function anchored() {
    render(panel())
    fireEvent.change(screen.getByTestId('graph-walk-search'), {
      target: { value: 'United' },
    })
    await waitFor(() => screen.getByTestId('graph-walk-search-results'))
    fireEvent.click(screen.getByText(/United States/))
    await waitFor(() => expect(screen.getByTestId('cytoscape-mock')).toBeInTheDocument())
  }

  it('requests only relation+reference on first paint', async () => {
    const seen: string[] = []
    vi.stubGlobal(
      'fetch',
      routedFetch(
        {
          '/entities?q=': { data: [{ id: US, canonical_name: 'United States', entity_class: 'country' }] },
          '/graph/ego': egoBody(),
        },
        seen,
      ),
    )
    await anchored()
    const egoUrl = seen.find((u) => u.includes('/graph/ego'))!
    expect(egoUrl).toContain('family=relation')
    expect(egoUrl).toContain('family=reference')
    // 8,722 of 12,566 edges are co-mentions — off by default, one chip away.
    expect(egoUrl).not.toContain('family=cooccurrence')
  })

  it('discloses the cooccurrence edges it is withholding', async () => {
    await anchored()
    await waitFor(() =>
      expect(screen.getByTestId('graph-walk-hidden-cooccurrence')).toHaveTextContent(
        '583 cooccurrence hidden',
      ),
    )
  })

  it('reports the anchor open degree', async () => {
    await anchored()
    expect(screen.getByTestId('graph-walk-disclosure')).toHaveTextContent('693')
  })
})

describe('GraphWalk — truncation is disclosed, never silent', () => {
  it('says how many matching edges were withheld by the limit', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        '/entities?q=': { data: [{ id: US, canonical_name: 'United States', entity_class: 'country' }] },
        '/graph/ego': egoBody({ truncated: true, degree_matched: 693 }),
      }),
    )
    render(panel())
    fireEvent.change(screen.getByTestId('graph-walk-search'), { target: { value: 'Un' } })
    await waitFor(() => screen.getByTestId('graph-walk-search-results'))
    fireEvent.click(screen.getByText(/United States/))

    await waitFor(() =>
      expect(screen.getByTestId('graph-walk-truncated')).toHaveTextContent(
        '1 of 693 matching edges drawn',
      ),
    )
  })
})

describe('GraphWalk — the disclosure strip describes the CANVAS, not the first hop', () => {
  const RUSSIA = '1f64f392-0751-45cf-96e4-a8d9d94762dd'

  /** Iran's own ego, folded on when the operator expands that node. */
  function iranHop() {
    return egoBody({
      anchor: {
        id: IRAN,
        canonical_name: 'Iran',
        entity_class: 'country',
        entity_type: 'country',
        geo_country: null,
        degree: 330,
        resolved: true,
      },
      nodes: [
        {
          id: RUSSIA,
          canonical_name: 'Russia',
          entity_class: 'country',
          entity_type: 'country',
          geo_country: null,
          degree: 592,
          resolved: true,
        },
      ],
      edges: [
        {
          ...egoBody().edges[0],
          id: 'e2',
          src_id: IRAN,
          dst_id: RUSSIA,
          edge_type: 'allied with',
          polarity: 1,
        },
      ],
      facets: [
        {
          edge_family: 'cooccurrence',
          edge_type: 'co occurs with',
          count: 301,
          negative: 0,
          neutral: 301,
          positive: 0,
        },
      ],
      degree_total: 330,
      degree_matched: 330,
      truncated: true,
    })
  }

  async function walkThenExpand() {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        '/entities?q=': {
          data: [{ id: US, canonical_name: 'United States', entity_class: 'country' }],
        },
        [`entity_id=${IRAN}`]: iranHop(),
        '/graph/ego': egoBody({ truncated: true, degree_matched: 111 }),
      }),
    )
    render(panel())
    fireEvent.change(screen.getByTestId('graph-walk-search'), { target: { value: 'Un' } })
    await waitFor(() => screen.getByTestId('graph-walk-search-results'))
    fireEvent.click(screen.getByText(/United States/))
    await waitFor(() => expect(screen.getByTestId('cytoscape-mock')).toBeInTheDocument())
  }

  it('moves the truncation denominator when a hop is folded in', async () => {
    await walkThenExpand()
    // the anchor hop, alone on the canvas
    expect(screen.getByTestId('graph-walk-truncated')).toHaveTextContent(
      '1 of 111 matching edges drawn',
    )
    expect(screen.queryByTestId('graph-walk-hops')).not.toBeInTheDocument()

    await tapNode(IRAN)

    // Two anchors are now drawn, so a single "1 of 111" would be a claim about
    // a picture that no longer exists. Each hop's numbers are named instead.
    await waitFor(() =>
      expect(screen.getByTestId('graph-walk-hops')).toHaveTextContent('2 hops walked'),
    )
    const strip = screen.getByTestId('graph-walk-truncated')
    expect(strip).toHaveTextContent('1 of 111 (United States)')
    expect(strip).toHaveTextContent('1 of 330 (Iran)')
  })

  it('attributes the withheld co-mentions to the hop that is withholding them', async () => {
    await walkThenExpand()
    expect(screen.getByTestId('graph-walk-hidden-cooccurrence')).toHaveTextContent(
      '583 cooccurrence hidden',
    )

    await tapNode(IRAN)

    await waitFor(() =>
      expect(screen.getByTestId('graph-walk-hidden-cooccurrence')).toHaveTextContent(
        '583 (United States)',
      ),
    )
    // summed, these would double-count any co-mention BETWEEN the two anchors
    expect(screen.getByTestId('graph-walk-hidden-cooccurrence')).toHaveTextContent(
      '301 (Iran)',
    )
  })
})

describe('GraphWalk — the hostile chips name their family and follow the canvas', () => {
  const G7 = '9d3a1c55-0f1e-4a2e-9b77-2c0f5f3a1b40'

  it('labels each chip by family instead of repeating a bare count', async () => {
    // Two families each carrying a negative edge used to render as two
    // identical, unattributed "N hostile" chips — indistinguishable from a
    // duplicate or a typo.
    vi.stubGlobal(
      'fetch',
      routedFetch({
        '/entities?q=': {
          data: [{ id: US, canonical_name: 'United States', entity_class: 'country' }],
        },
        '/graph/ego': egoBody({
          nodes: [
            egoBody().nodes[0],
            {
              id: G7,
              canonical_name: 'G7',
              entity_class: 'organization',
              entity_type: 'organization',
              geo_country: null,
              degree: 40,
              resolved: true,
            },
          ],
          edges: [
            egoBody().edges[0],
            {
              ...egoBody().edges[0],
              id: 'e2',
              dst_id: G7,
              edge_family: 'reference',
              edge_type: 'expelled from',
              polarity: -1,
            },
          ],
        }),
      }),
    )
    render(panel())
    fireEvent.change(screen.getByTestId('graph-walk-search'), { target: { value: 'Un' } })
    await waitFor(() => screen.getByTestId('graph-walk-search-results'))
    fireEvent.click(screen.getByText(/United States/))

    await waitFor(() =>
      expect(screen.getByTestId('graph-walk-hostile-relation')).toHaveTextContent(
        'relation: 1 hostile drawn',
      ),
    )
    expect(screen.getByTestId('graph-walk-hostile-reference')).toHaveTextContent(
      'reference: 1 hostile drawn',
    )
  })

  it('drops the chip when the canvas is drawing none of that family', async () => {
    // Shot 06: cooccurrence ON, the budget spent, ZERO relation edges drawn —
    // and the strip still announcing "38 hostile" off the facet denominator.
    // A count taken off the canvas cannot make that claim.
    vi.stubGlobal(
      'fetch',
      routedFetch({
        '/entities?q=': {
          data: [{ id: US, canonical_name: 'United States', entity_class: 'country' }],
        },
        // the view once the co-mention chip is on: no asserted edge survives
        'family=cooccurrence': egoBody({
          edges: [
            {
              ...egoBody().edges[0],
              id: 'c1',
              edge_family: 'cooccurrence',
              edge_type: 'co occurs with',
              polarity: 0,
            },
          ],
        }),
        '/graph/ego': egoBody(),
      }),
    )
    render(panel())
    fireEvent.change(screen.getByTestId('graph-walk-search'), { target: { value: 'Un' } })
    await waitFor(() => screen.getByTestId('graph-walk-search-results'))
    fireEvent.click(screen.getByText(/United States/))
    await waitFor(() =>
      expect(screen.getByTestId('graph-walk-hostile-relation')).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByTestId('graph-chip-cooccurrence'))

    await waitFor(() =>
      expect(screen.queryByTestId('graph-walk-hostile-relation')).not.toBeInTheDocument(),
    )
    // the facets still report them as existing — that is the `hidden` line's
    // job, not a chip sitting under a canvas that draws none of them
    expect(screen.getByTestId('graph-walk-disclosure')).toBeInTheDocument()
  })
})

describe('GraphWalk — a missing actor fails loud', () => {
  it('renders the 404 as an explicit statement, not an empty canvas', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        '/entities?q=': { data: [{ id: US, canonical_name: 'Ghost', entity_class: 'country' }] },
        '/graph/ego': { __status: 404, detail: 'no entity profile' },
      }),
    )
    render(panel())
    fireEvent.change(screen.getByTestId('graph-walk-search'), { target: { value: 'Gh' } })
    await waitFor(() => screen.getByTestId('graph-walk-search-results'))
    fireEvent.click(screen.getByText(/Ghost/))

    await waitFor(() =>
      expect(screen.getByTestId('graph-walk-error')).toHaveTextContent(
        'no such actor in the entity store',
      ),
    )
  })
})

describe('GraphWalk — edge evidence', () => {
  const evidence = {
    edge: egoBody().edges[0],
    src: egoBody().anchor,
    dst: egoBody().nodes[0],
    evidence_available: true,
    detail: '',
    evidence_text: 'US strikes reported near Darkhovin',
    signals: [
      {
        id: 's1',
        title: 'Strikes reported',
        url: 'https://example.test/a',
        source_id: 'source.example',
        fetched_at: null,
        language: 'en',
      },
    ],
    unresolved_signal_ids: [],
    signal_count: 1,
    promoted_from_proposed_edge: null,
    derived_from: [],
    analyst_id: 'proposed_edge_governance',
    run_id: null,
    produced_at: null,
  }

  async function openEvidence(body: Record<string, unknown>) {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        '/entities?q=': { data: [{ id: US, canonical_name: 'United States', entity_class: 'country' }] },
        '/graph/edge/': body,
        '/graph/ego': egoBody(),
      }),
    )
    render(panel())
    fireEvent.change(screen.getByTestId('graph-walk-search'), { target: { value: 'Un' } })
    await waitFor(() => screen.getByTestId('graph-walk-search-results'))
    fireEvent.click(screen.getByText(/United States/))
    await waitFor(() => expect(screen.getByTestId('cytoscape-mock')).toBeInTheDocument())
  }

  it('projects a clickable edge carrying its evidence flag', async () => {
    await openEvidence(evidence)
    const edgeEl = (lastElements.current as { data: Record<string, unknown> }[]).find(
      (e) => e.data.id === 'e1',
    )!
    expect(edgeEl.data.has_evidence).toBe(true)
    expect(edgeEl.data.edge_family).toBe('relation')
  })
})

describe('GraphWalk — evidence body honesty', () => {
  function ev(over: Partial<EdgeEvidence>): EdgeEvidence {
    return {
      edge: egoBody().edges[0] as EdgeEvidence['edge'],
      src: egoBody().anchor as EdgeEvidence['src'],
      dst: egoBody().nodes[0] as EdgeEvidence['dst'],
      evidence_available: true,
      detail: '',
      evidence_text: '',
      signals: [],
      unresolved_signal_ids: [],
      signal_count: 0,
      promoted_from_proposed_edge: null,
      derived_from: [],
      analyst_id: null,
      run_id: null,
      produced_at: null,
      ...over,
    }
  }

  it('explains WHY a seed edge has no evidence instead of showing a blank box', () => {
    render(
      <EvidenceBody
        evidence={ev({
          evidence_available: false,
          detail:
            'this edge came from a curated seed import, which asserts the relationship without attaching a source document',
          analyst_id: 'seed.wikidata_leaders',
        })}
      />,
    )
    expect(screen.getByTestId('graph-walk-evidence-absent')).toHaveTextContent(
      'curated seed import',
    )
    expect(screen.queryByTestId('graph-walk-evidence-signals')).not.toBeInTheDocument()
  })

  it('flags referenced signals that no longer resolve', () => {
    render(
      <EvidenceBody
        evidence={ev({
          evidence_text: 'snippet',
          unresolved_signal_ids: ['a', 'b'],
          signal_count: 2,
        })}
      />,
    )
    expect(screen.getByTestId('graph-walk-evidence-unresolved')).toHaveTextContent(
      '2 referenced signals no longer resolve',
    )
  })

  it('renders the snippet and links its source signals', () => {
    render(
      <EvidenceBody
        evidence={ev({
          evidence_text: 'US strikes reported near Darkhovin',
          signal_count: 1,
          signals: [
            {
              id: 's1',
              title: 'Strikes reported',
              url: 'https://example.test/a',
              source_id: 'source.example',
              fetched_at: null,
              language: 'en',
            },
          ],
        })}
      />,
    )
    expect(screen.getByTestId('graph-walk-evidence-text')).toHaveTextContent('Darkhovin')
    expect(screen.getByText('Strikes reported')).toHaveAttribute(
      'href',
      'https://example.test/a',
    )
    expect(screen.queryByTestId('graph-walk-evidence-absent')).not.toBeInTheDocument()
  })
})
