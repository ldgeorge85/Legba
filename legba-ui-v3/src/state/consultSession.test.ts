/**
 * The consult session store's rules (GLASS-4).
 *
 * The store is what makes a consult turn survive an unmount, so the parts
 * asserted here are the parts a silent regression would break invisibly: the
 * reconcile's "server truth wins, pending survives only while unanswered"
 * decision, the request-id addressing that keeps a late answer from landing in
 * the wrong conversation, the revision guard that stops a slow reconcile from
 * overwriting a fresh one, and the bounded, never-throwing persistence.
 *
 * The panel-level proof that all of this actually holds through a real Dockview
 * `api.clear()` lives in `src/lib/dockviewRuntime.test.tsx`.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { ConsultSessionDetail, ConsultTurnOut } from '@/lib/api'
import {
  CONSULT_PAGE_LOAD_ID,
  EMPTY_PANEL,
  MAX_PERSISTED_PANELS,
  MAX_PERSISTED_STEPS,
  MAX_PERSISTED_TURNS,
  isDetached,
  parseConsultPanels,
  reconcileWithServer,
  toPersisted,
  turnsFromServer,
  useConsultSessions,
  type ChatTurn,
  type ConsultPanelState,
  type PendingTurn,
} from './consultSession'

const PANEL = 'singleton:system.consult'
const STORAGE_KEY = 'legba_consult_panels_v1'

function pending(overrides: Partial<PendingTurn> = {}): PendingTurn {
  return {
    requestId: 'req-1',
    question: 'what moved in Brazil?',
    mode: 'chat',
    startedAt: Date.now(),
    pageLoadId: CONSULT_PAGE_LOAD_ID,
    steps: [],
    ...overrides,
  }
}

function serverTurn(role: 'user' | 'assistant', content: string): ConsultTurnOut {
  return {
    id: `${role}-${content}`,
    role,
    content,
    steps: [],
    tool_calls: [],
    cited_refs: [],
    finding_id: null,
    created_at: '2026-08-06T00:00:00Z',
  }
}

function session(turns: ConsultTurnOut[]): ConsultSessionDetail {
  return {
    id: 'sess-1',
    mode: 'chat',
    title: 't',
    task_id: null,
    run_id: null,
    created_at: null,
    updated_at: null,
    turns,
  }
}

function localState(overrides: Partial<ConsultPanelState> = {}): ConsultPanelState {
  return { ...EMPTY_PANEL, ...overrides }
}

beforeEach(() => {
  localStorage.clear()
  useConsultSessions.setState({ panels: {} })
})

// ---------------------------------------------------------------------------

describe('reconcileWithServer', () => {
  it('leaves everything alone when the server could not be read', () => {
    const local = localState({
      transcript: [{ role: 'user', content: 'q' }],
      pendingTurn: pending(),
    })
    const merged = reconcileWithServer(local, null)
    expect(merged.transcript).toBe(local.transcript)
    expect(merged.pendingTurn).toBe(local.pendingTurn)
  })

  it('never lets an empty server transcript erase a real conversation', () => {
    const local = localState({ transcript: [{ role: 'user', content: 'q' }] })
    expect(reconcileWithServer(local, session([])).transcript).toBe(local.transcript)
  })

  it('ignores a server transcript that is behind ours', () => {
    // `_persist_assistant_turn` is best-effort — an audit-write outage can
    // leave the server without a turn the operator has already read. Adopting
    // that as truth would erase the answer off the screen.
    const local = localState({
      transcript: [
        { role: 'user', content: 'q1' },
        { role: 'assistant', content: 'an answer the audit log never recorded' },
      ],
    })
    const merged = reconcileWithServer(local, session([serverTurn('user', 'q1')]))
    expect(merged.transcript).toBe(local.transcript)
  })

  it('still keeps an in-flight turn when the server read is behind', () => {
    const local = localState({
      transcript: [
        { role: 'user', content: 'q1' },
        { role: 'assistant', content: 'a1' },
        { role: 'user', content: 'q2' },
      ],
      pendingTurn: pending({ question: 'q2' }),
    })
    const merged = reconcileWithServer(local, session([serverTurn('user', 'q1')]))
    expect(merged.pendingTurn).toBe(local.pendingTurn)
  })

  it('adopts server truth for completed turns', () => {
    const local = localState({
      transcript: [{ role: 'user', content: 'stale local copy' }],
    })
    const merged = reconcileWithServer(
      local,
      session([serverTurn('user', 'q1'), serverTurn('assistant', 'a1')]),
    )
    expect(merged.transcript.map((t) => t.content)).toEqual(['q1', 'a1'])
    expect(merged.pendingTurn).toBeNull()
  })

  it('keeps a pending turn while the server shows it unanswered', () => {
    // The registry appends the USER turn before the actor runs — a trailing
    // user turn is exactly "still running".
    const local = localState({ pendingTurn: pending({ question: 'q2' }) })
    const merged = reconcileWithServer(
      local,
      session([
        serverTurn('user', 'q1'),
        serverTurn('assistant', 'a1'),
        serverTurn('user', 'q2'),
      ]),
    )
    expect(merged.pendingTurn).toBe(local.pendingTurn)
    expect(merged.transcript.map((t) => t.content)).toEqual(['q1', 'a1', 'q2'])
  })

  it('drops the pending turn once the server has recorded its answer', () => {
    const local = localState({ pendingTurn: pending({ question: 'q2' }) })
    const merged = reconcileWithServer(
      local,
      session([
        serverTurn('user', 'q2'),
        serverTurn('assistant', 'the answer that arrived while we were away'),
      ]),
    )
    expect(merged.pendingTurn).toBeNull()
    expect(merged.transcript.map((t) => t.content)).toEqual([
      'q2',
      'the answer that arrived while we were away',
    ])
  })

  it('matches through the pinned-context prefix the panel prepends', () => {
    // The panel sends `[Pinned …]\n\nquestion` but shows the question as typed,
    // so the server's user turn only ENDS with what we hold.
    const local = localState({ pendingTurn: pending({ question: 'why is this rising?' }) })
    const merged = reconcileWithServer(
      local,
      session([
        serverTurn(
          'user',
          '[Pinned finding: "X" (id=abc)]\n\nwhy is this rising?',
        ),
      ]),
    )
    expect(merged.pendingTurn).toBe(local.pendingTurn)
  })

  it('drops a pending turn the server transcript has moved past', () => {
    // A trailing assistant turn that does not answer OUR question means the
    // request never landed; keeping it would strand the panel on a turn that
    // is never coming.
    const local = localState({ pendingTurn: pending({ question: 'never sent' }) })
    const merged = reconcileWithServer(
      local,
      session([serverTurn('user', 'q1'), serverTurn('assistant', 'a1')]),
    )
    expect(merged.pendingTurn).toBeNull()
  })
})

describe('turnsFromServer', () => {
  it('marks a turn carrying a finding id as a deep turn', () => {
    const detail = session([serverTurn('assistant', 'a')])
    detail.turns[0].finding_id = 'f-1'
    const [turn] = turnsFromServer(detail)
    expect(turn.deep).toBe(true)
    expect(turn.findingId).toBe('f-1')
  })
})

// ---------------------------------------------------------------------------

describe('page-load identity', () => {
  it('treats a turn from this page load as still attached', () => {
    expect(isDetached(pending())).toBe(false)
  })

  it('treats a turn restored from a previous page as detached', () => {
    expect(isDetached(pending({ pageLoadId: 'pl-some-older-page' }))).toBe(true)
    expect(isDetached(null)).toBe(false)
  })

  it('treats a stamp-less persisted turn as detached (the safe reading)', () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        [PANEL]: { pendingTurn: { requestId: 'r', question: 'q' } },
      }),
    )
    const parsed = parseConsultPanels(localStorage.getItem(STORAGE_KEY))
    expect(isDetached(parsed[PANEL].pendingTurn)).toBe(true)
  })
})

// ---------------------------------------------------------------------------

describe('store actions', () => {
  it('appends the user turn and marks it pending on send', () => {
    const store = useConsultSessions.getState()
    store.startTurn(PANEL, pending({ question: 'q1' }))
    const panel = useConsultSessions.getState().panel(PANEL)
    expect(panel.transcript).toEqual([{ role: 'user', content: 'q1' }])
    expect(panel.pendingTurn?.requestId).toBe('req-1')
  })

  it('clears the composer draft when a turn opens', () => {
    const store = useConsultSessions.getState()
    store.setDraft(PANEL, 'half typed')
    store.startTurn(PANEL, pending())
    expect(useConsultSessions.getState().panel(PANEL).draft).toBe('')
  })

  it('lands the answer and clears the pending turn', () => {
    const store = useConsultSessions.getState()
    store.startTurn(PANEL, pending({ question: 'q1' }))
    store.pushStep(PANEL, 'req-1', { type: 'step', phase: 'plan' })
    const answer: ChatTurn = { role: 'assistant', content: 'a1' }
    store.completeTurn(PANEL, 'req-1', answer)

    const panel = useConsultSessions.getState().panel(PANEL)
    expect(panel.transcript.map((t) => t.content)).toEqual(['q1', 'a1'])
    expect(panel.pendingTurn).toBeNull()
  })

  it('ignores an answer addressed to a turn that is no longer pending', () => {
    // The operator hit "New chat" while a turn was in flight. Its POST is still
    // running; when it resolves it must not graft onto the fresh conversation.
    const store = useConsultSessions.getState()
    store.startTurn(PANEL, pending({ requestId: 'old' }))
    store.reset(PANEL)
    store.completeTurn(PANEL, 'old', { role: 'assistant', content: 'late answer' })

    const panel = useConsultSessions.getState().panel(PANEL)
    expect(panel.transcript).toEqual([])
    expect(panel.pendingTurn).toBeNull()
  })

  it('ignores step frames for a settled or superseded turn', () => {
    const store = useConsultSessions.getState()
    store.startTurn(PANEL, pending({ requestId: 'r1' }))
    store.pushStep(PANEL, 'r2', { type: 'step', phase: 'stale' })
    expect(useConsultSessions.getState().panel(PANEL).pendingTurn?.steps).toEqual([])
  })

  it('records a failure against the pending turn and releases the composer', () => {
    const store = useConsultSessions.getState()
    store.startTurn(PANEL, pending({ question: 'q1' }))
    store.failTurn(PANEL, 'req-1', 'consult 500: kaboom')

    const panel = useConsultSessions.getState().panel(PANEL)
    expect(panel.pendingTurn).toBeNull()
    expect(panel.error).toBe('consult 500: kaboom')
    // The question stays on the transcript — a failed turn is not an erased one.
    expect(panel.transcript).toEqual([{ role: 'user', content: 'q1' }])
  })

  it('dedupes pins and removes them by (kind, id)', () => {
    const store = useConsultSessions.getState()
    store.addPin(PANEL, { kind: 'finding', id: 'f1', label: 'F1' })
    store.addPin(PANEL, { kind: 'finding', id: 'f1', label: 'F1 again' })
    store.addPin(PANEL, { kind: 'target', id: 't1' })
    expect(useConsultSessions.getState().panel(PANEL).pins).toHaveLength(2)

    store.removePin(PANEL, 'finding', 'f1')
    expect(useConsultSessions.getState().panel(PANEL).pins).toEqual([
      { kind: 'target', id: 't1' },
    ])
  })

  it('keeps panels independent — one conversation never bleeds into another', () => {
    const store = useConsultSessions.getState()
    store.startTurn('panel-a', pending({ question: 'a-question' }))
    store.startTurn('panel-b', pending({ requestId: 'req-b', question: 'b-question' }))
    store.completeTurn('panel-a', 'req-1', { role: 'assistant', content: 'a-answer' })

    expect(useConsultSessions.getState().panel('panel-a').pendingTurn).toBeNull()
    expect(useConsultSessions.getState().panel('panel-b').pendingTurn?.question).toBe(
      'b-question',
    )
  })

  it('returns a stable default slice for an unknown panel id', () => {
    expect(useConsultSessions.getState().panel('never-seen')).toBe(EMPTY_PANEL)
  })
})

describe('reconcile revision guard', () => {
  it('refuses to apply a server read that raced a newer local change', () => {
    const store = useConsultSessions.getState()
    store.startTurn(PANEL, pending({ question: 'q1' }))
    const staleRev = useConsultSessions.getState().panel(PANEL).rev

    // The answer lands while the reconcile round-trip is still in the air.
    store.completeTurn(PANEL, 'req-1', { role: 'assistant', content: 'the real answer' })

    // The now-stale reconcile arrives, carrying a server transcript that
    // predates the answer. Applying it would silently undo the answer.
    store.reconcile(PANEL, session([serverTurn('user', 'q1')]), staleRev)

    expect(
      useConsultSessions.getState().panel(PANEL).transcript.map((t) => t.content),
    ).toEqual(['q1', 'the real answer'])
  })

  it('applies a reconcile that did not race anything', () => {
    const store = useConsultSessions.getState()
    store.setSessionId(PANEL, 'sess-1')
    store.startTurn(PANEL, pending({ question: 'q1' }))
    const rev = useConsultSessions.getState().panel(PANEL).rev

    store.reconcile(
      PANEL,
      session([serverTurn('user', 'q1'), serverTurn('assistant', 'recovered')]),
      rev,
    )

    const panel = useConsultSessions.getState().panel(PANEL)
    expect(panel.pendingTurn).toBeNull()
    expect(panel.transcript.map((t) => t.content)).toEqual(['q1', 'recovered'])
  })
})

// ---------------------------------------------------------------------------

describe('persistence', () => {
  it('writes the conversation to localStorage on every mutation', () => {
    const store = useConsultSessions.getState()
    store.startTurn(PANEL, pending({ question: 'q1' }))
    store.setSessionId(PANEL, 'sess-1')

    const raw = parseConsultPanels(localStorage.getItem(STORAGE_KEY))
    expect(raw[PANEL].sessionId).toBe('sess-1')
    expect(raw[PANEL].transcript).toEqual([{ role: 'user', content: 'q1' }])
    expect(raw[PANEL].pendingTurn?.question).toBe('q1')
  })

  it('round-trips pins and the composer draft', () => {
    const store = useConsultSessions.getState()
    store.addPin(PANEL, { kind: 'finding', id: 'f1', label: 'F1' })
    store.setDraft(PANEL, 'half-typed question')
    store.setScope(PANEL, 'target.id == "brazil"')
    store.setMaxRounds(PANEL, 22)

    const restored = parseConsultPanels(localStorage.getItem(STORAGE_KEY))[PANEL]
    expect(restored.pins).toEqual([{ kind: 'finding', id: 'f1', label: 'F1' }])
    expect(restored.draft).toBe('half-typed question')
    expect(restored.scope).toBe('target.id == "brazil"')
    expect(restored.maxRounds).toBe(22)
  })

  it('does not persist the transient error', () => {
    const store = useConsultSessions.getState()
    store.startTurn(PANEL, pending())
    store.failTurn(PANEL, 'req-1', 'consult 500: kaboom')
    expect(parseConsultPanels(localStorage.getItem(STORAGE_KEY))[PANEL].error).toBeNull()
  })

  it('survives garbage in localStorage without crashing the shell', () => {
    expect(parseConsultPanels(null)).toEqual({})
    expect(parseConsultPanels('not json')).toEqual({})
    expect(parseConsultPanels('[1,2,3]')).toEqual({})
    expect(parseConsultPanels('{"p": 7}')).toEqual({})
  })

  it('drops malformed turns and pins without taking their neighbours', () => {
    const parsed = parseConsultPanels(
      JSON.stringify({
        [PANEL]: {
          transcript: [
            { role: 'user', content: 'good' },
            { role: 'nonsense', content: 'bad role' },
            { role: 'assistant' },
            null,
          ],
          pins: [{ kind: 'finding', id: 'f1' }, { kind: 'finding' }, 'nope'],
        },
      }),
    )
    expect(parsed[PANEL].transcript).toEqual([{ role: 'user', content: 'good' }])
    expect(parsed[PANEL].pins).toEqual([{ kind: 'finding', id: 'f1' }])
  })

  it('caps every axis that could otherwise grow without bound', () => {
    const fat: ConsultPanelState = localState({
      transcript: Array.from({ length: MAX_PERSISTED_TURNS + 25 }, (_, i) => ({
        role: 'user' as const,
        content: `turn-${i}`,
        steps: Array.from({ length: MAX_PERSISTED_STEPS + 30 }, () => ({ type: 'step' })),
        toolCalls: Array.from({ length: 40 }, () => ({ tool: 't', args: {}, result: 1 })),
      })),
      pendingTurn: pending({
        steps: Array.from({ length: MAX_PERSISTED_STEPS + 30 }, () => ({ type: 'step' })),
      }),
      updatedAt: 5,
    })

    const persisted = toPersisted({ [PANEL]: fat })[PANEL]
    expect(persisted.transcript).toHaveLength(MAX_PERSISTED_TURNS)
    // The NEWEST turns are the ones kept.
    expect(persisted.transcript[persisted.transcript.length - 1].content).toBe(
      `turn-${MAX_PERSISTED_TURNS + 24}`,
    )
    expect(persisted.transcript[0].steps).toHaveLength(MAX_PERSISTED_STEPS)
    expect(persisted.transcript[0].toolCalls).toHaveLength(10)
    expect(persisted.pendingTurn?.steps).toHaveLength(MAX_PERSISTED_STEPS)
  })

  it('evicts the least-recently-touched panels past the cap', () => {
    const panels: Record<string, ConsultPanelState> = {}
    for (let i = 0; i < MAX_PERSISTED_PANELS + 4; i++) {
      panels[`panel-${i}`] = localState({ updatedAt: i })
    }
    const persisted = toPersisted(panels)
    expect(Object.keys(persisted)).toHaveLength(MAX_PERSISTED_PANELS)
    expect(persisted['panel-0']).toBeUndefined()
    expect(persisted[`panel-${MAX_PERSISTED_PANELS + 3}`]).toBeDefined()
  })

  // jsdom's `localStorage` is a Proxy that turns property assignment into a
  // stored ITEM, so the method can only be replaced on the prototype.
  it('keeps working when localStorage refuses the write', () => {
    const spy = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => {
        throw new DOMException('QuotaExceededError')
      })
    try {
      useConsultSessions.getState().startTurn(PANEL, pending({ question: 'q1' }))
      // Quota exhaustion: the full write AND the austere retry both fail.
      expect(spy.mock.calls.length).toBeGreaterThanOrEqual(2)
    } finally {
      spy.mockRestore()
    }
    // The in-memory conversation is untouched — persistence is best-effort and
    // a storage outage must never break a working panel.
    expect(useConsultSessions.getState().panel(PANEL).transcript).toEqual([
      { role: 'user', content: 'q1' },
    ])
  })

  it('falls back to an austere projection when the full write is too big', () => {
    const spy = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementationOnce(() => {
        throw new DOMException('QuotaExceededError')
      })
    try {
      useConsultSessions
        .getState()
        .startTurn(PANEL, pending({ question: 'q1', steps: [{ type: 'step' }] }))
    } finally {
      spy.mockRestore()
    }

    const restored = parseConsultPanels(localStorage.getItem(STORAGE_KEY))[PANEL]
    // The conversation text is what matters; the trace is what gets dropped.
    expect(restored.transcript).toEqual([{ role: 'user', content: 'q1' }])
    expect(restored.pendingTurn?.steps).toEqual([])
  })
})
