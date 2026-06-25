/**
 * Component test for the UI-5 polished Dead-letter Inspector.
 *
 * Asserts:
 *  - renders entries from the mocked `/registry/dead_letter`
 *  - expanding an unresolved entry exposes the wired resubmit control
 *  - resubmit POSTs to the resubmit endpoint (with a valid inline patch)
 *  - an invalid patch JSON surfaces inline and does NOT POST
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import DeadLetterPanel from './DeadLetter'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'p',
    panel_id: 'system_dead_letter',
    descriptor_id: 'dlq.inspector',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'analyst',
    analyst_id: null,
    title: 'Dead-letter Inspector',
    mode: 'personal',
    layout_slot: 'system.dead_letter.main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-05-20T00:00:00Z',
    retired_at: null,
  }
}
function wrap(ui: ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

// Mirrors `DLQEntryOut` (the live `/registry/dead_letter` contract).
const PAGE = [
  {
    id: 'd1',
    attempted_at: '2026-06-03T00:00:00Z',
    actor: 'a'.repeat(64),
    namespace: 'descriptor',
    declared_schema_uri: 'legba/target/brazil/1.0.0',
    validation_error: {
      kind: 'schema',
      summary: 'schema validation failed: missing field foo',
    },
    resolution: null,
    attempted_payload: { kind: 'target', foo: null },
  },
]

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

function stubGet() {
  return vi.fn().mockResolvedValue({ ok: true, json: async () => PAGE })
}

describe('DeadLetterPanel', () => {
  it('renders DLQ entries', async () => {
    vi.stubGlobal('fetch', stubGet())
    render(wrap(<DeadLetterPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('dlq-row-d1')).toBeInTheDocument())
  })

  it('resubmits an entry with a valid inline patch', async () => {
    // First call = GET list; subsequent = POST resubmit.
    const fetchMock = vi.fn().mockImplementation((_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        return Promise.resolve({ ok: true, json: async () => ({ resubmitted: true }) })
      }
      return Promise.resolve({ ok: true, json: async () => PAGE })
    })
    vi.stubGlobal('fetch', fetchMock)

    render(wrap(<DeadLetterPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('dlq-row-d1')).toBeInTheDocument())

    // expand
    fireEvent.click(within(screen.getByTestId('dlq-row-d1')).getByText(/schema validation/))
    fireEvent.change(screen.getByTestId('dlq-patch-d1'), {
      target: { value: '{"foo": "fixed"}' },
    })
    fireEvent.click(screen.getByTestId('dlq-resubmit-d1'))

    await waitFor(() => {
      const post = fetchMock.mock.calls.find((c) => (c[1] as RequestInit)?.method === 'POST')
      expect(post).toBeTruthy()
      expect(String(post![0])).toContain('/registry/dead_letter/d1/resubmit')
      expect(String((post![1] as RequestInit).body)).toContain('"foo":"fixed"')
    })
  })

  it('rejects an invalid patch JSON inline without POSTing', async () => {
    const fetchMock = vi.fn().mockImplementation((_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') throw new Error('should not POST')
      return Promise.resolve({ ok: true, json: async () => PAGE })
    })
    vi.stubGlobal('fetch', fetchMock)

    render(wrap(<DeadLetterPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('dlq-row-d1')).toBeInTheDocument())
    fireEvent.click(within(screen.getByTestId('dlq-row-d1')).getByText(/schema validation/))
    fireEvent.change(screen.getByTestId('dlq-patch-d1'), { target: { value: '{not json' } })
    fireEvent.click(screen.getByTestId('dlq-resubmit-d1'))

    await waitFor(() => {
      expect(screen.getByTestId('dlq-error-d1')).toHaveTextContent(/invalid patch JSON/)
    })
    const posts = fetchMock.mock.calls.filter((c) => (c[1] as RequestInit)?.method === 'POST')
    expect(posts.length).toBe(0)
  })
})
