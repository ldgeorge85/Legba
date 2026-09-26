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
import { consultActions, useConsultSessions } from '@/state/consultSession'
import type { PanelRegistration } from '@/types'
import { resetScope, useScope } from '@/state/scope'
import { scopeFromTarget } from '@/lib/scopeFromReport'
import { selectRow, useSelection } from '@/state/selection'
import { SCOPE_PIN_ORIGIN } from '@/lib/consultContext'

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
  // The conversation now lives in a MODULE-level store (GLASS-4), so it
  // outlives a `render()` the way it outlives an unmount in the app. Clearing
  // localStorage no longer isolates these tests — the slices have to go too,
  // or each test inherits the previous one's transcript.
  useConsultSessions.setState({ panels: {} })
  resetScope()
  useSelection.getState().clear()
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

    // The ACTOR's bare `final` no longer closes the stream: it carries no
    // answer, and the registry still has to persist and publish one. Closing
    // here is what used to leave the panel with nowhere for the answer to
    // arrive from except the POST — the dependency the 2026-09-16 504 severed.
    act(() => {
      es.emit({ type: 'final', request_id: 'x', output_id: null, mode: 'chat' })
    })
    expect(es.closed).toBe(false)
    // ...and the operator is told what it is waiting for, rather than watching
    // a frozen ticker.
    await waitFor(() => {
      expect(screen.getByTestId('consult-live-steps')).toHaveTextContent('3 steps')
    })

    // Now let the POST resolve.
    await act(async () => {
      resolveFetch({
        ok: true,
        status: 200,
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
    expect(es.closed).toBe(true)
  })

  it('renders error text and preserves the question on failure', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      // `readErrorBody` reads the body ONCE via `res.text()` (api.ts) — the stub
      // must expose it, else the error path throws `res.text is not a function`.
      text: async () => JSON.stringify({ detail: 'kaboom' }),
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

describe('ConsultPanel (F1 model picker)', () => {
  it('renders the model dropdown defaulting to opus and sends model on submit', async () => {
    const fetchMock = answerOnce('ok')
    vi.stubGlobal('fetch', fetchMock)

    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    const select = screen.getByTestId('consult-model') as HTMLSelectElement
    expect(select.value).toBe('opus')

    fireEvent.change(screen.getByTestId('consult-question'), { target: { value: 'q' } })
    fireEvent.click(screen.getByTestId('consult-submit'))
    await waitFor(() => expect(screen.getByTestId('consult-answer')).toBeInTheDocument())

    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)
    expect(body.model).toBe('opus')
  })

  it('sends the chosen plane, persists it, and surfaces the answered plane', async () => {
    const fetchMock = answerOnce('ok', { model: 'core' })
    vi.stubGlobal('fetch', fetchMock)

    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    fireEvent.change(screen.getByTestId('consult-model'), { target: { value: 'core' } })
    expect(localStorage.getItem('legba_consult_model')).toBe('core')

    fireEvent.change(screen.getByTestId('consult-question'), { target: { value: 'q' } })
    fireEvent.click(screen.getByTestId('consult-submit'))
    await waitFor(() => expect(screen.getByTestId('consult-answer')).toBeInTheDocument())

    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)
    expect(body.model).toBe('core')
    // The plane that answered surfaces on the assistant turn.
    expect(screen.getByTestId('consult-answer-model')).toHaveTextContent('via core')
  })

  it('restores the persisted plane on mount', () => {
    localStorage.setItem('legba_consult_model', 'core')
    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    expect((screen.getByTestId('consult-model') as HTMLSelectElement).value).toBe('core')
  })

  it('offers Fable 5.1 and sends/persists/echoes it like the other planes', async () => {
    const fetchMock = answerOnce('ok', { model: 'fable' })
    vi.stubGlobal('fetch', fetchMock)

    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    fireEvent.change(screen.getByTestId('consult-model'), { target: { value: 'fable' } })
    expect(localStorage.getItem('legba_consult_model')).toBe('fable')

    fireEvent.change(screen.getByTestId('consult-question'), { target: { value: 'q' } })
    fireEvent.click(screen.getByTestId('consult-submit'))
    await waitFor(() => expect(screen.getByTestId('consult-answer')).toBeInTheDocument())

    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)
    expect(body.model).toBe('fable')
    expect(screen.getByTestId('consult-answer-model')).toHaveTextContent('via fable')
  })

  it('restores a persisted fable choice on mount', () => {
    localStorage.setItem('legba_consult_model', 'fable')
    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    expect((screen.getByTestId('consult-model') as HTMLSelectElement).value).toBe('fable')
  })

  it('falls back to opus for a garbage / stale stored value', () => {
    localStorage.setItem('legba_consult_model', 'some-old-raw-component-id')
    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    expect((screen.getByTestId('consult-model') as HTMLSelectElement).value).toBe('opus')
  })
})

