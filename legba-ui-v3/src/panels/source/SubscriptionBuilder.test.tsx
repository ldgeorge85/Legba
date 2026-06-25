/**
 * Component test for the subscription builder.
 *
 * ACCEPTANCE (UI-2): the builder validates a predicate + shows a preview.
 * Mocks GET /registry/sources + GET /signals at the HTTP boundary.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import SubscriptionBuilderPanel from './SubscriptionBuilder'
import type { PanelRegistration } from '@/types'
import type { SourceDescriptorOut } from './sourceTypes'

function reg(): PanelRegistration {
  return {
    id: 's1',
    panel_id: 'source_subscription_builder',
    descriptor_id: 'source.subscription_builder',
    descriptor_version: 'v'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Subscription Builder',
    mode: 'personal',
    layout_slot: 'main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-06-01T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const SOURCES: SourceDescriptorOut[] = [
  {
    descriptor_id: 'source.rss.brazil',
    version: 'a'.repeat(64),
    schema_uri: 'legba/source/1.0.0',
    is_head: true,
    state: 'active',
    owner: 'op',
    name: 'Brazil RSS',
    abstraction_level: 'L1',
    inherits: [],
    created_at: '2026-06-01T00:00:00Z',
    retire_after: null,
    kind: 'rss',
    acquisition: 'poll',
    subscription_policy: 'open',
    owner_tenant: 'default',
    geo: ['BR'],
    languages: ['pt'],
    tags: ['news'],
    has_discovery: false,
    has_provision: false,
    output_subject: 'source.x.signals',
    body: {},
  },
  {
    descriptor_id: 'source.gdelt.global',
    version: 'b'.repeat(64),
    schema_uri: 'legba/source/1.0.0',
    is_head: true,
    state: 'active',
    owner: 'op',
    name: 'GDELT',
    abstraction_level: 'L1',
    inherits: [],
    created_at: '2026-06-01T00:00:00Z',
    retire_after: null,
    kind: 'gdelt',
    acquisition: 'poll',
    subscription_policy: 'open',
    owner_tenant: 'default',
    geo: ['US'],
    languages: ['en'],
    tags: ['global'],
    has_discovery: false,
    has_provision: false,
    output_subject: 'source.y.signals',
    body: {},
  },
]

const SIGNALS = {
  data: [
    {
      id: 'sig-1',
      // data.geo is the GEOCODE object (live shape); coarse facets are top-level
      data: { geo: { country_iso2: 'BR', country: 'Brazil' } },
      title: 'Protest in Brasília',
      source_id: null,
      source_url: '',
      guid: 'g1',
      category: 'text',
      event_timestamp: null,
      language: 'pt',
      confidence: 0.8,
      classification_scores: null,
      target_id: null,
      analyst_id: null,
      produced_at: '2026-06-02T10:00:00Z',
      derived_from: [],
      schema_uri: 'legba/signal/1.0.0',
      descriptor_source_id: 'source.rss.brazil',
      geo: ['BR'],
      tags: ['protest'],
      entity_classes: ['location'],
    },
    {
      id: 'sig-2',
      data: { geo: { country_iso2: 'US', country: 'United States' } },
      title: 'US election news',
      source_id: null,
      source_url: '',
      guid: 'g2',
      category: 'text',
      event_timestamp: null,
      language: 'en',
      confidence: 0.6,
      classification_scores: null,
      target_id: null,
      analyst_id: null,
      produced_at: '2026-06-02T11:00:00Z',
      derived_from: [],
      schema_uri: 'legba/signal/1.0.0',
      descriptor_source_id: 'source.gdelt.global',
      geo: ['US'],
      tags: ['election'],
      entity_classes: ['org'],
    },
  ],
  next_cursor: null,
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

/** Route the mocked fetch by URL so both queries resolve. */
function mockFetch() {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url.includes('/signals')) {
      return Promise.resolve({ ok: true, json: async () => SIGNALS })
    }
    // by-id detail route (GET /registry/sources/{id}) → single object
    const m = url.match(/\/registry\/sources\/([^?]+)/)
    if (m) {
      const id = decodeURIComponent(m[1])
      const found = SOURCES.find((s) => s.descriptor_id === id)
      return found
        ? Promise.resolve({ ok: true, json: async () => found })
        : Promise.resolve({ ok: false, status: 404, json: async () => ({ detail: 'not found' }) })
    }
    // list route (GET /registry/sources?...) → array
    return Promise.resolve({ ok: true, json: async () => SOURCES })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** Pick a source from the (async-loaded) ScopePicker select by descriptor id.
 *  The explicit-mode source_id box is now a registry dropdown, so the option
 *  must land before we can select it. */
