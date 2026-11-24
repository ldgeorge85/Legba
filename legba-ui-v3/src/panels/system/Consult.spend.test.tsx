/**
 * The Consult panel's SPEND surface — the meter, the brake, and the salvage.
 *
 * Why these tests exist
 * =====================
 *
 * A consult ran on the billed Anthropic plane, cost roughly ten dollars, and
 * delivered 487 characters of "ran out of time budget". Three separate failures
 * had to line up for that, and each one is a test below:
 *
 *   1. **Nobody could see the spend.** Tokens and cost accumulated with no
 *      readout, against ceilings the panel never showed. The meter is now
 *      driven off the live `llm_call` frames and escalates as it closes on a
 *      ceiling, so "this is getting expensive" arrives BEFORE the invoice.
 *   2. **Nobody could see the cap.** The run drilled under a round cap of 10
 *      that the operator never chose and could not read. It is now on the
 *      in-flight block, and a cap that came from a DEFAULT rather than the
 *      request is coloured as the warning it is.
 *   3. **Nobody could stop it, and the partial was discarded.** There was no
 *      brake, and the synthesis text written before the deadline was thrown
 *      away. Now the answer streams as it forms, Stop ends the drilling and
 *      synthesises from what was already bought, and a settled turn whose
 *      synthesis was cut carries a button that re-runs it over the persisted
 *      evidence — declaring, when the transcript had to be rebuilt, that that
 *      is what happened.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import ConsultPanel from './Consult'
import { useConsultSessions, type ChatTurn } from '@/state/consultSession'
import type { PanelRegistration } from '@/types'
import { resetScope } from '@/state/scope'
import { useSelection } from '@/state/selection'

const REQUEST_ID = '00000000-0000-0000-0000-0000000000f1'
const PANEL_ID = 'c-spend'

function reg(): PanelRegistration {
  return {
    id: PANEL_ID,
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

/** A POST that leaves the run going — the shape every spend test starts from. */
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

function ok(body: unknown, status = 200) {
  return { ok: true, status, json: async () => body }
}

function err(status: number, detail: string) {
  return {
    ok: false,
    status,
    // `readErrorBody` reads the body ONCE via `res.text()`.
    text: async () => JSON.stringify({ detail }),
  }
}

/**
 * One stubbed fetch, routed by (method, path) — the salvage path POSTs to three
 * different endpoints and a test has to be able to tell them apart.
 */
function routedFetch(routes: {
  post?: () => unknown
  stop?: () => unknown
  synthesize?: () => unknown
  session?: () => unknown
}) {
  return vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    const path = String(url)
    if (init?.method === 'POST') {
      if (path.endsWith('/stop')) return routes.stop?.() ?? ok({ request_id: REQUEST_ID, status: 'stopped' })
      if (path.endsWith('/synthesize')) return routes.synthesize?.() ?? err(404, 'no run')
      return routes.post?.() ?? ACCEPTED
    }
    if (path.includes('/consult/runs/')) return err(404, 'no run')
    if (path.includes('/consult/sessions/')) return routes.session?.() ?? err(404, 'no session')
    return ok([])
  })
}

/** Bodies of the POSTs a mocked fetch saw, in order, as `[path, parsedBody]`. */
function posts(fetchMock: ReturnType<typeof vi.fn>): [string, Record<string, unknown>][] {
  return fetchMock.mock.calls
    .filter((c) => (c[1] as RequestInit | undefined)?.method === 'POST')
    .map((c) => [
      String(c[0]),
      JSON.parse(((c[1] as RequestInit).body as string) || '{}') as Record<string, unknown>,
    ])
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  useConsultSessions.setState({ panels: {} })
  resetScope()
  useSelection.getState().clear()
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  vi.stubGlobal('crypto', { ...globalThis.crypto, randomUUID: () => REQUEST_ID })
})

/** Start a turn and hand back its live stream. */
async function startTurn() {
  render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
  fireEvent.change(screen.getByTestId('consult-question'), {
    target: { value: 'what is this going to cost me' },
  })
  await act(async () => {
    fireEvent.click(screen.getByTestId('consult-submit'))
  })
  return FakeEventSource.instances[0]
}

const USAGE_CEILINGS = { max_input_tokens: 150_000, max_cost_usd: 3.0 }

function llmCall(input: number, cost: number, calls = 1) {
  return {
    type: 'step',
    phase: 'act',
    kind: 'llm_call',
    usage: {
      calls,
      input_tokens: input,
      output_tokens: 1_200,
      est_cost_usd: cost,
      ...USAGE_CEILINGS,
    },
  }
}

// ---------------------------------------------------------------------------