/**
 * Consult at the centre (WORKSTATION_V2_FLOW_DESIGN §5).
 *
 * The model used to be answering questions about "the world" while the operator
 * was looking at one region's read, and neither of them said so. The scope pin
 * is that missing sentence — ONE ambient pin, replaced rather than appended,
 * beside whatever the operator pinned by hand.
 */
describe('ConsultPanel — scope auto-pins, focus pins by hand', () => {
  it('auto-pins the scope, tagged so it can be told from a manual pin', async () => {
    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    act(() => useScope.getState().setScope(scopeFromTarget('country_g20_br', 'Brazil')))

    await waitFor(() => expect(consultActions().panel('c1').pins.length).toBe(1))
    const pin = consultActions().panel('c1').pins[0]
    expect(pin.id).toBe('country_g20_br')
    expect(pin.origin).toBe(SCOPE_PIN_ORIGIN)
  })

  it('REPLACES the scope pin on every scope change — never a stack of stale scopes', async () => {
    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    act(() => useScope.getState().setScope(scopeFromTarget('a', 'A')))
    await waitFor(() => expect(consultActions().panel('c1').pins.length).toBe(1))
    act(() => useScope.getState().setScope(scopeFromTarget('b', 'B')))
    await waitFor(() => expect(consultActions().panel('c1').pins[0].id).toBe('b'))
    expect(consultActions().panel('c1').pins.length).toBe(1)
  })

  it('clearing the scope removes the pin and pins nothing in its place', async () => {
    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    act(() => useScope.getState().setScope(scopeFromTarget('a', 'A')))
    await waitFor(() => expect(consultActions().panel('c1').pins.length).toBe(1))
    act(() => useScope.getState().clear())
    await waitFor(() => expect(consultActions().panel('c1').pins.length).toBe(0))
  })

  it('the manual pin still pins FOCUS — three findings under one report scope', async () => {
    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    act(() => useScope.getState().setScope(scopeFromTarget('country_g20_br', 'Brazil')))
    await waitFor(() => expect(consultActions().panel('c1').pins.length).toBe(1))

    for (const id of ['f1', 'f2', 'f3']) {
      act(() => selectRow('finding', id, id))
      fireEvent.click(screen.getByTestId('consult-pin'))
    }

    const pins = consultActions().panel('c1').pins
    expect(pins.map((p) => p.id)).toEqual(['country_g20_br', 'f1', 'f2', 'f3'])
    // …and the scope survived all three row clicks.
    expect(useScope.getState().scope?.id).toBe('country_g20_br')
  })

  it('sends both channels: the [Pinned …] prefix AND structured pinned_context', async () => {
    const fetchMock = answerOnce('ok')
    vi.stubGlobal('fetch', fetchMock)

    render(wrap(<ConsultPanel registration={reg()} scope={{}} mode="personal" />))
    act(() => useScope.getState().setScope(scopeFromTarget('country_g20_br', 'Brazil')))
    await waitFor(() => expect(consultActions().panel('c1').pins.length).toBe(1))

    fireEvent.change(screen.getByTestId('consult-question'), { target: { value: 'what moved?' } })
    fireEvent.click(screen.getByTestId('consult-submit'))
    await waitFor(() => expect(screen.getByTestId('consult-answer')).toBeInTheDocument())

    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)
    // Channel 1 — works against today's text-only backend.
    expect(body.question).toContain('[Pinned report: "Brazil"')
    expect(body.question).toContain('what moved?')
    // Channel 2 — `{kind, id, title, text}`, for a backend that hydrates.
    expect(body.pinned_context).toEqual([
      {
        kind: 'report',
        id: 'country_g20_br',
        title: 'Brazil',
        text: expect.stringContaining('scoped to this target'),
      },
    ])
  })
})
