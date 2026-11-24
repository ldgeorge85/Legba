/**
 * M-3g — the phone's Consult SCREEN.
 *
 *   3  always open, no toggle; the composer row is the question box and Ask;
 *      the two controls are the window and "Only what is on record"; an empty
 *      state that says what Consult does here, with examples built from the
 *      screen (filled in, never sent); the thread keeps its distance from its
 *      bottom across a resize (the keyboard)
 *   2  the window is the shell's chip, said in plain words, and sent
 *   5  "Asking about" shows the pin above the composer, removable; the pin
 *      travels through the desktop's builders
 *   6  the answer is rendered prose with citation buttons; the contract is one
 *      line (window · coverage · Partial answer) that opens to the shared
 *      block; Stop, partial, the stream and the detached pickup keep working
 *   4  drive mode on sends the turn as a case; a recorded case is offered as
 *      a button; the strip names what the phone could not open
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import ConsultScreen, { exampleQuestions } from './ConsultScreen'
import { useConsultSessions } from '@/state/consultSession'
import { casePlayer, resetCasePlayer } from '@/state/casePlayer'
import { useDriveMode } from '@/state/driveMode'
import { parseCaseRecord } from '@/lib/caseModel'
import { useWorldState } from '@/v4/world/worldState'
import { usePhoneShell } from '@/mobile/shell/phoneState'
import { registerPhoneFramePort } from '@/mobile/shell/phoneFramePort'
import { registerPhoneShellPort } from '@/mobile/shell/phoneShellPort'
import { phoneScreenOf } from '@/mobile/shell/phoneAddress'
import { MOBILE_PANEL_ID, type PhoneAsking } from '@/mobile/mobileConsult'

const REQUEST_ID = '00000000-0000-0000-0000-0000000000f1'
const DAY = 24 * 3_600_000

class FakeEventSource {
  static instances: FakeEventSource[] = []
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  closed = false
  constructor(public url: string) {
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

type Reply = { ok: boolean; status: number; json: () => Promise<unknown>; text?: () => Promise<string> }
const ok = (status: number, body: unknown): Reply => ({ ok: true, status, json: async () => body, text: async () => JSON.stringify(body) })

const ACCEPTED = ok(202, { answer: '', status: 'accepted', request_id: REQUEST_ID, session_id: 'sess-m', finding_id: null, derived_from: [], tool_calls: [], cited_refs: [] })

const CONTRACT = {
  schema: 'consult_contract/1',
  armed: true,
  window: { start: '2026-11-07T08:00:00+00:00', end: '2026-11-14T08:00:00+00:00', hours: 168, source: 'console', basis: 'console 168h', deictic: false },
  coverage: {
    state: 'measured',
    days: ['2026-11-08', '2026-11-09', '2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13', '2026-11-14'].map((day, i) => ({ day, signals: i === 2 ? 0 : 4, sources: i === 2 ? 0 : 2, findings: i === 2 ? 0 : 1, events: 0 })),
    zero_days: ['2026-11-10'],
    sparse_days: [],
    gaps: { paused_sources: [], error_sources: [] },
  },
  grounding_mode: 'default',
  cost_stop: "> **Stopped for cost:** drilling stopped after round 4 because the turn's cost ceiling leaves no room.",
}

const REF_A = 'a1b2c3d4-0000-4000-8000-000000000001'
const REF_B = 'b2c3d4e5-0000-4000-8000-000000000002'
const MARKDOWN_ANSWER =
  '**Short answer.** Two sources disagree [a1b2c3d4].\n\n' +
  '- The first point [b2c3d4e5-0000].\n' +
  '- An ordinal the model wrote with no binding [3].\n\n' +
  '| Measure | Last 7 d | Prior 7 d |\n|---|---|---|\n| Events | 41 | 33 |\n\n' +
  'A row that is not cited [ffffffff] stays as written.'

const ASKING: PhoneAsking = {
  label: 'Example harbour read',
  pins: [{ kind: 'finding', id: 'ex-read-1', label: 'Example harbour read', preview: { title: 'Example harbour read', body: 'A read about Example.' } }],
}

function routed(routes: { post?: () => Reply; synth?: () => Reply; get?: (u: string) => Reply } = {}) {
  return vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    const path = String(url)
    if (init?.method === 'POST' && path.endsWith('/stop')) return ok(200, { stopped: true })
    if (init?.method === 'POST' && path.endsWith('/synthesize')) return routes.synth?.() ?? ok(500, {})
    if (init?.method === 'POST') return routes.post?.() ?? ACCEPTED
    return routes.get?.(path) ?? { ok: false, status: 404, json: async () => ({ detail: 'none' }), text: async () => 'none' }
  })
}

function postBodies(fetchMock: ReturnType<typeof vi.fn>): [string, Record<string, unknown>][] {
  return fetchMock.mock.calls
    .filter(([, init]) => (init as RequestInit)?.method === 'POST')
    .map(([u, init]) => [String(u).replace(/^.*\/api\/v1/, ''), JSON.parse(String((init as RequestInit).body))])
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  window.history.replaceState(null, '', '/m/#screen=consult')
  useConsultSessions.setState({ panels: {} })
  usePhoneShell.setState({ watchSegment: 'alerts', lastScreen: null, asking: null })
  useDriveMode.setState({ on: false })
  useWorldState.getState().setSpan(7 * DAY)
  resetCasePlayer()
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  vi.stubGlobal('crypto', { ...globalThis.crypto, randomUUID: () => REQUEST_ID })
})

afterEach(() => {
  cleanup()
  resetCasePlayer()
  vi.unstubAllGlobals()
})

async function ask(fetchMock: ReturnType<typeof vi.fn>, question = 'what changed?') {
  vi.stubGlobal('fetch', fetchMock)
  render(<ConsultScreen />)
  fireEvent.change(screen.getByTestId('consult-input'), { target: { value: question } })
  await act(async () => {
    fireEvent.click(screen.getByTestId('consult-send'))
  })
  return FakeEventSource.instances[0]
}

function settle(es: FakeEventSource, response: Record<string, unknown>) {
  act(() => es.emit({ type: 'final', final_source: 'registry', status: 'complete', response: { finding_id: null, derived_from: [], tool_calls: [], cited_refs: [], session_id: 'sess-m', ...response } }))
}

describe('3 · the screen: always open, two controls, the composer', () => {
  it('has no toggle; the composer row is the question box and Ask; the controls are the window and record-only', () => {
    render(<ConsultScreen />)
    expect(screen.queryByTestId('consult-toggle')).toBeNull()
    const row = screen.getByTestId('consult-composer')
    expect(within(row).getByTestId('consult-input')).toBeInTheDocument()
    expect(row.querySelectorAll('button, select, input')).toHaveLength(1)
    const controls = screen.getByTestId('consult-controls')
    expect(controls.querySelectorAll('button, select, input')).toHaveLength(2)
    expect(within(controls).getByTestId('consult-window')).toBeInTheDocument()
    expect(within(controls).getByTestId('consult-record-only').getAttribute('aria-checked')).toBe('false')
    expect(screen.queryByTestId('consult-model')).toBeNull()
    expect(screen.queryByTestId('consult-allow-writes')).toBeNull()
    // no keyboard-shortcut hints on a touch composer
    expect(row.textContent ?? '').not.toMatch(/⌘|Ctrl|Shift|Enter/)
  })

  it('the empty state says what Consult does here, with examples from the screen that fill the box but never send', () => {
    const fetchMock = routed()
    vi.stubGlobal('fetch', fetchMock)
    usePhoneShell.setState({ lastScreen: phoneScreenOf('#ws=watch') })
    render(<ConsultScreen />)
    const empty = screen.getByTestId('consult-empty')
    expect(empty.textContent).toMatch(/Ask a question about what is on record/)
    const ex = screen.getAllByTestId('consult-example')
    expect(ex.length).toBeGreaterThanOrEqual(2)
    expect(ex.length).toBeLessThanOrEqual(3)
    expect(ex.map((e) => e.textContent).join(' ')).toMatch(/alerts/)
    fireEvent.click(ex[0])
    expect((screen.getByTestId('consult-input') as HTMLTextAreaElement).value).toBe(ex[0].textContent)
    expect(postBodies(fetchMock)).toHaveLength(0)
    // no engine words in what it says
    expect(empty.textContent).not.toMatch(/tool|round|grounding|substrate|on_screen|panel/i)
  })

  it('examples follow the pin when there is one, and the window in words', () => {
    expect(exampleQuestions(ASKING, null, 'the last 24 hours')[0]).toBe('What changed for Example harbour read in the last 24 hours?')
    expect(exampleQuestions(null, phoneScreenOf('#ws=ledger'), 'x')[0]).toMatch(/forecasts/)
    expect(exampleQuestions(null, null, 'the last 7 days')[0]).toBe('What changed in the last 7 days?')
  })

  it('keeps its distance from the bottom when the keyboard resizes it (the last message stays in view)', () => {
    let fire: (() => void) | null = null
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(cb: () => void) {
          fire = cb
        }
        observe() {}
        disconnect() {}
      },
    )
    let now = 1_000_000
    vi.spyOn(Date, 'now').mockImplementation(() => now)
    render(<ConsultScreen />)
    const t = screen.getByTestId('consult-transcript')
    let clientHeight = 600
    Object.defineProperty(t, 'scrollHeight', { configurable: true, get: () => 2000 })
    Object.defineProperty(t, 'clientHeight', { configurable: true, get: () => clientHeight })
    // at the bottom
    t.scrollTop = 1400
    fireEvent.scroll(t)
    clientHeight = 300 // the keyboard opens
    act(() => fire?.())
    expect(t.scrollTop).toBe(1700)
    // the browser's own clamp lands a frame late, against a layout that moved
    // again: not the reader, so the distance is not re-measured from it
    t.scrollTop = 1650
    fireEvent.scroll(t)
    act(() => fire?.())
    expect(t.scrollTop).toBe(1700)
    // later, the reader scrolls up a little; the keyboard closes: the same distance from the bottom
    now += 1000
    t.scrollTop = 1500
    fireEvent.scroll(t)
    clientHeight = 600
    act(() => fire?.())
    expect(t.scrollTop).toBe(1200)
  })
})

describe('2 · the window is the shell chip', () => {
  it('says the window in plain words and sends it; changing it moves the shell window', async () => {
    useWorldState.getState().setSpan(DAY)
    const fetchMock = routed()
    vi.stubGlobal('fetch', fetchMock)
    render(<ConsultScreen />)
    expect(screen.getByTestId('consult-window-words').textContent).toContain('the last 24 hours')
    fireEvent.change(screen.getByTestId('consult-window'), { target: { value: '30d' } })
    expect(useWorldState.getState().spanMs).toBe(30 * DAY)
    expect(screen.getByTestId('consult-window-words').textContent).toContain('the last 30 days')
    fireEvent.change(screen.getByTestId('consult-input'), { target: { value: 'q' } })
    await act(async () => {
      fireEvent.click(screen.getByTestId('consult-send'))
    })
    const [[, body]] = postBodies(fetchMock)
    expect(body.window).toMatchObject({ hours: 720, label: 'console 720h' })
  })
})

describe('5 · Asking about', () => {
  it('shows the pin above the composer and sends it through the desktop builders; × removes it', async () => {
    usePhoneShell.setState({ asking: ASKING })
    const fetchMock = routed()
    await ask(fetchMock)
    expect(screen.getByTestId('phone-asking-label').textContent).toBe('Example harbour read')
    const [[, body]] = postBodies(fetchMock)
    expect(String(body.question)).toBe('[Pinned finding: "Example harbour read" (id=ex-read-1)]\n\nwhat changed?')
    expect(body.pinned_context).toEqual([{ kind: 'finding', id: 'ex-read-1', title: 'Example harbour read', text: 'A read about Example.' }])
    fireEvent.click(screen.getByTestId('phone-asking-clear'))
    expect(screen.queryByTestId('phone-asking')).toBeNull()
    expect(usePhoneShell.getState().asking).toBeNull()
  })

  it('record-only is sent as the grounding mode', async () => {
    const fetchMock = routed()
    vi.stubGlobal('fetch', fetchMock)
    render(<ConsultScreen />)
    fireEvent.click(screen.getByTestId('consult-record-only'))
    expect(screen.getByTestId('consult-record-only').getAttribute('aria-checked')).toBe('true')
    fireEvent.change(screen.getByTestId('consult-input'), { target: { value: 'q' } })
    await act(async () => {
      fireEvent.click(screen.getByTestId('consult-send'))
    })
    const [[, body]] = postBodies(fetchMock)
    expect(body.grounding_mode).toBe('record_only')
    expect(body.model).toBe('core')
    expect(body).not.toHaveProperty('allow_writes')
  })
})

describe('6 · the answer, the contract, Stop', () => {
  it('streams, settles, renders markdown with citation buttons, and the contract is one line that opens', async () => {
    const es = await ask(routed())
    expect(es.url).toContain(`/consult/stream/${REQUEST_ID}`)
    expect(screen.getByTestId('consult-stop')).toBeInTheDocument()
    act(() => es.emit({ type: 'step', kind: 'answer_delta', text: '**Forming** the answer' }))
    expect(screen.getByTestId('consult-answer-live').textContent).not.toContain('**')
    expect(screen.getByTestId('consult-pending-label').textContent).toMatch(/Writing the answer/)
    settle(es, {
      answer: MARKDOWN_ANSWER,
      cited_refs: [
        { kind: 'signal', id: REF_A, description: 'Port strike enters its third day' },
        { kind: 'finding', id: REF_B },
      ],
      contract: CONTRACT,
    })
    const turn = await screen.findByTestId('consult-turn-assistant')
    expect(turn.textContent).not.toContain('**')
    expect(turn.querySelector('table')).not.toBeNull()
    const chips = within(turn).getAllByTestId('citation-chip')
    expect(chips).toHaveLength(2)
    for (const c of chips) expect(c.tagName).toBe('BUTTON')
    expect(turn.textContent).not.toContain('a1b2c3d4')
    // the contract: one line, then the shared block on a tap
    const line = within(turn).getByTestId('phone-contract-toggle')
    expect(line.textContent).toBe('Partial answer · 7–14 Nov · records on 6 of 7 days')
    expect(within(turn).queryByTestId('consult-contract')).toBeNull()
    fireEvent.click(line)
    expect(within(within(turn).getByTestId('consult-contract')).getByTestId('consult-contract-partial').textContent).toMatch(/Partial answer/)
    expect(screen.getByTestId('consult-send')).toBeInTheDocument()
    expect(useConsultSessions.getState().panel(MOBILE_PANEL_ID).sessionId).toBe('sess-m')
  })

  it('Stop stops the run, then answers from what it gathered on the free route', async () => {
    const fetchMock = routed({ synth: () => ok(200, { answer: 'Answered from what it found.', synthesis_status: 'complete' }) })
    const es = await ask(fetchMock)
    await act(async () => {
      fireEvent.click(screen.getByTestId('consult-stop'))
    })
    const turn = await screen.findByTestId('consult-turn-assistant')
    expect(turn.textContent).toContain('Answered from what it found.')
    const posts = postBodies(fetchMock)
    expect(posts.map(([p]) => p)).toEqual(['/consult', `/consult/runs/${REQUEST_ID}/stop`, `/consult/runs/${REQUEST_ID}/synthesize`])
    expect(posts[2][1]).toEqual({ model: 'core' })
    expect(es.closed).toBe(true)
  })

  it('a failed run lands its partial and says so', async () => {
    const es = await ask(routed())
    act(() => es.emit({ type: 'final', final_source: 'registry', status: 'error', error_status: 504, error_detail: 'round 3 timed out', partial_answer: 'What it had gathered.' }))
    const turn = await screen.findByTestId('consult-turn-assistant')
    expect(turn.textContent).toContain('What it had gathered.')
    expect(within(turn).getByTestId('consult-partial').textContent).toMatch(/Partial answer/)
    expect(screen.getByTestId('consult-error').textContent).toContain('round 3 timed out')
  })

  it('a turn a reload orphaned is picked up again and says so', async () => {
    vi.stubGlobal('fetch', routed({ get: () => ok(200, { request_id: REQUEST_ID, status: 'running' }) }))
    useConsultSessions.setState({
      panels: {
        [MOBILE_PANEL_ID]: {
          ...useConsultSessions.getState().panel(MOBILE_PANEL_ID),
          sessionId: 'sess-m',
          pendingTurn: { requestId: REQUEST_ID, question: 'earlier?', mode: 'chat', startedAt: Date.now(), pageLoadId: 'an-earlier-load', steps: [] },
        },
      },
    })
    render(<ConsultScreen />)
    await waitFor(() => expect(screen.getByTestId('consult-pending-label').textContent).toMatch(/Picked up again after a reload/))
    expect(screen.getByTestId('consult-stop')).toBeInTheDocument()
  })
})

describe('4 · drive mode', () => {
  it('on: the turn is sent as a case; off: it is not', async () => {
    useDriveMode.setState({ on: true })
    const fetchMock = routed()
    await ask(fetchMock)
    expect(postBodies(fetchMock)[0][1].present_case).toBe(true)
    cleanup()
    useConsultSessions.setState({ panels: {} })
    resetCasePlayer()
    useDriveMode.setState({ on: false })
    FakeEventSource.instances = []
    const off = routed()
    await ask(off)
    expect(postBodies(off)[0][1]).not.toHaveProperty('present_case')
  })

  it('a recorded case is offered as a button; it navigates, refuses what the phone lacks by name, and Undo returns', () => {
    const unregister = [registerPhoneFramePort(), registerPhoneShellPort()]
    try {
      const rec = parseCaseRecord({
        version: 'case/1',
        request_id: 'r-offered',
        status: 'sealed',
        as_of: '2026-11-14T00:00:00Z',
        plane: 'core',
        vocabulary: 'a'.repeat(64),
        refused: 0,
        stopped_after_seq: null,
        complete: true,
        gap_at_seq: null,
        summary: null,
        actions: [
          { seq: 1, action: 'panel_action', args: { view: 'v4.map', action: 'open', args: {} }, at: null, cites: [], rests_on: [] },
          { seq: 2, action: 'panel_action', args: { view: 'system.source_health', action: 'open', args: {} }, at: null, cites: [], rests_on: [] },
        ],
      })!
      useConsultSessions.setState({
        panels: {
          [MOBILE_PANEL_ID]: {
            ...useConsultSessions.getState().panel(MOBILE_PANEL_ID),
            transcript: [
              { role: 'user', content: 'show me the map' },
              { role: 'assistant', content: 'Here is the map.', caseRecord: rec },
            ],
          },
        },
      })
      render(<ConsultScreen />)
      act(() => fireEvent.click(screen.getByTestId('consult-drive-offer')))
      expect(phoneScreenOf(window.location.hash).page).toEqual({ kind: 'map' })
      const refusals = screen.getAllByTestId('consult-drive-refusal')
      expect(refusals).toHaveLength(1)
      expect(refusals[0].textContent).toMatch(/is on the desktop only/)
      expect(refusals[0].textContent).not.toMatch(/system\.|not opened/)
      // the offer is gone once the case is the live one; Undo puts the screen back
      expect(screen.queryByTestId('consult-drive-offer')).toBeNull()
      act(() => fireEvent.click(screen.getByTestId('consult-drive-undo')))
      expect(phoneScreenOf(window.location.hash)).toMatchObject({ tab: 'consult', page: null })
    } finally {
      for (const f of unregister) f()
    }
  })

  it('a live case moving the phone shows Stop; STOP latches', () => {
    render(<ConsultScreen />)
    act(() => {
      casePlayer().beginLive('r-live', MOBILE_PANEL_ID)
    })
    expect(screen.getByTestId('consult-drive-strip').getAttribute('data-state')).toBe('moving')
    act(() => fireEvent.click(screen.getByTestId('consult-drive-stop')))
    expect(screen.queryByTestId('consult-drive-stop')).toBeNull()
  })
})