describe('ConsultPanel — live spend meter', () => {
  it('shows tokens and cost against their ceilings as the run spends', async () => {
    vi.stubGlobal('fetch', routedFetch({}))
    const es = await startTurn()

    // Nothing yet: no frame has said anything about spend, and inventing a
    // "$0.00" before the first call would be a number nobody can trust.
    expect(screen.queryByTestId('consult-usage')).not.toBeInTheDocument()

    act(() => {
      es.emit({
        type: 'step',
        phase: 'plan',
        kind: 'render_prompt',
        max_rounds: 6,
        rounds_source: 'request',
        ...USAGE_CEILINGS,
      })
      es.emit(llmCall(78_400, 1.94, 3))
    })

    await waitFor(() => expect(screen.getByTestId('consult-usage')).toBeInTheDocument())
    const meter = screen.getByTestId('consult-usage')
    expect(meter).toHaveTextContent('78,400 / 150,000 tok')
    expect(meter).toHaveTextContent('$1.94 / $3.00')
    // The breakdown that would clutter the line rides the tooltip instead.
    expect(meter.getAttribute('title')).toContain('3 model call(s)')
  })

  it('escalates amber past 75% and red past 90% of a ceiling', async () => {
    vi.stubGlobal('fetch', routedFetch({}))
    const es = await startTurn()

    act(() => {
      es.emit({ type: 'step', kind: 'render_prompt', max_rounds: 6, ...USAGE_CEILINGS })
      es.emit(llmCall(60_000, 1.2))
    })
    await waitFor(() => expect(screen.getByTestId('consult-usage')).toBeInTheDocument())
    expect(screen.getByTestId('consult-usage').className).toContain('text-slate-400')

    // 80% of the COST ceiling, only 40% of the token one — the tighter of the
    // two is what the operator needs to see.
    act(() => es.emit(llmCall(60_000, 2.4)))
    await waitFor(() =>
      expect(screen.getByTestId('consult-usage').className).toContain('text-amber-400'),
    )

    act(() => es.emit(llmCall(140_000, 2.4)))
    await waitFor(() =>
      expect(screen.getByTestId('consult-usage').className).toContain('text-rose-400'),
    )
  })

  it('keeps the ceilings from render_prompt when llm_call carries only totals', async () => {
    // The ceilings arrive ONCE, on the first frame of the run. A meter that
    // forgot them by the second call would be back to a number with no scale.
    vi.stubGlobal('fetch', routedFetch({}))
    const es = await startTurn()

    act(() => {
      es.emit({ type: 'step', kind: 'render_prompt', max_rounds: 6, ...USAGE_CEILINGS })
      es.emit({
        type: 'step',
        kind: 'llm_call',
        usage: { calls: 2, input_tokens: 20_000, output_tokens: 500, est_cost_usd: 0.5 },
      })
    })

    await waitFor(() =>
      expect(screen.getByTestId('consult-usage')).toHaveTextContent(
        '20,000 / 150,000 tok',
      ),
    )
    expect(screen.getByTestId('consult-usage')).toHaveTextContent('$0.50 / $3.00')
  })
})

describe('ConsultPanel — the round cap the incident hid', () => {
  it('surfaces the effective cap off the first frame', async () => {
    vi.stubGlobal('fetch', routedFetch({}))
    const es = await startTurn()
    expect(screen.queryByTestId('consult-round-cap')).not.toBeInTheDocument()

    act(() => {
      es.emit({
        type: 'step',
        phase: 'plan',
        kind: 'render_prompt',
        max_rounds: 6,
        rounds_source: 'request',
      })
    })

    await waitFor(() =>
      expect(screen.getByTestId('consult-round-cap')).toHaveTextContent('6 rounds'),
    )
    expect(screen.getByTestId('consult-round-cap').className).toContain('text-slate-400')
  })

  it('warns when the cap is a default the operator never asked for', async () => {
    vi.stubGlobal('fetch', routedFetch({}))
    const es = await startTurn()
    act(() => {
      es.emit({
        type: 'step',
        kind: 'render_prompt',
        max_rounds: 10,
        rounds_source: 'default_malformed_request',
      })
    })

    await waitFor(() =>
      expect(screen.getByTestId('consult-round-cap')).toHaveTextContent('10 rounds'),
    )
    const cap = screen.getByTestId('consult-round-cap')
    expect(cap.className).toContain('text-rose-400')
    expect(cap.getAttribute('title')).toContain('MALFORMED')
  })
})

