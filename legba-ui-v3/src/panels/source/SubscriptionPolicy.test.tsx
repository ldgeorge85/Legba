/**
 * Component test for the subscription-policy LOCKING panel.
 *
 * ACCEPTANCE (UI-2): the panel loads a source's policy, lets the operator flip
 * it + edit the allowlist, surfaces which registered targets would be REFUSED a
 * subscription (with the policy.py reason), and saves via PUT. Mocks the
 * registry GET/PUT endpoints at the HTTP boundary (per the source-panel test
 * convention).
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import SubscriptionPolicyPanel from './SubscriptionPolicy'
import type { PanelRegistration } from '@/types'
import type { SourceDescriptorOut } from './sourceTypes'

function reg(): PanelRegistration {
  return {
    id: 'sp1',
    panel_id: 'source_subscription_policy',
    descriptor_id: 'source.subscription_policy',
    descriptor_version: 'v'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Subscription Policy',
    mode: 'personal',
    layout_slot: 'main',
    data_query: { source_id: 'source.rss.brazil' },
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

const SOURCE: SourceDescriptorOut = {
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
  output_subject: 'source.rss.brazil.signals',
  body: {
    identity: { id: 'source.rss.brazil', version: 'a'.repeat(64), state: 'active' },
    scope: { owner_tenant: 'default', geo: ['BR'], languages: ['pt'], tags: ['news'] },
    subscription_policy: 'open',
    allowed_targets: [],
    allowed_tenants: [],
  },
}

// Two targets: one default-tenant, one acme-tenant (via scope.owner_tenant).
const TARGETS = [
  {
    descriptor_id: 'target.brazil.osint',
    name: 'Brazil OSINT',
    state: 'active',
    body: { scope: { owner_tenant: 'default' } },
  },
  {
    descriptor_id: 'target.acme.estate',
    name: 'Acme Estate',
    state: 'active',
    body: { scope: { owner_tenant: 'acme' } },
  },
]

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

/** Route the mocked fetch by URL + method so every query/mutation resolves. */
function mockFetch(putCapture?: (body: unknown) => void) {
  const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    // PUT source body (save)
    if (init?.method === 'PUT') {
      putCapture?.(JSON.parse(String(init.body)))
      return Promise.resolve({ ok: true, json: async () => ({ version: 'b'.repeat(64) }) })
    }
    // targets list (must check BEFORE the generic descriptors match)
    if (url.includes('/registry/descriptors') && url.includes('family=target')) {
      return Promise.resolve({ ok: true, json: async () => TARGETS })
    }
    // ScopePicker's source list (GET /registry/descriptors?family=source)
    if (url.includes('/registry/descriptors') && url.includes('family=source')) {
      return Promise.resolve({
        ok: true,
        json: async () => [{ descriptor_id: SOURCE.descriptor_id, name: SOURCE.name, state: 'active' }],
      })
    }
    // by-id source detail (GET /registry/sources/{id})
    if (/\/registry\/sources\//.test(url)) {
      return Promise.resolve({ ok: true, json: async () => SOURCE })
    }
    return Promise.resolve({ ok: true, json: async () => [] })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('SubscriptionPolicyPanel', () => {
  it('loads the source policy and evaluates registered targets', async () => {
    mockFetch()
    render(wrap(<SubscriptionPolicyPanel registration={reg()} scope={{}} mode="personal" />))

    // policy body renders once the source loads
    await waitFor(() => expect(screen.getByTestId('policy-body')).toBeInTheDocument())

    // open policy: same-tenant target allowed, cross-tenant (acme) refused
    await waitFor(() => {
      expect(screen.getByTestId('policy-decision-target.brazil.osint')).toHaveTextContent('ALLOWED')
      expect(screen.getByTestId('policy-decision-target.acme.estate')).toHaveTextContent('REFUSED')
    })
    expect(screen.getByTestId('policy-allowed-count')).toHaveTextContent('1 allowed')
    expect(screen.getByTestId('policy-refused-count')).toHaveTextContent('1 refused')
    // and the refusal explains why (cross-tenant)
    expect(screen.getByTestId('policy-decision-target.acme.estate')).toHaveTextContent('cross-tenant')
  })

  it('flipping to allowlist refuses every target until one is allow-listed', async () => {
    mockFetch()
    render(wrap(<SubscriptionPolicyPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('policy-body')).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('policy-set-allowlist'))
    // nothing listed yet → both refused
    await waitFor(() =>
      expect(screen.getByTestId('policy-refused-count')).toHaveTextContent('2 refused'),
    )

    // allow-list one target → it flips to ALLOWED
    fireEvent.change(screen.getByTestId('policy-allowed-targets'), {
      target: { value: 'target.brazil.osint' },
    })
    await waitFor(() => {
      expect(screen.getByTestId('policy-decision-target.brazil.osint')).toHaveTextContent('ALLOWED')
      expect(screen.getByTestId('policy-allowed-count')).toHaveTextContent('1 allowed')
    })
  })

  it('grant policy refuses every target and surfaces the grant wiring id; "treat as granted" allows it', async () => {
    mockFetch()
    render(wrap(<SubscriptionPolicyPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('policy-body')).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('policy-set-grant'))
    await waitFor(() =>
      expect(screen.getByTestId('policy-refused-count')).toHaveTextContent('2 refused'),
    )
    // the stable grant id is shown for a refused target
    const row = screen.getByTestId('policy-decision-target.brazil.osint')
    expect(row).toHaveTextContent('subgrant.source_rss_brazil.target.brazil.osint')

    // mark it granted → it flips to ALLOWED + a copy-ready grant body appears
    fireEvent.click(screen.getByTestId('policy-grant-target.brazil.osint'))
    await waitFor(() => {
      expect(screen.getByTestId('policy-decision-target.brazil.osint')).toHaveTextContent('ALLOWED')
    })
    expect(screen.getByTestId('policy-grant-json')).toHaveTextContent('subscription_grant')
  })

  it('saves the patched body via PUT (policy + allowlist), preserving other body fields', async () => {
    let captured: unknown = null
    mockFetch((b) => {
      captured = b
    })
    render(wrap(<SubscriptionPolicyPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('policy-body')).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('policy-set-allowlist'))
    fireEvent.change(screen.getByTestId('policy-allowed-targets'), {
      target: { value: 'target.brazil.osint\ntarget.acme.estate' },
    })
    fireEvent.click(screen.getByTestId('policy-save'))

    await waitFor(() => expect(screen.getByTestId('policy-saved')).toBeInTheDocument())
    const body = captured as Record<string, unknown>
    expect(body.subscription_policy).toBe('allowlist')
    expect(body.allowed_targets).toEqual(['target.brazil.osint', 'target.acme.estate'])
    // unrelated body fields survive the patch (scope preserved)
    expect((body.scope as Record<string, unknown>).geo).toEqual(['BR'])
    // a real content-hash version passes through untouched — the registry
    // re-stamps a fresh content hash server-side on update().
    expect((body.identity as Record<string, unknown>).version).toBe('a'.repeat(64))
  })

  it('re-stamps a placeholder identity.version to the sentinel before PUT', async () => {
    let captured: unknown = null
    // a source whose body carries a not-yet-stamped (short/zero) version
    const draftSource = {
      ...SOURCE,
      body: {
        ...SOURCE.body,
        identity: { id: 'source.rss.brazil', version: '0', state: 'active' },
      },
    }
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (init?.method === 'PUT') {
        captured = JSON.parse(String(init.body))
        return Promise.resolve({ ok: true, json: async () => ({ version: 'b'.repeat(64) }) })
      }
      if (url.includes('/registry/descriptors') && url.includes('family=target')) {
        return Promise.resolve({ ok: true, json: async () => TARGETS })
      }
      if (/\/registry\/sources\//.test(url)) {
        return Promise.resolve({ ok: true, json: async () => draftSource })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    })
    vi.stubGlobal('fetch', fetchMock)

    render(wrap(<SubscriptionPolicyPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('policy-body')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('policy-set-grant'))
    fireEvent.click(screen.getByTestId('policy-save'))

    await waitFor(() => expect(screen.getByTestId('policy-saved')).toBeInTheDocument())
    const body = captured as Record<string, unknown>
    expect((body.identity as Record<string, unknown>).version).toBe('0'.repeat(16))
  })
})
