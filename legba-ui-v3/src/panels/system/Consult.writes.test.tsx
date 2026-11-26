/**
 * C-2b-1 — "Allow changes this turn" in the Consult panel (decision D2).
 *
 *   * the toggle sits beside Send, is OFF by default and resets on every send;
 *   * a plain turn's request body carries no `allow_writes` key at all;
 *   * a settled armed turn renders its block (the stamp + the ledger's cards).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import ConsultPanel from './Consult'
import { useConsultSessions } from '@/state/consultSession'
import { resetCasePlayer } from '@/state/casePlayer'
import type { PanelRegistration } from '@/types'
import { openConsultMenu } from '@/test/consultMenu'

const PANEL = 'singleton:system.consult'

function reg(): PanelRegistration {
  return {
    id: PANEL,
    panel_id: 'system_consult',
    descriptor_id: '(singleton)',
    descriptor_version: '00000000',
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Consult',
    mode: 'personal',
    layout_slot: 'system.consult',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-10-04T00:00:00Z',
    retired_at: null,
  }
}

class FakeEventSource {
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  constructor(public url: string) {}
  addEventListener() {}
  close() {}
}

let fetchMock: ReturnType<typeof vi.fn>
let answer: (() => void) | null = null

function respond(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

beforeEach(() => {
  localStorage.clear()
  useConsultSessions.setState({ panels: {} })
  resetCasePlayer()
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  let n = 0
  vi.stubGlobal('crypto', { ...globalThis.crypto, randomUUID: () => `00000000-0000-0000-0000-0000000000a${++n}` })
  fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    if (String(url).endsWith('/consult') && init?.method === 'POST') {
      const sent = JSON.parse(init.body as string)
      if (!sent.allow_writes) return new Promise(() => {})
      return new Promise((resolve) => {
        answer = () =>
          resolve(
            respond({
              answer: 'Staged the pause.\n\nwrites: executed 0 · staged 1 · refused 0',
              finding_id: null, derived_from: [], tool_calls: [], cited_refs: [],
              session_id: '9f6c2b1e-0000-4000-8000-000000000001', status: 'complete',
              request_id: sent.request_id,
              writes: [{ write_id: 'w1', status: 'pending_confirmation', tool: 'transition_object' }],
            }),
          )
      })
    }
    if (String(url).includes('/consult/writes?')) {
      return Promise.resolve(respond({ writes: [{
        id: 'w1', session_id: '9f6c2b1e-0000-4000-8000-000000000001',
        request_id: '00000000-0000-0000-0000-0000000000a1', turn_index: 0, class: 'source',
        verb: 'transition', object_ref: 'source.feed', tier: 'B', args: { to_state: 'paused' },
        status: 'pending_confirmation', expires_at: '2099-01-01T00:15:00Z', confirmed_via: null,
        pre_image: { version: 'abcd1234', state: 'active' }, post_image: null,
        reversal: { kind: 'transition' }, reverses_write_id: null, reversed_by_write_id: null,
        activation: null, error: null, receipt: null, created_at: null, executed_at: null,
        confirm_code: 'K7QD',
      }], count: 1 }))
    }
    return new Promise(() => {})
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  answer = null
  vi.unstubAllGlobals()
})

function postBodies(): Record<string, unknown>[] {
  return fetchMock.mock.calls
    .filter(([url, init]) => String(url).endsWith('/consult') && (init as RequestInit)?.method === 'POST')
    .map(([, init]) => JSON.parse((init as RequestInit).body as string))
}

function ask(text: string) {
  fireEvent.change(screen.getByTestId('consult-question'), { target: { value: text } })
  fireEvent.click(screen.getByTestId('consult-submit'))
}

describe('"Allow changes this turn" (D2)', () => {
  it('is off by default, in the ⋯ menu, and a plain turn sends no allow_writes key', () => {
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    openConsultMenu()
    const toggle = screen.getByTestId('consult-allow-writes')
    expect(toggle.getAttribute('aria-pressed')).toBe('false')
    expect(screen.getByTestId('consult-more-menu').contains(toggle)).toBe(true)
    ask('what changed overnight?')
    expect(postBodies()).toHaveLength(1)
    expect('allow_writes' in postBodies()[0]).toBe(false)
  })

  it('arms ONE turn, resets, and the settled turn renders the pending card from the ledger', async () => {
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    openConsultMenu()
    fireEvent.click(screen.getByTestId('consult-allow-writes'))
    openConsultMenu()
    expect(screen.getByTestId('consult-allow-writes').getAttribute('aria-pressed')).toBe('true')
    ask('pause the feed')
    expect(postBodies()[0].allow_writes).toBe(true)
    openConsultMenu()
    expect(screen.getByTestId('consult-allow-writes').getAttribute('aria-pressed')).toBe('false')
    await waitFor(() => expect(answer).not.toBeNull())
    answer!()
    await waitFor(() => expect(screen.getByTestId('consult-write-pending')).toBeInTheDocument())
    expect(screen.getByTestId('consult-writes-armed')).toHaveTextContent('changes allowed')
    expect(screen.getByTestId('consult-write-pending')).toHaveTextContent('Pause source source.feed')
    expect(screen.getByTestId('consult-write-code')).toHaveTextContent('or type confirm K7QD')
    expect(screen.getByTestId('consult-answer')).toHaveTextContent('writes: executed 0 · staged 1 · refused 0')
  })
})