describe('ConsultPanel — live answer preview', () => {
  it('accumulates answer_delta chunks in order without polluting the step ticker', async () => {
    vi.stubGlobal('fetch', routedFetch({}))
    const es = await startTurn()

    act(() => {
      es.emit({ type: 'step', phase: 'act', kind: 'tool_call', tool: 'search_signals', round: 1 })
      es.emit({ type: 'step', phase: 'narrate', kind: 'answer_delta', text: 'The slope is ' })
      es.emit({ type: 'step', phase: 'narrate', kind: 'answer_delta', text: '**flattening**.' })
    })

    await waitFor(() =>
      expect(screen.getByTestId('consult-answer-live')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('consult-answer-live')).toHaveTextContent(
      'The slope is flattening.',
    )
    // One tool call happened. Three hundred 400-char chunks must never be
    // reported to the operator as three hundred "steps".
    expect(screen.getByTestId('consult-pending-label')).toHaveTextContent('(1 steps)')
  })

  it('replaces the preview with the settled answer when the turn lands', async () => {
    vi.stubGlobal('fetch', routedFetch({}))
    const es = await startTurn()

    act(() => es.emit({ type: 'step', kind: 'answer_delta', text: 'half an ans' }))
    await waitFor(() =>
      expect(screen.getByTestId('consult-answer-live')).toBeInTheDocument(),
    )

    act(() => {
      es.emit({
        type: 'final',
        final_source: 'registry',
        status: 'complete',
        response: {
          answer: 'half an answer, then the whole one',
          finding_id: null,
          derived_from: [],
          tool_calls: [],
          cited_refs: [],
          synthesis_status: 'complete',
        },
      })
    })

    await waitFor(() => expect(screen.getByTestId('consult-answer')).toBeInTheDocument())
    expect(screen.queryByTestId('consult-answer-live')).not.toBeInTheDocument()
  })
})

describe('ConsultPanel — STOP', () => {
  it('is absent until a turn is in flight, and is NOT gated on busy', async () => {
    vi.stubGlobal('fetch', routedFetch({}))
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    expect(screen.queryByTestId('consult-stop')).not.toBeInTheDocument()

    fireEvent.change(screen.getByTestId('consult-question'), { target: { value: 'go' } })
    await act(async () => {
      fireEvent.click(screen.getByTestId('consult-submit'))
    })

    // Send is locked for the duration of the turn. Stop, the escape hatch, is
    // live precisely then — a brake that only works when the car is stopped is
    // not a brake.
    expect(screen.getByTestId('consult-submit')).toBeDisabled()
    expect(screen.getByTestId('consult-stop')).toBeEnabled()
    expect(screen.getByTestId('consult-stop')).toHaveTextContent('Stop')
  })

  it('stops the run, synthesises from the evidence, and settles the turn', async () => {
    const fetchMock = routedFetch({
      synthesize: () =>
        ok({
          answer: 'salvaged from 50 tool calls',
          finding_id: null,
          derived_from: [],
          tool_calls: [],
          cited_refs: [],
          model: 'core',
          synthesis_status: 'complete',
          resynthesizable: true,
          replay_fidelity: 'exact',
        }),
    })
    vi.stubGlobal('fetch', fetchMock)
    const es = await startTurn()
    act(() => es.emit({ type: 'step', kind: 'tool_call', tool: 'search_signals', round: 1 }))

    await act(async () => {
      fireEvent.click(screen.getByTestId('consult-stop'))
    })

    await waitFor(() =>
      expect(screen.getByText('salvaged from 50 tool calls')).toBeInTheDocument(),
    )
    // The turn settled through the ordinary path: pending block gone, stream
    // closed, Stop retired with it.
    expect(screen.queryByTestId('consult-live-steps')).not.toBeInTheDocument()
    expect(screen.queryByTestId('consult-stop')).not.toBeInTheDocument()
    expect(es.closed).toBe(true)

    const [first, second] = posts(fetchMock).slice(1)
    expect(first[0]).toContain(`/consult/runs/${REQUEST_ID}/stop`)
    expect(second[0]).toContain(`/consult/runs/${REQUEST_ID}/synthesize`)
    // The salvage runs on the plane the operator has selected.
    expect(second[1]).toEqual({ model: 'core' })
  })

  it('salvages anyway when the run had already finished (stop 404s)', async () => {
    const fetchMock = routedFetch({
      stop: () => err(404, 'unknown run'),
      synthesize: () =>
        ok({
          answer: 'the evidence was still there',
          finding_id: null,
          derived_from: [],
          tool_calls: [],
          cited_refs: [],
        }),
    })
    vi.stubGlobal('fetch', fetchMock)
    await startTurn()

    await act(async () => {
      fireEvent.click(screen.getByTestId('consult-stop'))
    })

    await waitFor(() =>
      expect(screen.getByText('the evidence was still there')).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('consult-error')).not.toBeInTheDocument()
  })

  it('surfaces a failed salvage without stranding the turn', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({ synthesize: () => err(500, 'the synthesis plane is down') }),
    )
    await startTurn()

    await act(async () => {
      fireEvent.click(screen.getByTestId('consult-stop'))
    })

    await waitFor(() =>
      expect(screen.getByTestId('consult-error')).toHaveTextContent(
        'the synthesis plane is down',
      ),
    )
    // The turn is still pending — the run may yet deliver, and failing it here
    // would throw away evidence that has already been paid for.
    expect(screen.getByTestId('consult-live-steps')).toBeInTheDocument()
    expect(screen.getByTestId('consult-stop')).toBeEnabled()
  })
})

