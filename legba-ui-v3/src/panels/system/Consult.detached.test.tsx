/**
 * The Consult panel against a DETACHED run — 202, the stream, the reconnect.
 *
 * Why these tests and not more of the same
 * ========================================
 *
 * On 2026-09-16 a real consult ran past the front door's 300s timeout. The
 * registry gave up on the invoke, returned 504, and — because a chat consult's
 * answer exists only in that response — the answer was lost. The server side of
 * the fix is a run that outlives the request; THIS side of it is a panel that
 * never lets the operator see the difference.
 *
 * That last part is a promise with teeth, and it is what these tests hold:
 *
 *   1. **The 202 is invisible.** The stream opened before the POST, and it keeps
 *      rendering rounds while the run continues. Nothing in the panel says
 *      "accepted", nothing pauses, no spinner replaces the ticker.
 *   2. **Every round is visible while it happens** — including the ones that
 *      went wrong. A round the planner wasted (`unparseable`) renders as a step
 *      WITH ITS REASON, because that step is the whole explanation for a
 *      disappointing answer.
 *   3. **A failure shows the partial, never a bare status code.** What it
 *      gathered, which round it died on, and why.
 *   4. **A reload mid-run re-attaches.** The run knows its own steps, so the
 *      panel seeds what it missed, re-opens the stream, and the operator
 *      watches the rest arrive instead of a frozen "Consulting…".
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import ConsultPanel from './Consult'
import { useConsultSessions, CONSULT_PAGE_LOAD_ID } from '@/state/consultSession'
import type { PanelRegistration } from '@/types'
import { resetScope } from '@/state/scope'
import { useSelection } from '@/state/selection'

const REQUEST_ID = '00000000-0000-0000-0000-0000000000d7'

function reg(): PanelRegistration {
  return {
    id: 'c-detached',
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
  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent)
  }
  close() {
    this.closed = true
  }
}

/** Route a stubbed fetch by path, so one mock serves POST + both GETs. */
function routedFetch(routes: {
  post?: () => Promise<unknown> | unknown
  run?: () => Promise<unknown> | unknown
  session?: () => Promise<unknown> | unknown
}) {
  return vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    const path = String(url)
    if (init?.method === 'POST') return routes.post?.()
    if (path.includes('/consult/runs/')) {
      return (
        routes.run?.() ?? { ok: false, status: 404, text: async () => 'no run' }
      )
    }
    if (path.includes('/consult/sessions/')) {
      return (
        routes.session?.() ?? {
          ok: false,
          status: 404,
          text: async () => 'no session',
        }
      )
    }
    return { ok: true, status: 200, json: async () => [] }
  })
}

const ACCEPTED = {
  ok: true,
  status: 202,
  json: async () => ({
    answer: '',
    status: 'accepted',
    request_id: REQUEST_ID,
    session_id: 'sess-1',
    finding_id: null,
    derived_from: [],
    tool_calls: [],
    cited_refs: [],
  }),
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  useConsultSessions.setState({ panels: {} })
  resetScope()
  useSelection.getState().clear()
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  vi.stubGlobal('crypto', {
    ...globalThis.crypto,
    randomUUID: () => REQUEST_ID,
  })
})

async function startTurn() {
  render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
  fireEvent.change(screen.getByTestId('consult-question'), {
    target: { value: 'assess the slope' },
  })
  await act(async () => {
    fireEvent.click(screen.getByTestId('consult-submit'))
  })
  return FakeEventSource.instances[0]
}