async function pickSource(value: string) {
  const sel = (await screen.findByTestId('subbuilder-source-id')) as HTMLSelectElement
  await waitFor(() => expect([...sel.options].some((o) => o.value === value)).toBe(true))
  fireEvent.change(sel, { target: { value } })
  await waitFor(() => expect(sel.value).toBe(value))
}

describe('SubscriptionBuilderPanel', () => {
  it('starts in explicit mode and is invalid until a source_id is entered', async () => {
    mockFetch()
    render(wrap(<SubscriptionBuilderPanel registration={reg()} scope={{}} mode="personal" />))
    // No source_id yet → an issue for source_id is shown.
    await waitFor(() => {
      expect(screen.getAllByTestId('subbuilder-issue').length).toBeGreaterThan(0)
    })
    expect(screen.getByTestId('subbuilder-copy')).toBeDisabled()
  })

  it('validates a bad Starlark residual and clears it when fixed', async () => {
    mockFetch()
    render(wrap(<SubscriptionBuilderPanel registration={reg()} scope={{}} mode="personal" />))
    await pickSource('source.rss.brazil')
    // Bad residual (assignment) → issue mentioning predicate.
    fireEvent.change(screen.getByTestId('subbuilder-sub-predicate'), {
      target: { value: 'x = 1' },
    })
    await waitFor(() => {
      const issues = screen.getAllByTestId('subbuilder-issue')
      expect(issues.some((el) => el.textContent?.includes('predicate'))).toBe(true)
    })
    // Fix it → valid.
    fireEvent.change(screen.getByTestId('subbuilder-sub-predicate'), {
      target: { value: 'severity_at_least("high")' },
    })
    await waitFor(() => {
      expect(screen.getByTestId('subbuilder-valid')).toBeInTheDocument()
    })
  })

  it('previews matching sources + signals for an explicit source', async () => {
    mockFetch()
    render(wrap(<SubscriptionBuilderPanel registration={reg()} scope={{}} mode="personal" />))
    await pickSource('source.rss.brazil')
    // The explicit source resolves into the matching-sources preview.
    await waitFor(() => {
      expect(screen.getByTestId('subbuilder-source-preview')).toHaveTextContent('source.rss.brazil')
    })
    // Signal preview shows match-rate; with no structured filter all scoped match.
    await waitFor(() => {
      expect(screen.getByTestId('subbuilder-match-rate')).toHaveTextContent(/\d+ \/ \d+ loaded signals match/)
    })
  })

  it('selector mode previews sources matching the structured filter', async () => {
    mockFetch()
    render(wrap(<SubscriptionBuilderPanel registration={reg()} scope={{}} mode="personal" />))
    fireEvent.click(screen.getByTestId('subbuilder-mode-selector'))
    fireEvent.change(screen.getByTestId('subbuilder-sel-kinds'), { target: { value: 'gdelt' } })
    await waitFor(() => {
      const preview = screen.getByTestId('subbuilder-source-preview')
      expect(preview).toHaveTextContent('source.gdelt.global')
      expect(preview).not.toHaveTextContent('source.rss.brazil')
    })
  })

  it('emits a SourceRef JSON with exactly one of source_id / selector', async () => {
    mockFetch()
    render(wrap(<SubscriptionBuilderPanel registration={reg()} scope={{}} mode="personal" />))
    await pickSource('source.rss.brazil')
    await waitFor(() => {
      const json = screen.getByTestId('subbuilder-json').textContent ?? ''
      const ref = JSON.parse(json)
      expect(ref.source_id).toBe('source.rss.brazil')
      expect(ref.source_selector).toBeNull()
      expect(ref.subscription).toBeTruthy()
    })
  })

  it('narrows the signal preview by structured subscription filter', async () => {
    mockFetch()
    render(wrap(<SubscriptionBuilderPanel registration={reg()} scope={{}} mode="personal" />))
    // No explicit source → all loaded signals are scoped; filter to geo=US.
    fireEvent.change(screen.getByTestId('subbuilder-sub-geo'), { target: { value: 'US' } })
    await waitFor(() => {
      const preview = screen.getByTestId('subbuilder-signal-preview')
      expect(preview).toHaveTextContent('US election news')
      expect(preview).not.toHaveTextContent('Protest in Brasília')
    })
  })
})