describe('ConsultPanel — the provenance census (7g-2)', () => {
  /** Seed a settled answer carrying whatever census the server composed. */
  function seedAnswer(census: unknown) {
    useConsultSessions.setState({
      panels: {
        [PANEL_ID]: {
          sessionId: 'sess-c',
          transcript: [
            { role: 'user', content: 'compare the last ten years' },
            {
              role: 'assistant',
              content: 'An answer.',
              steps: [],
              requestId: REQUEST_ID,
              provenanceCensus: census,
            } as ChatTurn,
          ],
          pendingTurn: null,
          pins: [],
          draft: '',
          scope: '',
          maxRounds: 10,
          error: null,
          rev: 1,
          updatedAt: Date.now(),
        },
      },
    })
  }

  it('prints the count behind "mostly model knowledge", with units on each half', () => {
    seedAnswer({
      version: '2026-09/7g-2',
      live: 6,
      history: 2,
      web_retrieval: 0,
      seed: 0,
      model_knowledge: 3,
      sentences_examined: 9,
      basis: 'cited refs classified by their own origin_class column',
    })
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    const line = screen.getByTestId('consult-provenance-census')
    // REFS and SENTENCES are different units and the line says so on each, so
    // the two halves can never be read as one total.
    expect(line).toHaveTextContent('cited: 6 live · 2 history · 0 web · 0 seed')
    expect(line).toHaveTextContent('model knowledge: 3 sentences')
  })

  it('never prints a zero it did not measure', () => {
    // A census whose classes could not be read carries null, not 0 — "cites
    // no live reporting" is a claim, and absence is not.
    seedAnswer({
      version: '2026-09/7g-2',
      live: null,
      history: null,
      web_retrieval: null,
      seed: null,
      model_knowledge: 4,
      basis: 'origin class not measured for this run',
    })
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    const line = screen.getByTestId('consult-provenance-census')
    expect(line).toHaveTextContent('cited: not measured')
    expect(line).toHaveTextContent('model knowledge: 4 sentences')
    expect(line.textContent).not.toMatch(/0 live/)
  })

  it('renders nothing at all for a turn the server composed no census for', () => {
    seedAnswer(null)
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    expect(screen.queryByTestId('consult-provenance-census')).not.toBeInTheDocument()
  })

  it('says "1 sentence" rather than "1 sentences"', () => {
    seedAnswer({ live: 1, model_knowledge: 1 })
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    expect(screen.getByTestId('consult-provenance-census')).toHaveTextContent(
      'model knowledge: 1 sentence',
    )
  })

  // 2026-09-28 §B.3 — the 29 refs nobody ever saw.
  it('prints unresolved, fabricated and malformed beside the classes', () => {
    // The 2026-09-28 Opus run's own shape: 80 cited ids, 51 of them signals,
    // the other 29 real findings/situations/hypotheses/events that the census
    // filed as unresolved and the panel never rendered at all.
    seedAnswer({
      version: '2026-09/7g-2',
      live: 51,
      history: 0,
      web_retrieval: 0,
      seed: 0,
      unresolved: 29,
      fabricated: 2,
      malformed: ['sit-001'],
      by_kind: { signal: 51, finding: 15, situation: 8, hypothesis: 5, event: 1 },
      model_knowledge: 3,
      sentences_examined: 40,
      basis: 'cited refs classified by their own origin_class column',
    })
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    const line = screen.getByTestId('consult-provenance-census')
    expect(line).toHaveTextContent('cited: 51 live · 0 history · 0 web · 0 seed')
    expect(line).toHaveTextContent('29 unresolved')
    expect(line).toHaveTextContent('2 fabricated')
    expect(line).toHaveTextContent('1 malformed')
    // The 29 are NAMED, not just counted — the breakdown rides the tooltip so
    // the line stays one line.
    expect(line.title).toContain('15 finding')
    expect(line.title).toContain('8 situation')
    expect(line.title).toContain('sit-001')
  })

  it('never prints an unresolved or fabricated count it did not measure', () => {
    // `fabricated: 0` that nobody measured reads as "nothing was invented",
    // which is a claim. Absence is not.
    seedAnswer({ live: 6, history: 2, model_knowledge: 3 })
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    const line = screen.getByTestId('consult-provenance-census')
    expect(line.textContent).not.toMatch(/unresolved/)
    expect(line.textContent).not.toMatch(/fabricated/)
    expect(line.textContent).not.toMatch(/malformed/)
  })

  it('does not print a measured zero as a defect count', () => {
    seedAnswer({ live: 6, unresolved: 0, fabricated: 0, malformed: [], model_knowledge: 1 })
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    const line = screen.getByTestId('consult-provenance-census')
    expect(line.textContent).not.toMatch(/0 unresolved/)
    expect(line.textContent).not.toMatch(/0 fabricated/)
  })
})

describe('ConsultPanel — the provenance hold (2026-09-28 §B.3)', () => {
  function seedHeldAnswer(holdReason: string | null) {
    useConsultSessions.setState({
      panels: {
        [PANEL_ID]: {
          sessionId: 'sess-h',
          transcript: [
            { role: 'user', content: 'what is going on in iran' },
            {
              role: 'assistant',
              content: 'Iran struck three sites on Tuesday.',
              steps: [],
              requestId: REQUEST_ID,
              holdReason,
            } as ChatTurn,
          ],
          pendingTurn: null,
          pins: [],
          draft: '',
          scope: '',
          maxRounds: 10,
          error: null,
          rev: 1,
          updatedAt: Date.now(),
        },
      },
    })
  }

  it('frames a held answer before the reader reaches a word of it', () => {
    seedHeldAnswer('provenance_hold')
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    const hold = screen.getByTestId('consult-provenance-hold')
    expect(hold).toHaveTextContent('nothing this answer cites resolved')
    // The answer is KEPT, below the frame — a held answer is not a discarded
    // one, and the operator paid for it.
    expect(screen.getByTestId('consult-answer')).toHaveTextContent(
      'Iran struck three sites',
    )
  })

  it('renders no frame for an answer the server did not hold', () => {
    seedHeldAnswer(null)
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    expect(screen.queryByTestId('consult-provenance-hold')).not.toBeInTheDocument()
  })
})

