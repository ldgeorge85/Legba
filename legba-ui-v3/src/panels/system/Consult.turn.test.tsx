/**
 * M-1 — the desktop Consult's TURN, held before the turn moved into
 * `state/useConsultTurn`. Written and passing against the inline code first;
 * the extraction must leave every one of these green unchanged.
 *
 *   * a 202 turn settles from the stream's terminal frame, contract included,
 *     and a cost stop on that contract reads as a partial answer;
 *   * a terminal ERROR frame lands its partial and the error banner;
 *   * the actor's bare final says it is waiting, the registry's final settles
 *     and closes the stream;
 *   * a POST that fails outright is recovered from the run status;
 *   * a POST that fails with nothing on the server fails the turn and says why;
 *   * STOP stops the run, then synthesises on the panel's model, then closes
 *     the stream and settles.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import ConsultPanel from './Consult'
import { useConsultSessions } from '@/state/consultSession'
import { resetScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { resetCasePlayer } from '@/state/casePlayer'
import type { PanelRegistration } from '@/types'

const PANEL = 'c-turn'
const REQUEST_ID = '00000000-0000-0000-0000-0000000000e1'

function reg(): PanelRegistration {
  return {
    id: PANEL,
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
  }
}

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
  addEventListener() {}
  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent)
  }
  close() {
    this.closed = true
  }
}

type Reply = { ok: boolean; status: number; json?: () => Promise<unknown>; text?: () => Promise<string> }
const ok = (status: number, body: unknown): Reply => ({ ok: true, status, json: async () => body })
const fail = (status: number, detail: string): Reply => ({
  ok: false,
  status,
  json: async () => ({ detail }),
  text: async () => JSON.stringify({ detail }),
})

function routed(routes: { post?: () => Reply; run?: () => Reply; stop?: () => Reply; synth?: () => Reply }) {
  return vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    const path = String(url)
    if (init?.method === 'POST' && path.endsWith('/stop')) return routes.stop?.() ?? ok(200, { stopped: true })
    if (init?.method === 'POST' && path.endsWith('/synthesize')) return routes.synth?.() ?? fail(500, 'no synth')
    if (init?.method === 'POST') return routes.post?.() ?? fail(500, 'no post')
    if (path.includes('/consult/runs/')) return routes.run?.() ?? fail(404, 'no run')
    if (path.includes('/consult/sessions/')) return fail(404, 'no session')
    return ok(200, [])
  })
}

const ACCEPTED = ok(202, {
  answer: '',
  status: 'accepted',
  request_id: REQUEST_ID,
  session_id: 'sess-t',
  finding_id: null,
  derived_from: [],
  tool_calls: [],
  cited_refs: [],
})

const CONTRACT = {
  schema: 'consult_contract/1',
  armed: true,
  window: { start: '2026-11-07T08:00:00+00:00', end: '2026-11-14T08:00:00+00:00', hours: 168, source: 'console', basis: 'console 168h', deictic: false },
  grounding_mode: 'default',
  cost_stop: "> **Stopped for cost:** drilling stopped after round 4 because the turn's cost ceiling ($10.00) leaves no room.",
}

function response(answer: string, extra: Record<string, unknown> = {}) {
  return { answer, finding_id: null, derived_from: [], tool_calls: [], cited_refs: [], session_id: 'sess-t', ...extra }
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  useConsultSessions.setState({ panels: {} })
  resetScope()
  resetCasePlayer()
  useSelection.getState().clear()
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  vi.stubGlobal('crypto', { ...globalThis.crypto, randomUUID: () => REQUEST_ID })
})

async function startTurn() {
  render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
  fireEvent.change(screen.getByTestId('consult-question'), { target: { value: 'what moved?' } })
  await act(async () => {
    fireEvent.click(screen.getByTestId('consult-submit'))
  })
  return FakeEventSource.instances[0]
}

describe('The desktop Consult turn (held before the extraction)', () => {
  it('a 202 turn settles from the terminal frame, and its contract and cost stop render', async () => {
    vi.stubGlobal('fetch', routed({ post: () => ACCEPTED }))
    const es = await startTurn()
    expect(es.url).toContain(`/consult/stream/${REQUEST_ID}`)
    expect(screen.getByTestId('consult-stop')).toBeInTheDocument()
    act(() => es.emit({ type: 'step', phase: 'act', kind: 'tool_call', tool: 'search_signals', round: 1 }))
    act(() => es.emit({ type: 'step', kind: 'answer_delta', text: 'Forming ' }))
    expect(screen.getByTestId('consult-answer-live')).toHaveTextContent('Forming')
    act(() => es.emit({ type: 'final', output_id: 'x' }))
    expect(es.closed).toBe(false)
    expect(screen.getByTestId('consult-live-steps').textContent).toMatch(/awaiting_answer/)
    act(() =>
      es.emit({
        type: 'final',
        final_source: 'registry',
        status: 'complete',
        response: response('the streamed answer', { contract: CONTRACT, model: 'core' }),
      }),
    )
    await waitFor(() => expect(screen.getByTestId('consult-answer')).toHaveTextContent('the streamed answer'))
    expect(es.closed).toBe(true)
    expect(screen.queryByTestId('consult-stop')).toBeNull()
    const block = screen.getByTestId('consult-contract')
    expect(within(block).getByTestId('consult-contract-partial').textContent).toMatch(/Partial answer/)
    expect(useConsultSessions.getState().panel(PANEL).sessionId).toBe('sess-t')
  })

  it('a terminal ERROR frame lands the partial and the error banner', async () => {
    vi.stubGlobal('fetch', routed({ post: () => ACCEPTED }))
    const es = await startTurn()
    act(() =>
      es.emit({
        type: 'final',
        final_source: 'registry',
        status: 'error',
        error_status: 504,
        error_detail: 'round 3 timed out',
        partial_answer: 'what it had gathered',
        steps: [{ type: 'step', kind: 'tool_call', tool: 'search_signals', round: 1 }],
      }),
    )
    await waitFor(() => expect(screen.getByTestId('consult-answer')).toHaveTextContent('what it had gathered'))
    expect(screen.getByTestId('consult-error')).toHaveTextContent('consult 504: round 3 timed out')
    expect(screen.getByTestId('consult-resynthesize')).toBeInTheDocument()
  })

  it('a POST that fails outright is recovered from the run status', async () => {
    vi.stubGlobal(
      'fetch',
      routed({
        post: () => fail(502, 'bad gateway'),
        run: () => ok(200, { request_id: REQUEST_ID, status: 'complete', steps: [], response: response('recovered answer') }),
      }),
    )
    const es = await startTurn()
    await waitFor(() => expect(screen.getByTestId('consult-answer')).toHaveTextContent('recovered answer'))
    expect(es.closed).toBe(true)
  })

  it('a POST that fails with nothing on the server fails the turn and says why', async () => {
    vi.stubGlobal('fetch', routed({ post: () => fail(422, 'question too long') }))
    const es = await startTurn()
    await waitFor(() => expect(screen.getByTestId('consult-error')).toHaveTextContent('consult 422: question too long'))
    expect(useConsultSessions.getState().panel(PANEL).pendingTurn).toBeNull()
    expect(screen.queryByTestId('consult-answer')).toBeNull()
    expect(es.closed).toBe(true)
  })

  it('STOP stops the run, then synthesises on the panel model, then settles', async () => {
    localStorage.setItem('legba_consult_model', 'opus')
    localStorage.setItem('legba_consult_model_chosen', '1') // an explicit pick
    const fetchMock = routed({
      post: () => ACCEPTED,
      synth: () => ok(200, { answer: 'salvaged answer', synthesis_status: 'complete', contract: CONTRACT }),
    })
    vi.stubGlobal('fetch', fetchMock)
    const es = await startTurn()
    await act(async () => {
      fireEvent.click(screen.getByTestId('consult-stop'))
    })
    await waitFor(() => expect(screen.getByTestId('consult-answer')).toHaveTextContent('salvaged answer'))
    const posts: [string, string][] = fetchMock.mock.calls
      .filter(([, init]) => (init as RequestInit)?.method === 'POST')
      .map(([u, init]) => [String(u), String((init as RequestInit).body)])
    expect(posts.map(([u]) => u.replace(/^.*\/api\/v1/, ''))).toEqual([
      '/consult',
      `/consult/runs/${REQUEST_ID}/stop`,
      `/consult/runs/${REQUEST_ID}/synthesize`,
    ])
    expect(JSON.parse(posts[2][1])).toEqual({ model: 'opus' })
    expect(es.closed).toBe(true)
    expect(screen.getByTestId('consult-contract')).toBeInTheDocument()
  })
})
