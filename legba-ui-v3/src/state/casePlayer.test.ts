/**
 * C-1b/ui — the player store: it owns the case stream, executes admitted
 * actions in seq order, pauses on a gap and resumes from the record, latches
 * STOP within one step (and tells the stop route how far it got), restores the
 * snapshot, and replays a record with NO network call.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { casePlayer, resetCasePlayer, useCasePlayer } from './casePlayer'
import { registerCaseShell, type CaseShell } from '@/lib/caseRunner'
import { parseCaseRecord } from '@/lib/caseModel'
import { emptyMembers, resetScope, useScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'

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

const SCOPE = frame(1, 'set_scope', { kind: 'target', id: 't1' })
const FOCUS = frame(2, 'focus', { ref: { kind: 'finding', id: 'f1' } })
const CLOCK = frame(3, 'timeline', {
  start: '2026-09-26T12:00:00Z',
  end: '2026-10-03T12:00:00Z',
  span: null,
  at: null,
})
const LAYERS = frame(4, 'map', { view: 'reset', ref: null, layers: ['events'] })

function recordOf(frames: ReturnType<typeof frame>[], extra: Record<string, unknown> = {}) {
  return {
    version: 'case/1',
    request_id: 'req-1',
    status: 'sealed',
    as_of: '2026-10-03T12:05:00Z',
    plane: 'core',
    vocabulary: 'a'.repeat(64),
    refused: 0,
    stopped_after_seq: null,
    complete: true,
    gap_at_seq: null,
    summary: null,
    actions: frames.map(({ seq, action, args, at, cites, rests_on }) => ({ seq, action, args, at, cites, rests_on })),
    ...extra,
  }
}

const handlers = () => ({ onStep: vi.fn(), onTerminal: vi.fn(), onActorFinal: vi.fn() })

let shell: CaseShell
let unregister: () => void
const fetchMock = vi.fn()

beforeEach(() => {
  resetCasePlayer()
  resetScope()
  useSelection.getState().clear()
  useWorldState.setState({
    windowStartMs: 1_000,
    windowEndMs: 2_000,
    spanMs: 1_000,
    layers: { signals: true, findings: true, situations: true, entities: false, events: true },
  })
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  fetchMock.mockReset()
  fetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => ({ request_id: 'req-1', status: 'stopping' }) })
  vi.stubGlobal('fetch', fetchMock)
  shell = {
    mode: 'personal',
    openSingleton: vi.fn(() => ({})),
    openBound: vi.fn(),
    switchStance: vi.fn(),
    applyPreset: vi.fn(),
    runMission: vi.fn(),
    reopenConsult: vi.fn(),
    currentStance: vi.fn(() => 'morning_read'),
    serializeLayout: vi.fn(() => ({ grid: 'before' })),
    restoreLayout: vi.fn(),
  }
  unregister = registerCaseShell(shell)
})

afterEach(() => {
  unregister()
  resetCasePlayer()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

function startLive() {
  casePlayer().beginLive('req-1', 'p1')
  const h = handlers()
  casePlayer().openStream('/api/v1/consult/stream/req-1?token=', h)
  return { es: FakeEventSource.instances.at(-1)!, h }
}

describe('the player owns the case stream', () => {
  it('runs admitted actions in seq order and passes every step to the trace', () => {
    const { es, h } = startLive()
    es.emit(SCOPE)
    es.emit(FOCUS)
    expect(useScope.getState().scope).toMatchObject({ kind: 'target', id: 't1' })
    expect(useSelection.getState().selection).toMatchObject({ kind: 'finding', id: 'f1' })
    expect(h.onStep).toHaveBeenCalledTimes(2)
    const p = useCasePlayer.getState().player!
    expect(p.executedThrough).toBe(2)
    expect(p.steps.map((s) => s.state)).toEqual(['executed', 'executed'])
    expect(p.steps[0].restsOn).toBe('rests on 1 read')
  })

  it('takes the snapshot BEFORE step 1 moves anything', () => {
    const { es } = startLive()
    es.emit(SCOPE)
    const snap = useCasePlayer.getState().snapshot!
    expect(snap.scope).toBeNull()
    expect(snap.window).toEqual({ startMs: 1_000, endMs: 2_000, spanMs: 1_000 })
    expect(snap.stance).toBe('morning_read')
    expect(snap.layout).toEqual({ grid: 'before' })
  })

  it('closes the stream on the registry terminal frame and reconciles with the record', () => {
    const { es, h } = startLive()
    es.emit(SCOPE)
    es.emit({ type: 'final', final_source: 'registry', status: 'complete', response: { answer: 'a', case: recordOf([SCOPE]) } })
    expect(h.onTerminal).toHaveBeenCalled()
    expect(es.closed).toBe(true)
    expect(useCasePlayer.getState().player).toMatchObject({ status: 'done', sealed: true })
  })
})

describe('a missing seq', () => {
  it('pauses live execution; the terminal record resumes it in order', () => {
    const { es } = startLive()
    es.emit(SCOPE)
    es.emit(CLOCK) // seq 2 was dropped by the relay
    expect(useCasePlayer.getState().player!.status).toBe('paused_gap')
    // Held, not run: the window has not moved.
    expect(useWorldState.getState().windowStartMs).toBe(1_000)
    expect(useSelection.getState().selection).toBeNull()

    casePlayer().landRecord(recordOf([SCOPE, FOCUS, CLOCK]))
    expect(useSelection.getState().selection).toMatchObject({ id: 'f1' })
    expect(useWorldState.getState().windowStartMs).toBe(Date.parse('2026-09-26T12:00:00Z'))
    expect(useCasePlayer.getState().player!.executedThrough).toBe(3)
  })
})

describe('STOP', () => {
  it('latches within one step: the workspace stays at step k and later frames are not executed', async () => {
    const { es } = startLive()
    es.emit(SCOPE)
    es.emit(FOCUS)
    await casePlayer().stop()
    es.emit(CLOCK)
    es.emit(LAYERS)
    // The wall is exactly where step 2 left it.
    expect(useWorldState.getState().windowStartMs).toBe(1_000)
    expect(useWorldState.getState().layers.signals).toBe(true)
    expect(useSelection.getState().selection).toMatchObject({ id: 'f1' })
    const p = useCasePlayer.getState().player!
    expect(p.status).toBe('stopped')
    expect(p.steps.slice(2).map((s) => [s.seq, s.state, s.reason])).toEqual([
      [3, 'stopped', 'not executed — stopped'],
      [4, 'stopped', 'not executed — stopped'],
    ])
    // The stop route is told how far the wall actually got.
    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toMatch(/\/consult\/runs\/req-1\/stop$/)
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ executed_through_seq: 2 })
  })

  it('the record landing after a stop runs nothing more', async () => {
    const { es } = startLive()
    es.emit(SCOPE)
    await casePlayer().stop()
    casePlayer().landRecord(recordOf([SCOPE, FOCUS, CLOCK]))
    expect(useSelection.getState().selection).toBeNull()
    expect(useCasePlayer.getState().player!.status).toBe('stopped')
  })

  it('a failed stop route still holds the latch, and says so', async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 404, text: async () => 'unknown run', json: async () => ({}) })
    const { es } = startLive()
    es.emit(SCOPE)
    await casePlayer().stop()
    es.emit(FOCUS)
    expect(useSelection.getState().selection).toBeNull()
    expect(useCasePlayer.getState().stopError).toBeTruthy()
  })
})

describe('Restore', () => {
  it('returns scope, selection, window, layers and layout to the snapshot', () => {
    useScope.getState().setScope({ kind: 'situation', id: 's0', label: 's0', members: emptyMembers(), origin: 'test' })
    useSelection.getState().select({ kind: 'signal', id: 'sig0' })
    const before = {
      scope: useScope.getState().scope,
      selection: useSelection.getState().selection,
      layers: { ...useWorldState.getState().layers },
    }
    const { es } = startLive()
    es.emit(SCOPE)
    es.emit(FOCUS)
    es.emit(CLOCK)
    es.emit(LAYERS)
    casePlayer().landRecord(recordOf([SCOPE, FOCUS, CLOCK, LAYERS]))
    expect(useWorldState.getState().layers.signals).toBe(false)

    casePlayer().restore()
    expect(useScope.getState().scope).toEqual(before.scope)
    expect(useSelection.getState().selection).toEqual(before.selection)
    expect(useWorldState.getState()).toMatchObject({ windowStartMs: 1_000, windowEndMs: 2_000, spanMs: 1_000 })
    expect(useWorldState.getState().layers).toEqual(before.layers)
    expect(shell.restoreLayout).toHaveBeenCalledWith('morning_read', { grid: 'before' })
    expect(useCasePlayer.getState().clockDrive).toBeNull()
  })
})

describe('Replay', () => {
  it('re-runs a persisted record with no consult POST and no tool call, to the same end-state', () => {
    // Live first, to capture the end-state…
    const { es } = startLive()
    for (const f of [SCOPE, FOCUS, CLOCK, LAYERS]) es.emit(f)
    const live = {
      scope: useScope.getState().scope,
      selection: useSelection.getState().selection,
      window: [useWorldState.getState().windowStartMs, useWorldState.getState().windowEndMs],
      layers: { ...useWorldState.getState().layers },
    }
    casePlayer().restore()
    casePlayer().dismiss()
    fetchMock.mockClear()
    const esBefore = FakeEventSource.instances.length

    // …then replay the record.
    casePlayer().replay(parseCaseRecord(recordOf([SCOPE, FOCUS, CLOCK, LAYERS]))!, 'p1')
    while (casePlayer().stepReplay()) {
      /* step by step */
    }
    expect(useCasePlayer.getState().player!.status).toBe('done')
    expect({
      scope: useScope.getState().scope,
      selection: useSelection.getState().selection,
      window: [useWorldState.getState().windowStartMs, useWorldState.getState().windowEndMs],
      layers: { ...useWorldState.getState().layers },
    }).toEqual(live)
    expect(fetchMock).not.toHaveBeenCalled()
    expect(FakeEventSource.instances.length).toBe(esBefore)
  })

  it('a stopped record replays only through stopped_after_seq', () => {
    casePlayer().replay(
      parseCaseRecord(recordOf([SCOPE, FOCUS, CLOCK], { status: 'stopped', stopped_after_seq: 1 }))!,
      'p1',
    )
    while (casePlayer().stepReplay()) {
      /* step */
    }
    expect(useScope.getState().scope).toMatchObject({ id: 't1' })
    expect(useSelection.getState().selection).toBeNull()
    expect(useCasePlayer.getState().player!.steps.map((s) => s.state)).toEqual(['executed', 'stopped', 'stopped'])
  })

  it('A stopped record holding the two steps that ran replays exactly those two', () => {
    casePlayer().replay(
      parseCaseRecord(recordOf([SCOPE, FOCUS], { status: 'stopped', stopped_after_seq: 2 }))!,
      'p1',
    )
    while (casePlayer().stepReplay()) {
      /* step */
    }
    expect(useCasePlayer.getState().player!.steps.map((s) => s.state)).toEqual(['executed', 'executed'])
    expect(useScope.getState().scope).toMatchObject({ id: 't1' })
    expect(useSelection.getState().selection).not.toBeNull()
  })

  it('autoplay steps on a timer and STOP latches it', async () => {
    vi.useFakeTimers()
    casePlayer().replay(parseCaseRecord(recordOf([SCOPE, FOCUS, CLOCK]))!, 'p1')
    casePlayer().setAutoplay(true)
    vi.advanceTimersByTime(1300)
    expect(useScope.getState().scope).toMatchObject({ id: 't1' })
    await casePlayer().stop()
    vi.advanceTimersByTime(5000)
    expect(useSelection.getState().selection).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