describe('ConsultPanel — Synthesize from evidence', () => {
  /** Seed a settled conversation whose answer was cut short. */
  function seedCutTurn(turn: Partial<ChatTurn> = {}) {
    useConsultSessions.setState({
      panels: {
        [PANEL_ID]: {
          sessionId: 'sess-1',
          transcript: [
            { role: 'user', content: 'assess the slope' },
            {
              role: 'assistant',
              content: '_ran out of time budget_',
              steps: [],
              requestId: REQUEST_ID,
              synthesisStatus: 'partial',
              resynthesizable: true,
              ...turn,
            },
          ],
          pendingTurn: null,
          pins: [],
          draft: '',
          scope: '',
          maxRounds: 10,
          error: null,
          rev: 1,
          updatedAt: Date.now(),
        },
      },
    })
  }

  it('offers the button on a partial answer and replaces it with the salvage', async () => {
    const fetchMock = routedFetch({
      synthesize: () =>
        ok({
          answer: 'the full answer, from the evidence that was already bought',
          finding_id: null,
          derived_from: [],
          tool_calls: [],
          cited_refs: [],
          synthesis_status: 'complete',
          replay_fidelity: 'exact',
        }),
    })
    vi.stubGlobal('fetch', fetchMock)
    seedCutTurn()
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)

    await act(async () => {
      fireEvent.click(screen.getByTestId('consult-resynthesize'))
    })

    await waitFor(() =>
      expect(
        screen.getByText('the full answer, from the evidence that was already bought'),
      ).toBeInTheDocument(),
    )
    // Replaced IN PLACE: one assistant turn, not a stump plus a repair.
    expect(screen.queryByText('ran out of time budget')).not.toBeInTheDocument()
    expect(screen.getAllByTestId('consult-turn-assistant')).toHaveLength(1)
    // ...and the answer is no longer advertised as recoverable.
    expect(screen.queryByTestId('consult-resynthesize')).not.toBeInTheDocument()

    const [path, body] = posts(fetchMock)[0]
    expect(path).toContain(`/consult/runs/${REQUEST_ID}/synthesize`)
    expect(body).toEqual({ model: 'core' })
  })

  it('never lets a REBUILT transcript pass for a clean re-run', async () => {
    const note =
      'transcript rebuilt by re-executing 50 tool calls; assistant free text between rounds not recoverable'
    vi.stubGlobal(
      'fetch',
      routedFetch({
        synthesize: () =>
          ok({
            answer: 'an answer, with an asterisk',
            finding_id: null,
            derived_from: [],
            tool_calls: [],
            cited_refs: [],
            synthesis_status: 'complete',
            replay_fidelity: 'rebuilt',
            replay_note: note,
          }),
      }),
    )
    seedCutTurn()
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)

    await act(async () => {
      fireEvent.click(screen.getByTestId('consult-resynthesize'))
    })

    await waitFor(() =>
      expect(screen.getByTestId('consult-replay-note')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('consult-replay-note')).toHaveTextContent(note)
  })

  it('falls back to the trace for turns older than synthesis_status', async () => {
    // The turns the incident actually happened in carry no `synthesis_status` —
    // only a `degraded_final` step. They are exactly the ones worth rescuing.
    vi.stubGlobal('fetch', routedFetch({}))
    seedCutTurn({
      synthesisStatus: undefined,
      resynthesizable: undefined,
      steps: [
        { type: 'step', kind: 'tool_call', tool: 'search_signals', round: 1 },
        { type: 'step', phase: 'reflect', kind: 'degraded_final', reason: 'out of budget' },
      ],
    })
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)

    expect(screen.getByTestId('consult-resynthesize')).toBeInTheDocument()
  })

  it('recovers a turn restored from History, which has no run id at all', async () => {
    // THE turn this whole path exists for. It was written before the run id was
    // recorded on the row, so it names no run — only itself. Addressing it by
    // turn id is the difference between recovering the ~$10 answer and not.
    const fetchMock = routedFetch({
      synthesize: () =>
        ok({
          answer: 'the answer that ten dollars had already bought',
          finding_id: null,
          derived_from: [],
          tool_calls: [],
          cited_refs: [],
          synthesis_status: 'complete',
          replay_fidelity: 'rebuilt',
          replay_note: 'transcript rebuilt by re-executing 50 tool calls',
          turn_id: 'turn-9',
        }),
    })
    vi.stubGlobal('fetch', fetchMock)
    seedCutTurn({
      requestId: null,
      turnId: 'turn-9',
      synthesisStatus: undefined,
      resynthesizable: undefined,
      steps: [
        { type: 'step', phase: 'reflect', kind: 'degraded_final', reason: 'out of budget' },
      ],
    })
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)

    await act(async () => {
      fireEvent.click(screen.getByTestId('consult-resynthesize'))
    })

    await waitFor(() =>
      expect(
        screen.getByText('the answer that ten dollars had already bought'),
      ).toBeInTheDocument(),
    )
    const [path, body] = posts(fetchMock)[0]
    expect(path).toContain('/consult/runs/turn-9/synthesize')
    expect(body).toEqual({ model: 'core', turn_id: 'turn-9' })
    // A rebuilt transcript still says so, however it was addressed.
    expect(screen.getByTestId('consult-replay-note')).toHaveTextContent('rebuilt')
  })

  it('carries the recovery\'s OWN census, never the cut run\'s', async () => {
    // 2026-09-28 §B.3. The recovery writes a NEW answer over the same
    // evidence; the panel used to keep the original turn's census beside it,
    // so the count described prose that no longer existed.
    vi.stubGlobal(
      'fetch',
      routedFetch({
        synthesize: () =>
          ok({
            answer: 'the recovered answer',
            finding_id: null,
            derived_from: [],
            tool_calls: [],
            cited_refs: [],
            synthesis_status: 'complete',
            replay_fidelity: 'exact',
            provenance_census: { live: 2, history: 0, model_knowledge: 1 },
          }),
      }),
    )
    seedCutTurn({
      provenanceCensus: { live: 51, history: 4, model_knowledge: 12 },
    })
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)

    await act(async () => {
      fireEvent.click(screen.getByTestId('consult-resynthesize'))
    })

    await waitFor(() =>
      expect(screen.getByText('the recovered answer')).toBeInTheDocument(),
    )
    const line = screen.getByTestId('consult-provenance-census')
    expect(line).toHaveTextContent('cited: 2 live · 0 history')
    expect(line.textContent).not.toMatch(/51 live/)
  })

  it('shows no census at all when the recovery could not measure one', async () => {
    // `?? null`, not `?? turn.provenanceCensus`: a recovery with no census
    // borrows nothing.
    vi.stubGlobal(
      'fetch',
      routedFetch({
        synthesize: () =>
          ok({
            answer: 'the recovered answer',
            finding_id: null,
            derived_from: [],
            tool_calls: [],
            cited_refs: [],
            synthesis_status: 'complete',
            replay_fidelity: 'exact',
          }),
      }),
    )
    seedCutTurn({
      provenanceCensus: { live: 51, history: 4, model_knowledge: 12 },
    })
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)

    await act(async () => {
      fireEvent.click(screen.getByTestId('consult-resynthesize'))
    })

    await waitFor(() =>
      expect(screen.getByText('the recovered answer')).toBeInTheDocument(),
    )
    expect(
      screen.queryByTestId('consult-provenance-census'),
    ).not.toBeInTheDocument()
  })

  it('offers nothing on a complete answer, or when the evidence has aged out', async () => {
    vi.stubGlobal('fetch', routedFetch({}))
    seedCutTurn({ synthesisStatus: 'complete' })
    const { unmount } = render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    expect(screen.queryByTestId('consult-resynthesize')).not.toBeInTheDocument()
    unmount()

    // The server says the evidence is gone: a button that can only 404 is
    // worse than no button.
    seedCutTurn({ synthesisStatus: 'partial', resynthesizable: false })
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    expect(screen.queryByTestId('consult-resynthesize')).not.toBeInTheDocument()
  })

  it('surfaces a failed salvage on the error banner, leaving the turn readable', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({ synthesize: () => err(404, 'run evidence has aged out') }),
    )
    seedCutTurn()
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)

    await act(async () => {
      fireEvent.click(screen.getByTestId('consult-resynthesize'))
    })

    await waitFor(() =>
      expect(screen.getByTestId('consult-error')).toHaveTextContent(
        'run evidence has aged out',
      ),
    )
    expect(screen.getByTestId('consult-answer')).toHaveTextContent('ran out of time budget')
  })
})