describe('ConsultPanel — detached run', () => {
  it('renders three round events then a failure, all of them', async () => {
    // The operator's contract, stated as one test: a run that emits rounds and
    // then dies must show the ROUNDS and the FAILURE — not a blank turn, and
    // not a bare 504 in place of the work that did happen.
    vi.stubGlobal('fetch', routedFetch({ post: async () => ACCEPTED }))
    const es = await startTurn()

    act(() => {
      es.emit({ type: 'step', phase: 'plan', kind: 'render_prompt' })
      es.emit({
        type: 'step',
        phase: 'act',
        kind: 'tool_call',
        tool: 'search_signals',
        round: 1,
      })
      es.emit({
        type: 'step',
        phase: 'reflect',
        kind: 'unparseable',
        round: 2,
        raw: '{"tool": "search_signals" ... and then some prose',
      })
    })

    // All three are on the live ticker WHILE the run is still going — the 202
    // changed nothing the operator can see.
    await waitFor(() => {
      expect(screen.getByTestId('consult-live-steps')).toHaveTextContent(
        'Thinking… (3 steps)',
      )
    })
    expect(screen.queryByText(/accepted/i)).not.toBeInTheDocument()

    // The wasted round is legible: kind AND reason.
    fireEvent.click(screen.getByTestId('consult-live-steps'))
    await waitFor(() => {
      expect(screen.getByText(/unparseable/)).toBeInTheDocument()
    })
    expect(
      screen.getByText(/matched neither reply shape/),
    ).toBeInTheDocument()

    // Then the run fails. The terminal frame carries the partial.
    act(() => {
      es.emit({
        type: 'final',
        final_source: 'registry',
        status: 'error',
        error_status: 504,
        error_detail: 'the consult actor did not return within 600s',
        partial_answer:
          '_This consult did not finish._\n\nIt ran 2 round(s) and completed 1 tool call(s) before it stopped at round 2.',
        steps: [
          { type: 'step', kind: 'tool_call', tool: 'search_signals', round: 1 },
        ],
      })
    })

    // The partial is rendered AS A TURN...
    await waitFor(() => {
      expect(screen.getByText(/This consult did not finish/)).toBeInTheDocument()
    })
    expect(screen.getByText(/stopped at round 2/)).toBeInTheDocument()
    // ...and the reason sits beside it rather than replacing it.
    await waitFor(() => {
      expect(screen.getByTestId('consult-error')).toHaveTextContent('504')
    })
    expect(es.closed).toBe(true)
  })

  it('renders the answer from the terminal frame when the POST returned 202', async () => {
    vi.stubGlobal('fetch', routedFetch({ post: async () => ACCEPTED }))
    const es = await startTurn()

    act(() => {
      es.emit({ type: 'step', phase: 'act', kind: 'tool_call', round: 1 })
    })
    await waitFor(() =>
      expect(screen.getByTestId('consult-live-steps')).toBeInTheDocument(),
    )

    act(() => {
      es.emit({
        type: 'final',
        final_source: 'registry',
        status: 'complete',
        session_id: 'sess-1',
        response: {
          answer: 'the slope is **flattening**',
          finding_id: null,
          derived_from: [],
          tool_calls: [],
          cited_refs: [],
          session_id: 'sess-1',
          model: 'opus',
        },
      })
    })

    await waitFor(() =>
      expect(screen.getByText(/the slope is/)).toBeInTheDocument(),
    )
    expect(es.closed).toBe(true)
  })

  it('re-attaches to a live run after a reload, then settles on its terminal frame', async () => {
    // A turn stamped with a DIFFERENT page load is what a reload leaves behind:
    // its fetch died with the old page, so nothing local will ever resolve it.
    useConsultSessions.setState({
      panels: {
        'c-detached': {
          transcript: [
            { role: 'user', content: 'assess the slope' },
          ],
          pendingTurn: {
            requestId: REQUEST_ID,
            question: 'assess the slope',
            mode: 'chat',
            startedAt: Date.now() - 5_000,
            pageLoadId: `${CONSULT_PAGE_LOAD_ID}-previous`,
            steps: [],
          },
          sessionId: 'sess-1',
          draft: '',
          pins: [],
          scope: '',
          maxRounds: 10,
          error: null,
          rev: 1,
          updatedAt: Date.now(),
        },
      },
    })

    vi.stubGlobal(
      'fetch',
      routedFetch({
        run: async () => ({
          ok: true,
          status: 200,
          json: async () => ({
            request_id: REQUEST_ID,
            session_id: 'sess-1',
            status: 'running',
            elapsed_s: 5,
            // The steps the reload missed — the run kept them precisely so a
            // reconnect is not a blank slate.
            steps: [
              { kind: 'tool_call', tool: 'search_signals', round: 1 },
              { kind: 'llm_call', round: 2 },
            ],
          }),
        }),
        session: async () => ({
          ok: true,
          status: 200,
          json: async () => ({
            id: 'sess-1',
            mode: 'chat',
            title: 'assess the slope',
            turns: [],
          }),
        }),
      }),
    )

    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)

    // The missed steps are seeded and the stream is re-opened — the run is
    // live, so the panel watches the rest of it rather than polling blind.
    await waitFor(() => {
      expect(screen.getByTestId('consult-live-steps')).toHaveTextContent(
        '2 steps',
      )
    })
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1))
    const es = FakeEventSource.instances[0]
    expect(es.url).toContain(`/consult/stream/${REQUEST_ID}`)

    // And the answer lands on the re-attached stream.
    act(() => {
      es.emit({ type: 'step', kind: 'tool_call', tool: 'get_timeline', round: 3 })
      es.emit({
        type: 'final',
        final_source: 'registry',
        status: 'complete',
        response: {
          answer: 'recovered after the reload',
          finding_id: null,
          derived_from: [],
          tool_calls: [],
          cited_refs: [],
        },
      })
    })

    await waitFor(() =>
      expect(screen.getByText('recovered after the reload')).toBeInTheDocument(),
    )
  })

  it('recovers a finished run from the run status when the stream never delivered', async () => {
    // The stream is best-effort; the ANSWER is not. A reload that lands after
    // the run already finished must render it, not wait for a frame that has
    // already been and gone.
    useConsultSessions.setState({
      panels: {
        'c-detached': {
          transcript: [{ role: 'user', content: 'assess the slope' }],
          pendingTurn: {
            requestId: REQUEST_ID,
            question: 'assess the slope',
            mode: 'chat',
            startedAt: Date.now() - 5_000,
            pageLoadId: `${CONSULT_PAGE_LOAD_ID}-previous`,
            steps: [],
          },
          sessionId: 'sess-1',
          draft: '',
          pins: [],
          scope: '',
          maxRounds: 10,
          error: null,
          rev: 1,
          updatedAt: Date.now(),
        },
      },
    })

    vi.stubGlobal(
      'fetch',
      routedFetch({
        run: async () => ({
          ok: true,
          status: 200,
          json: async () => ({
            request_id: REQUEST_ID,
            session_id: 'sess-1',
            status: 'complete',
            elapsed_s: 42,
            steps: [{ kind: 'tool_call', tool: 'search_signals', round: 1 }],
            response: {
              answer: 'the answer was waiting on the server',
              finding_id: null,
              derived_from: [],
              tool_calls: [],
              cited_refs: [],
            },
          }),
        }),
      }),
    )

    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)

    await waitFor(() =>
      expect(
        screen.getByText('the answer was waiting on the server'),
      ).toBeInTheDocument(),
    )
  })
})
