/**
 * C-1b/ui — Consult presenting a case (U-2: under the "Drive the view" mode).
 *
 *   * the mode switch sits beside Send, is OFF by default and STAYS as set;
 *     a plain turn's request body carries no `present_case` key at all;
 *   * while a case plays the rail renders with a STOP that is visible;
 *   * the PLAYER owns the case stream: unmounting the tile mid-case keeps the
 *     actions flowing and the turn still settles (note §7 test 13);
 *   * Replay runs a reloaded turn's record with no network call.
 */
import { useDriveMode } from '@/state/driveMode'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

import ConsultPanel from './Consult'
import { useConsultSessions } from '@/state/consultSession'
import { resetCasePlayer, useCasePlayer } from '@/state/casePlayer'
import { registerCaseShell, type CaseShell } from '@/lib/caseRunner'
import { parseCaseRecord } from '@/lib/caseModel'
import { resetScope, useScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
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
    created_at: '2026-10-03T00:00:00Z',
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

function frame(seq: number, action: string, args: Record<string, unknown>) {
  return {
    type: 'step',
    phase: 'case',
    kind: 'case_action',
    seq,
    action,
    args,
    cites: [{ kind: 'target', id: 't1' }],
    rests_on: [{ round: 1, tool: 'list_targets' }],
    at: '2026-10-03T12:00:00Z',
    round: 2,
  }
}

function caseRecord() {
  const frames = [
    frame(1, 'set_scope', { kind: 'target', id: 't1' }),
    frame(2, 'narrate', { text: 'this desk moved overnight' }),
    frame(3, 'focus', { ref: { kind: 'finding', id: 'f1' } }),
  ]
  return {
    version: 'case/1',
    request_id: 'req-old',
    status: 'sealed',
    as_of: '2026-10-02T09:00:00Z',
    plane: 'core',
    vocabulary: 'a'.repeat(64),
    refused: 0,
    stopped_after_seq: null,
    complete: true,
    gap_at_seq: null,
    summary: 'one desk, one finding',
    actions: frames.map(({ seq, action, args, at, cites, rests_on }) => ({ seq, action, args, at, cites, rests_on })),
  }
}

let fetchMock: ReturnType<typeof vi.fn>
let unregister: () => void

beforeEach(() => {
  useDriveMode.setState({ on: false })
  localStorage.clear()
  useConsultSessions.setState({ panels: {} })
  resetCasePlayer()
  resetScope()
  useSelection.getState().clear()
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  let n = 0
  vi.stubGlobal('crypto', {
    ...globalThis.crypto,
    randomUUID: () => `00000000-0000-0000-0000-00000000000${++n}`,
  })
  // The POST never resolves: a turn stays in flight unless a frame settles it.
  fetchMock = vi.fn().mockImplementation(() => new Promise(() => {}))
  vi.stubGlobal('fetch', fetchMock)
  const shell: CaseShell = {
    mode: 'personal',
    openSingleton: vi.fn(() => ({})),
    openBound: vi.fn(),
    switchStance: vi.fn(),
    applyPreset: vi.fn(),
    runMission: vi.fn(),
    reopenConsult: vi.fn(),
    currentStance: vi.fn(() => 'morning_read'),
    serializeLayout: vi.fn(() => null),
    restoreLayout: vi.fn(),
  }
  unregister = registerCaseShell(shell)
})

afterEach(() => {
  unregister()
  resetCasePlayer()
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

describe('the "Drive the view" mode (U-2; was the per-turn "Present as a case", D1)', () => {
  it('is off by default, in the ⋯ menu, and a plain turn sends no present_case key', () => {
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    openConsultMenu()
    const toggle = screen.getByTestId('consult-drive-toggle')
    expect(toggle.getAttribute('aria-checked')).toBe('false')
    expect(screen.getByTestId('consult-more-menu').contains(toggle)).toBe(true)
    ask('plain question')
    expect(postBodies()).toHaveLength(1)
    expect('present_case' in postBodies()[0]).toBe(false)
    expect(useCasePlayer.getState().player).toBeNull()
  })

  it('sends present_case while it is on, and STAYS on — it is a mode, not a per-turn choice', () => {
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    openConsultMenu()
    fireEvent.click(screen.getByTestId('consult-drive-toggle'))
    openConsultMenu()
    expect(screen.getByTestId('consult-drive-toggle').getAttribute('aria-checked')).toBe('true')
    ask('present this')
    expect(postBodies()[0].present_case).toBe(true)
    // A case turn left at the panel's default cap goes out at the case default.
    expect(postBodies()[0].max_tool_rounds).toBe(20)
    openConsultMenu()
    expect(screen.getByTestId('consult-drive-toggle').getAttribute('aria-checked')).toBe('true')
    expect(localStorage.getItem('legba_consult_drive')).toBe('on')
  })

  it('says in its label and one plain line what on and off do', () => {
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    openConsultMenu()
    expect(screen.getByTestId('consult-drive-toggle')).toHaveTextContent('Drive the view: off')
    openConsultMenu()
    expect(screen.getByTestId('consult-drive-hint')).toHaveTextContent('Off: answers only reply; nothing on screen moves.')
    openConsultMenu()
    fireEvent.click(screen.getByTestId('consult-drive-toggle'))
    openConsultMenu()
    expect(screen.getByTestId('consult-drive-toggle')).toHaveTextContent('Drive the view: on')
    openConsultMenu()
    expect(screen.getByTestId('consult-drive-hint')).toHaveTextContent('Undo puts the view back')
  })

  it('a plain turn keeps the panel cap', () => {
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    ask('plain')
    expect(postBodies()[0].max_tool_rounds).toBe(10)
  })
})

describe('while a case plays', () => {
  it('renders the rail, the census line per step, and a visible STOP', async () => {
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    openConsultMenu()
    fireEvent.click(screen.getByTestId('consult-drive-toggle'))
    ask('present this')
    const es = FakeEventSource.instances.at(-1)!
    act(() => {
      es.emit(frame(1, 'set_scope', { kind: 'target', id: 't1' }))
      es.emit({ ...frame(2, 'narrate', { text: 'look at the desk' }), rests_on: [] })
    })
    await waitFor(() => expect(screen.getByTestId('case-rail')).toBeInTheDocument())
    expect(screen.getByTestId('case-stop')).toBeVisible()
    const steps = screen.getAllByTestId('case-step')
    expect(steps).toHaveLength(2)
    const census = screen.getAllByTestId('case-step-census').map((e) => e.textContent)
    expect(census).toEqual(['rests on 1 read', 'no read'])
    expect(screen.getByTestId('case-narration')).toHaveTextContent('look at the desk')
    expect(screen.getByTestId('case-step-counter')).toHaveTextContent('step 2 of 2')
    // The mode cannot be flipped under a running case.
    openConsultMenu()
    expect(screen.getByTestId('consult-drive-toggle')).toBeDisabled()
  })

  it('STOP latches: a later frame renders not executed — stopped', async () => {
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    openConsultMenu()
    fireEvent.click(screen.getByTestId('consult-drive-toggle'))
    ask('present this')
    const es = FakeEventSource.instances.at(-1)!
    act(() => es.emit(frame(1, 'set_scope', { kind: 'target', id: 't1' })))
    await act(async () => {
      fireEvent.click(screen.getByTestId('case-stop'))
    })
    act(() => es.emit(frame(2, 'focus', { ref: { kind: 'finding', id: 'f1' } })))
    expect(useSelection.getState().selection).toBeNull()
    await waitFor(() =>
      expect(screen.getAllByTestId('case-step')[1].getAttribute('data-state')).toBe('stopped'),
    )
    expect(screen.queryByTestId('case-stop')).toBeNull()
  })

  it('the player owns the stream: unmounting the tile mid-case keeps actions flowing', async () => {
    const view = render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    openConsultMenu()
    fireEvent.click(screen.getByTestId('consult-drive-toggle'))
    ask('present this')
    const es = FakeEventSource.instances.at(-1)!
    act(() => es.emit(frame(1, 'arrange', { layout: 'workspace', layout_type: 'preset' })))
    // The preset's api.clear() unmounts the tile…
    view.unmount()
    expect(es.closed).toBe(false)
    // …and the case keeps going.
    act(() => {
      es.emit(frame(2, 'set_scope', { kind: 'target', id: 't1' }))
      es.emit(frame(3, 'focus', { ref: { kind: 'finding', id: 'f1' } }))
    })
    expect(useScope.getState().scope).toMatchObject({ kind: 'target', id: 't1' })
    expect(useSelection.getState().selection).toMatchObject({ kind: 'finding', id: 'f1' })
    // The terminal frame still settles the turn, through the store.
    act(() =>
      es.emit({
        type: 'final',
        final_source: 'registry',
        status: 'complete',
        response: { answer: 'the case answer', finding_id: null, derived_from: [], tool_calls: [], cited_refs: [], case: caseRecord() },
      }),
    )
    const slice = useConsultSessions.getState().panels[PANEL]
    expect(slice.pendingTurn).toBeNull()
    expect(slice.transcript.at(-1)).toMatchObject({ role: 'assistant', content: 'the case answer' })
    expect(slice.transcript.at(-1)?.caseRecord?.actions).toHaveLength(3)
    expect(es.closed).toBe(true)
  })
})

describe('Replay', () => {
  function seedReloadedTurn(extra: Record<string, unknown> = {}) {
    useConsultSessions.setState({
      panels: {
        [PANEL]: {
          ...useConsultSessions.getState().panel(PANEL),
          sessionId: null,
          transcript: [
            { role: 'user', content: 'show me' },
            {
              role: 'assistant',
              content: 'the answer',
              caseRecord: parseCaseRecord(caseRecord()),
              responseRecorded: true,
              ...extra,
            },
          ],
        },
      },
    })
  }

  it('re-runs the persisted case without the model — no network call', async () => {
    seedReloadedTurn()
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    expect(screen.getByTestId('consult-case-line')).toHaveTextContent('case · sealed · 3 steps · recorded 2026-10-02T09:00:00Z')
    fetchMock.mockClear()
    const esBefore = FakeEventSource.instances.length
    fireEvent.click(screen.getByTestId('consult-case-replay'))
    await waitFor(() => expect(screen.getByTestId('case-rail')).toBeInTheDocument())
    expect(screen.getByTestId('case-replay-caption')).toHaveTextContent(
      'Replaying a case recorded 2026-10-02T09:00:00Z; panels show the record as it stands now.',
    )
    expect(screen.getByTestId('case-stop')).toBeVisible()
    for (let i = 0; i < 3; i++) fireEvent.click(screen.getByTestId('case-replay-next'))
    expect(useScope.getState().scope).toMatchObject({ kind: 'target', id: 't1' })
    expect(useSelection.getState().selection).toMatchObject({ kind: 'finding', id: 'f1' })
    await waitFor(() => expect(screen.getByTestId('case-status')).toHaveTextContent('sealed'))
    expect(screen.getByTestId('case-restore')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(FakeEventSource.instances.length).toBe(esBefore)
  })

  it('a held turn shows the amber banner before step 1', async () => {
    seedReloadedTurn({ holdReason: 'no_live_or_held_refs' })
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    fireEvent.click(screen.getByTestId('consult-case-replay'))
    await waitFor(() => expect(screen.getByTestId('case-held')).toBeInTheDocument())
    expect(useScope.getState().scope).toBeNull()
  })

  it('a turn with no case offers no Replay', () => {
    useConsultSessions.setState({
      panels: {
        [PANEL]: {
          ...useConsultSessions.getState().panel(PANEL),
          transcript: [
            { role: 'user', content: 'q' },
            { role: 'assistant', content: 'a' },
          ],
        },
      },
    })
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    expect(screen.queryByTestId('consult-case-replay')).toBeNull()
  })
})