// ---------------------------------------------------------------------------
// Migration 0223 — a RELOADED turn renders what the live one did
// ---------------------------------------------------------------------------

describe('ConsultPanel — the response bag survives a reload (0223)', () => {
  const CENSUS = {
    version: '2026-09/7g-2',
    live: 0,
    history: 0,
    web_retrieval: 0,
    seed: 0,
    unresolved: 3,
    fabricated: 1,
    model_knowledge: 11,
  }
  const FLOOR = { from: 0.3, to: 0.85, resolved: 0 }

  /** The session the server holds: a pre-0223 turn and a 0223 turn. */
  const DETAIL = {
    id: 'sess-r',
    mode: 'chat',
    title: 'what is going on in iran',
    turns: [
      { id: 't1', role: 'user', content: 'what was it before', steps: [], tool_calls: [], cited_refs: [] },
      {
        id: 't2',
        role: 'assistant',
        content: 'the old answer',
        steps: [],
        tool_calls: [],
        cited_refs: [],
        synthesis_status: 'complete',
        response_recorded: false,
        uncertainty: null,
        provenance_census: null,
        hold_reason: null,
        uncertainty_floor_applied: null,
        watch_candidates: null,
        model: null,
      },
      { id: 't3', role: 'user', content: 'what is going on in iran', steps: [], tool_calls: [], cited_refs: [] },
      {
        id: 't4',
        role: 'assistant',
        content: 'Iran struck three sites on Tuesday.',
        steps: [],
        tool_calls: [],
        cited_refs: [],
        synthesis_status: 'complete',
        response_recorded: true,
        uncertainty: 0.85,
        provenance_census: CENSUS,
        hold_reason: 'provenance_hold',
        uncertainty_floor_applied: FLOOR,
        watch_candidates: ['a second outlet names the sites'],
        model: 'core',
      },
    ],
  }

  function seedReloadedSlice() {
    // What a reload leaves: the session id, and nothing the server has not
    // re-sent. Reconcile-on-mount re-seeds the transcript from the server.
    useConsultSessions.setState({
      panels: {
        [PANEL_ID]: {
          sessionId: 'sess-r',
          transcript: [],
          pendingTurn: null,
          pins: [],
          draft: '',
          scope: '',
          maxRounds: 10,
          error: null,
          rev: 1,
          updatedAt: Date.now(),
        },
      },
    })
  }

  it('renders the census line, the hold banner and the floor stamp on reload', async () => {
    vi.stubGlobal('fetch', routedFetch({ session: () => ok(DETAIL) }))
    seedReloadedSlice()
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)

    await waitFor(() =>
      expect(screen.getByText('Iran struck three sites on Tuesday.')).toBeInTheDocument(),
    )
    const [oldTurn, newTurn] = screen.getAllByTestId('consult-turn-assistant')

    // The 0223 turn: exactly the live rendering.
    const census = within(newTurn).getByTestId('consult-provenance-census')
    expect(census).toHaveTextContent('cited: 0 live · 0 history · 0 web · 0 seed')
    expect(census).toHaveTextContent('3 unresolved')
    expect(census).toHaveTextContent('1 fabricated')
    expect(census).toHaveTextContent('model knowledge: 11 sentences')
    expect(within(newTurn).getByTestId('consult-provenance-hold')).toHaveTextContent(
      'nothing this answer cites resolved',
    )
    const floor = within(newTurn).getByTestId('consult-uncertainty-floor')
    expect(floor).toHaveTextContent('floored from 0.30')
    expect(floor).toHaveTextContent('0 cited refs resolved')
    expect(newTurn).toHaveTextContent('uncertainty=0.85')
    expect(within(newTurn).getByTestId('consult-answer-model')).toHaveTextContent('via core')

    // The pre-0223 turn: "not measured", never a zero and never a frame.
    const oldCensus = within(oldTurn).getByTestId('consult-provenance-census')
    expect(oldCensus).toHaveTextContent('cited: not measured')
    expect(oldCensus.textContent).not.toMatch(/0 live/)
    expect(within(oldTurn).queryByTestId('consult-provenance-hold')).not.toBeInTheDocument()
    expect(within(oldTurn).queryByTestId('consult-uncertainty-floor')).not.toBeInTheDocument()
    expect(oldTurn.textContent).not.toMatch(/uncertainty=/)
  })

  it('a registry that predates the field renders as it always did', async () => {
    const legacy = {
      ...DETAIL,
      turns: DETAIL.turns.map((t) => ({
        id: t.id,
        role: t.role,
        content: t.content,
        steps: [],
        tool_calls: [],
        cited_refs: [],
      })),
    }
    vi.stubGlobal('fetch', routedFetch({ session: () => ok(legacy) }))
    seedReloadedSlice()
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    await waitFor(() =>
      expect(screen.getByText('Iran struck three sites on Tuesday.')).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('consult-provenance-census')).not.toBeInTheDocument()
    expect(screen.queryByTestId('consult-uncertainty-floor')).not.toBeInTheDocument()
  })

  it('stamps the floor on a LIVE answer from the terminal frame', async () => {
    vi.stubGlobal('fetch', routedFetch({}))
    const es = await startTurn()
    act(() => {
      es.emit({
        type: 'final',
        final_source: 'registry',
        status: 'complete',
        session_id: 'sess-1',
        response: {
          answer: 'the live answer',
          finding_id: null,
          derived_from: [],
          tool_calls: [],
          cited_refs: [],
          session_id: 'sess-1',
          uncertainty: 0.6,
          provenance_census: { live: 2, model_knowledge: 1 },
          uncertainty_floor_applied: { from: 0.2, to: 0.6, resolved: 1 },
          watch_candidates: ['the vote is postponed'],
        },
      })
    })
    await waitFor(() => expect(screen.getByText('the live answer')).toBeInTheDocument())
    const floor = screen.getByTestId('consult-uncertainty-floor')
    expect(floor).toHaveTextContent('floored from 0.20 · 1 cited ref resolved')
    const turn = useConsultSessions.getState().panels[PANEL_ID].transcript[1]
    expect(turn.watchCandidates).toEqual(['the vote is postponed'])
    // A live turn is not a reloaded one — no reload marker.
    expect(turn.responseRecorded).toBeUndefined()
  })

  // W3 / F-4 — the watch mint's outcome, one line beside the answer header.
  const MINTED = [
    {
      candidate: 'a second outlet names the sites by 2026-12-01',
      hypothesis_id: 'h-1',
      refused: false,
      reason: null,
      resolves_by: '2026-12-01T00:00:00+00:00',
      folded: false,
    },
    {
      candidate: 'the strikes resume',
      hypothesis_id: null,
      refused: true,
      reason: 'hypothesis_refused_no_resolves_by',
      resolves_by: null,
      folded: false,
    },
  ]

  it('prints how many watches stand on reload, and nothing where no mint ran', async () => {
    const detail = {
      ...DETAIL,
      turns: DETAIL.turns.map((t) => (t.id === 't4' ? { ...t, watch_minted: MINTED } : t)),
    }
    vi.stubGlobal('fetch', routedFetch({ session: () => ok(detail) }))
    seedReloadedSlice()
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    await waitFor(() =>
      expect(screen.getByText('Iran struck three sites on Tuesday.')).toBeInTheDocument(),
    )
    const [oldTurn, newTurn] = screen.getAllByTestId('consult-turn-assistant')
    const line = within(newTurn).getByTestId('consult-watch-standing')
    expect(line).toHaveTextContent('1 watch standing · 1 not watched')
    expect(line.getAttribute('title')).toContain(
      'watching: a second outlet names the sites by 2026-12-01 — until 2026-12-01',
    )
    expect(line.getAttribute('title')).toContain(
      'not watched: the strikes resume — hypothesis_refused_no_resolves_by',
    )
    // No mint attempted on the old turn: no line at all — never "0 standing".
    expect(within(oldTurn).queryByTestId('consult-watch-standing')).not.toBeInTheDocument()
  })

  it('prints the standing watches on a LIVE answer from the terminal frame', async () => {
    vi.stubGlobal('fetch', routedFetch({}))
    const es = await startTurn()
    act(() => {
      es.emit({
        type: 'final',
        final_source: 'registry',
        status: 'complete',
        session_id: 'sess-1',
        response: {
          answer: 'the live answer',
          finding_id: null,
          derived_from: [],
          tool_calls: [],
          cited_refs: [],
          session_id: 'sess-1',
          watch_candidates: MINTED.map((m) => m.candidate),
          watch_minted: [{ ...MINTED[0], folded: true }],
        },
      })
    })
    await waitFor(() => expect(screen.getByText('the live answer')).toBeInTheDocument())
    const line = screen.getByTestId('consult-watch-standing')
    expect(line).toHaveTextContent('1 watch standing')
    expect(line.textContent).not.toMatch(/not watched/)
    expect(line.getAttribute('title')).toContain('(already standing)')
    const turn = useConsultSessions.getState().panels[PANEL_ID].transcript[1]
    expect(turn.watchMinted).toHaveLength(1)
  })

  it('W4-F1: counts the folded candidates and names what each folded onto', async () => {
    const minted = [
      { ...MINTED[0], hypothesis_id: 'h-new', fold_check: 'semantic', similarity: 0.43 },
      {
        candidate: 'detection of a major arms acquisition by 2026-12-01',
        hypothesis_id: 'e6227510-0b12-4027-b4b4-dd55804ee044',
        refused: false,
        reason: null,
        resolves_by: '2026-12-01T00:00:00+00:00',
        folded: true,
        folded_onto: 'e6227510-0b12-4027-b4b4-dd55804ee044',
        fold_check: 'semantic',
        similarity: 0.947,
      },
      {
        candidate: 'a naval buildup by 2026-12-02',
        hypothesis_id: 'h-unchecked',
        refused: false,
        reason: null,
        resolves_by: '2026-12-02T00:00:00+00:00',
        folded: false,
        fold_check: 'unavailable',
      },
      MINTED[1],
    ]
    const detail = {
      ...DETAIL,
      turns: DETAIL.turns.map((t) => (t.id === 't4' ? { ...t, watch_minted: minted } : t)),
    }
    vi.stubGlobal('fetch', routedFetch({ session: () => ok(detail) }))
    seedReloadedSlice()
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    await waitFor(() =>
      expect(screen.getByText('Iran struck three sites on Tuesday.')).toBeInTheDocument(),
    )
    const [, newTurn] = screen.getAllByTestId('consult-turn-assistant')
    const line = within(newTurn).getByTestId('consult-watch-standing')
    expect(line).toHaveTextContent('3 watches standing · 1 not watched · 1 folded')
    const title = line.getAttribute('title') ?? ''
    expect(title).toContain(
      'detection of a major arms acquisition by 2026-12-01 — until 2026-12-01 ' +
        '(already standing — folded onto e6227510, claim-core cosine 0.95)',
    )
    expect(title).toContain('a naval buildup by 2026-12-02 — until 2026-12-02 (fold check unavailable)')
    // A watch that stood on its own carries no fold note at all.
    expect(title).toContain('watching: a second outlet names the sites by 2026-12-01 — until 2026-12-01\n')
  })

  it('a re-synthesis of a reloaded pre-0223 turn renders ITS OWN census and floor', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        synthesize: () =>
          ok({
            answer: 'the recovered answer',
            finding_id: null,
            derived_from: [],
            tool_calls: [],
            cited_refs: [],
            synthesis_status: 'complete',
            replay_fidelity: 'exact',
            uncertainty: 0.45,
            provenance_census: { live: 9, model_knowledge: 2 },
            uncertainty_floor_applied: { from: 0.1, to: 0.45, resolved: 9 },
          }),
      }),
    )
    useConsultSessions.setState({
      panels: {
        [PANEL_ID]: {
          sessionId: 'sess-r',
          transcript: [
            { role: 'user', content: 'assess the slope' },
            {
              role: 'assistant',
              content: '_ran out of time budget_',
              steps: [],
              turnId: 't-old',
              synthesisStatus: 'partial',
              resynthesizable: true,
              responseRecorded: false,
            },
          ],
          pendingTurn: null,
          pins: [],
          draft: '',
          scope: '',
          maxRounds: 10,
          error: null,
          rev: 1,
          updatedAt: Date.now(),
        },
      },
    })
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    expect(screen.getByTestId('consult-provenance-census')).toHaveTextContent(
      'cited: not measured',
    )

    await act(async () => {
      fireEvent.click(screen.getByTestId('consult-resynthesize'))
    })
    await waitFor(() =>
      expect(screen.getByText('the recovered answer')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('consult-provenance-census')).toHaveTextContent('cited: 9 live')
    expect(screen.getByTestId('consult-uncertainty-floor')).toHaveTextContent(
      'floored from 0.10 · 9 cited refs resolved',
    )
  })
})
