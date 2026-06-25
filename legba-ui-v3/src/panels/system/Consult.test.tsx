/**
 * Component test for the daily-driver Consult chat panel (Piece 1 rework).
 *
 * Asserts:
 *  - The question textarea + scope input + Send button render.
 *  - Submitting POSTs to `/api/v1/consult` with mode='chat', a request_id, and
 *    the client-held transcript as messages[]; default max_tool_rounds is 10.
 *  - The answer renders into the transcript; a second submit re-sends the first
 *    turn in messages[].
 *  - EventSource `step` frames grow the live-steps list; `final` closes it.
 *  - An error response renders inline without losing the question.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import type { ReactElement } from 'react'
import ConsultPanel from './Consult'
import type { PanelRegistration } from '@/types'

function reg(overrides: Partial<PanelRegistration> = {}): PanelRegistration {
  return {
    id: 'c1',
    panel_id: 'system_consult',
    descriptor_id: 'consult.daily',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'analyst',
    analyst_id: 'consult.daily',
    title: 'Consult',
    mode: 'personal',
    layout_slot: 'system.consult.main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-05-20T00:00:00Z',
    retired_at: null,
    ...overrides,
  }
}

function wrap(ui: ReactElement) {
  return ui
}

// --- Minimal EventSource stub -------------------------------------------
// Captures instances so a test can push frames, and records the URL so we can
// assert the request_id + token wiring on the stream subscription.
class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  closed = false
  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }
  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent)
  }
  close() {
    this.closed = true
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  // Deterministic request id.
  vi.stubGlobal('crypto', {
    ...globalThis.crypto,
    randomUUID: () => '00000000-0000-0000-0000-000000000abc',
  })
})

function answerOnce(answer: string, extra: Record<string, unknown> = {}) {
  return vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      answer,
      finding_id: null,
      derived_from: [],
      tool_calls: [],
      cited_refs: [],
      ...extra,
    }),
  })
}

describe('ConsultPanel (chat)', () => {
  it('renders the composer; Send disabled until typed', () => {
    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    expect(screen.getByTestId('consult-question')).toBeInTheDocument()
    expect(screen.getByTestId('consult-scope')).toBeInTheDocument()
    expect(screen.getByTestId('consult-submit')).toBeDisabled()
  })

  it('defaults max_tool_rounds to 10', () => {
    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    expect(screen.getByTestId('consult-max-rounds')).toHaveValue(10)
  })

  it('POSTs chat with request_id + messages, renders the answer transcript', async () => {
    const fetchMock = answerOnce('Brazil credibility is **stable**.')
    vi.stubGlobal('fetch', fetchMock)

    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    fireEvent.change(screen.getByTestId('consult-question'), {
      target: { value: 'check creds' },
    })
    fireEvent.click(screen.getByTestId('consult-submit'))

    await waitFor(() => {
      expect(screen.getByTestId('consult-answer')).toBeInTheDocument()
    })
    expect(screen.getByTestId('consult-answer')).toHaveTextContent('Brazil credibility')

    const init = fetchMock.mock.calls[0][1] as RequestInit
    const body = JSON.parse(init.body as string)
    expect(body).toMatchObject({
      question: 'check creds',
      scope_predicate: null,
      max_tool_rounds: 10,
      mode: 'chat',
      request_id: '00000000-0000-0000-0000-000000000abc',
      messages: [],
    })
  })

  it('re-sends the first turn in messages[] on the second submit', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          answer: 'first answer',
          finding_id: null,
          derived_from: [],
          tool_calls: [],
          cited_refs: [],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          answer: 'second answer',
          finding_id: null,
          derived_from: [],
          tool_calls: [],
          cited_refs: [],
        }),
      })
    vi.stubGlobal('fetch', fetchMock)

    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    fireEvent.change(screen.getByTestId('consult-question'), { target: { value: 'q1' } })
    fireEvent.click(screen.getByTestId('consult-submit'))
    await waitFor(() => expect(screen.getByText('first answer')).toBeInTheDocument())

    fireEvent.change(screen.getByTestId('consult-question'), { target: { value: 'q2' } })
    fireEvent.click(screen.getByTestId('consult-submit'))
    await waitFor(() => expect(screen.getByText('second answer')).toBeInTheDocument())

    const secondInit = fetchMock.mock.calls[1][1] as RequestInit
    const body = JSON.parse(secondInit.body as string)
    expect(body.messages).toEqual([
      { role: 'user', content: 'q1' },
      { role: 'assistant', content: 'first answer' },
    ])
  })

  it('subscribes EventSource before POST; step frames grow live steps', async () => {
    let resolveFetch: (v: unknown) => void = () => {}
    const fetchMock = vi.fn().mockImplementation(
      () =>
        new Promise((res) => {
          resolveFetch = res
        }),
    )
    vi.stubGlobal('fetch', fetchMock)
    localStorage.setItem('legba_token', 'tok123')

    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    fireEvent.change(screen.getByTestId('consult-question'), { target: { value: 'go' } })
    fireEvent.click(screen.getByTestId('consult-submit'))

    // EventSource opened with the request id + token before the POST resolves.
    expect(FakeEventSource.instances).toHaveLength(1)
    const es = FakeEventSource.instances[0]
    expect(es.url).toContain('/api/v1/consult/stream/00000000-0000-0000-0000-000000000abc')
    expect(es.url).toContain('token=tok123')

    act(() => {
      es.emit({ type: 'step', phase: 'plan', kind: 'render_prompt' })
      es.emit({ type: 'step', phase: 'act', kind: 'tool_call', tool: 'search_signals', round: 1 })
    })
    await waitFor(() => {
      expect(screen.getByTestId('consult-live-steps')).toHaveTextContent('Thinking… (2 steps)')
    })

    // final closes the stream.
    act(() => {
      es.emit({ type: 'final', request_id: 'x', output_id: null, mode: 'chat' })
    })
    expect(es.closed).toBe(true)

    // Now let the POST resolve.
    await act(async () => {
      resolveFetch({
        ok: true,
        json: async () => ({
          answer: 'done',
          finding_id: null,
          derived_from: [],
          tool_calls: [],
          cited_refs: [],
        }),
      })
    })
    await waitFor(() => expect(screen.getByText('done')).toBeInTheDocument())
  })

  it('renders error text and preserves the question on failure', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({ detail: 'kaboom' }),
    })
    vi.stubGlobal('fetch', fetchMock)

    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    fireEvent.change(screen.getByTestId('consult-question'), {
      target: { value: 'will this work' },
    })
    fireEvent.click(screen.getByTestId('consult-submit'))

    await waitFor(() => {
      expect(screen.getByTestId('consult-error')).toBeInTheDocument()
    })
    expect(screen.getByTestId('consult-error')).toHaveTextContent(/500/)
    // The user turn is appended optimistically; the composer clears on submit.
    expect(screen.getByTestId('consult-turn-user')).toHaveTextContent('will this work')
  })
})
