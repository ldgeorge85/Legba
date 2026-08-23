/**
 * The consult panel through the REAL Dockview runtime (GLASS-4).
 *
 * Why real Dockview and not a bare `render()`
 * ===========================================
 *
 * The defect this suite pins was invisible to component tests precisely
 * because it lived in the shell: `applyPreset` and both Investigate grids call
 * `DockviewApi.clear()`, Dockview destroys the panel's React tree, and the
 * consult conversation — which used to be `useState` inside that tree — went
 * with it. A test that renders `<ConsultPanel />` directly never exercises the
 * thing that does the destroying, so it can pass forever while every preset
 * pick silently drops an in-flight turn.
 *
 * So these tests drive the actual `DockviewReact` component, mount the panel
 * through `addPanel` the way `App.tsx` does, and clear it through the real
 * `applyPreset`. The panel is reached only via the dock — nothing here reaches
 * around it.
 *
 * What each test pins
 * ===================
 *
 *   * a turn in flight when a preset is picked still lands its answer, and the
 *     reopened panel shows it — the headline defect;
 *   * the transcript and the pinned context survive the same clear;
 *   * the EventSource is closed on unmount (the leak that shipped alongside —
 *     `Consult.tsx` had no cleanup at all) while the turn keeps running;
 *   * a turn orphaned by a RELOAD is recovered from the server, and one the
 *     server never answers stops pretending to be live.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import {
  DockviewReact,
  type DockviewApi,
  type DockviewReadyEvent,
  type IDockviewPanelProps,
} from 'dockview-react'
import ConsultPanel from '@/panels/system/Consult'
import { applyPreset, findPreset } from '@/lib/layoutPresets'
import {
  CONSULT_PAGE_LOAD_ID,
  parseConsultPanels,
  useConsultSessions,
  type PendingTurn,
} from '@/state/consultSession'
import type { PanelRegistration } from '@/types'

/** The store key a singleton consult panel gets — see `App.tsx`. */
const CONSULT_PANEL_ID = 'singleton:system.consult'
const STORAGE_KEY = 'legba_consult_panels_v1'

/**
 * The synthetic registration `App.tsx` mints for a singleton panel. The `id` is
 * what keys the store, and it is deterministic on purpose: that is what lets a
 * panel re-opened after `api.clear()` rejoin its own conversation.
 */
function singletonRegistration(): PanelRegistration {
  return {
    id: CONSULT_PANEL_ID,
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
    created_at: '2026-08-06T00:00:00Z',
    retired_at: null,
  }
}

function DockPanel(_props: IDockviewPanelProps) {
  return <ConsultPanel registration={singletonRegistration()} scope={{}} mode="personal" />
}

/** A stand-in for every non-consult kind a preset opens. */
function OtherPanel() {
  return <div data-testid="other-panel" />
}

const components = { default: DockPanel, other: OtherPanel }

// --- EventSource stub, capturing instances so leaks are visible -------------
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
  static get open(): FakeEventSource[] {
    return FakeEventSource.instances.filter((es) => !es.closed)
  }
}

/** Mount the dock and hand back its api. */
async function mountDock(): Promise<DockviewApi> {
  let api: DockviewApi | null = null
  render(
    <div style={{ width: 1200, height: 800 }}>
      <DockviewReact
        components={components}
        onReady={(e: DockviewReadyEvent) => {
          api = e.api
        }}
      />
    </div>,
  )
  await waitFor(() => expect(api).not.toBeNull())
  return api as unknown as DockviewApi
}

/** Open the consult panel the way `App.tsx`'s `addSingleton` does. */
function openConsult(api: DockviewApi): void {
  act(() => {
    api.addPanel({
      id: 'system.consult',
      component: 'default',
      title: 'Consult',
      params: { registration: null, singletonKind: 'system.consult', mode: 'personal' },
    })
  })
}

function pendingTurn(overrides: Partial<PendingTurn> = {}): PendingTurn {
  return {
    requestId: 'req-reload',
    question: 'what happened while I was gone?',
    mode: 'chat',
    startedAt: Date.now(),
    pageLoadId: CONSULT_PAGE_LOAD_ID,
    steps: [],
    ...overrides,
  }
}

/**
 * A `GET /consult/sessions/{id}` body. `steps` is modelled because the
 * registry really does persist the ReAct trace onto the assistant turn
 * (`_persist_assistant_turn(..., steps=_steps_from_payload(...))` →
 * `consult_turns.steps`), and a stub that dropped it would misrepresent what a
 * reconciled turn looks like.
 */
function sessionDetail(
  turns: { role: 'user' | 'assistant'; content: string; steps?: unknown[] }[],
) {
  return {
    id: 'sess-1',
    mode: 'chat',
    title: 't',
    task_id: null,
    run_id: null,
    created_at: null,
    updated_at: null,
    turns: turns.map((t, i) => ({
      id: `t${i}`,
      role: t.role,
      content: t.content,
      steps: t.steps ?? [],
      tool_calls: [],
      cited_refs: [],
      finding_id: null,
      created_at: null,
    })),
  }
}

/** A `fetch` stub routing the two endpoints the panel touches. */
function stubFetch(handlers: {
  consult?: () => Promise<unknown>
  session?: () => unknown
  sessions?: () => unknown
}) {
  const mock = vi.fn(async (url: string) => {
    const u = String(url)
    if (u.includes('/consult/sessions/')) {
      return { ok: true, json: async () => handlers.session?.() ?? sessionDetail([]) }
    }
    if (u.includes('/consult/sessions')) {
      return { ok: true, json: async () => handlers.sessions?.() ?? [] }
    }
    if (u.includes('/consult')) {
      return handlers.consult ? handlers.consult() : { ok: true, json: async () => ({}) }
    }
    return { ok: true, json: async () => ({}) }
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

function answer(text: string) {
  return {
    ok: true,
    json: async () => ({
      answer: text,
      finding_id: null,
      derived_from: [],
      tool_calls: [],
      cited_refs: [],
      session_id: 'sess-1',
    }),
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  useConsultSessions.setState({ panels: {} })
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  vi.stubGlobal('crypto', {
    ...globalThis.crypto,
    randomUUID: () => '00000000-0000-0000-0000-00000000000a',
  })
})

afterEach(() => {
  cleanup()
})

// ---------------------------------------------------------------------------

describe('consult panel inside the real Dockview runtime', () => {
  it('mounts through addPanel and renders its composer', async () => {
    stubFetch({})
    const api = await mountDock()
    openConsult(api)
    expect(await screen.findByTestId('consult-question')).toBeInTheDocument()
  })

  it('applyPreset really does destroy the panel (the mechanism under test)', async () => {
    stubFetch({})
    const api = await mountDock()
    openConsult(api)
    await screen.findByTestId('consult-question')

    // The Workspace preset opens `system.consult` among others; every preset
    // starts with `api.clear()`, and that is the destructive step.
    act(() => {
      api.clear()
    })
    await waitFor(() =>
      expect(screen.queryByTestId('consult-question')).not.toBeInTheDocument(),
    )
    expect(api.panels).toHaveLength(0)
  })

  it('keeps an in-flight turn — and lands its answer — across a preset pick', async () => {
    // THE DEFECT. Before the store, `api.clear()` unmounted the panel holding
    // the transcript in useState and the turn's answer had nowhere to land.
    let resolveConsult: (v: unknown) => void = () => {}
    stubFetch({
      consult: () => new Promise((res) => {
        resolveConsult = res
      }),
      session: () => sessionDetail([{ role: 'user', content: 'q in flight' }]),
    })

    const api = await mountDock()
    openConsult(api)
    await screen.findByTestId('consult-question')

    fireEvent.change(screen.getByTestId('consult-question'), {
      target: { value: 'q in flight' },
    })
    fireEvent.click(screen.getByTestId('consult-submit'))
    await screen.findByTestId('consult-live-steps')

    // The operator picks a layout preset mid-turn.
    const preset = findPreset('workspace')
    expect(preset).toBeDefined()
    act(() => {
      // The opener mirrors `App.tsx`'s `addSingleton`, positions included —
      // without them every preset panel stacks as tabs in one group and
      // Dockview renders only the active one.
      applyPreset(api, preset!, (kind, position) => {
        api.addPanel({
          id: kind,
          component: kind === 'system.consult' ? 'default' : 'other',
          title: kind,
          ...(position
            ? {
                position: {
                  referencePanel: position.referencePanel,
                  direction: position.direction,
                },
              }
            : {}),
        })
      })
    })

    // The consult panel is back (the preset includes it) and the turn is STILL
    // in flight — not silently discarded.
    await screen.findByTestId('consult-question')
    expect(screen.getByTestId('consult-live-steps')).toBeInTheDocument()
    expect(screen.getByTestId('consult-turn-user')).toHaveTextContent('q in flight')

    // The POST — never aborted — now resolves, and its answer lands in the
    // store and therefore on screen.
    await act(async () => {
      resolveConsult(answer('the answer that used to be thrown away'))
    })
    await waitFor(() =>
      expect(screen.getByTestId('consult-answer')).toHaveTextContent(
        'the answer that used to be thrown away',
      ),
    )
  })

  it('keeps a completed transcript and pinned context across a preset pick', async () => {
    stubFetch({
      consult: async () => answer('a1'),
      session: () =>
        sessionDetail([
          { role: 'user', content: 'q1' },
          { role: 'assistant', content: 'a1' },
        ]),
    })
    const api = await mountDock()
    openConsult(api)
    await screen.findByTestId('consult-question')

    // A pin, assembled by hand into the store the way the Inspector's
    // pin-selection button does.
    act(() => {
      useConsultSessions
        .getState()
        .addPin(CONSULT_PANEL_ID, { kind: 'finding', id: 'f-1', label: 'Finding One' })
    })

    fireEvent.change(screen.getByTestId('consult-question'), { target: { value: 'q1' } })
    fireEvent.click(screen.getByTestId('consult-submit'))
    await waitFor(() => expect(screen.getByTestId('consult-answer')).toHaveTextContent('a1'))

    act(() => {
      api.clear()
    })
    openConsult(api)

    await screen.findByTestId('consult-question')
    expect(screen.getByTestId('consult-answer')).toHaveTextContent('a1')
    expect(screen.getByTestId('consult-turn-user')).toHaveTextContent('q1')
    expect(screen.getByTestId('consult-pin-chip')).toHaveTextContent('Finding One')
  })

  it('closes the EventSource on unmount while the turn keeps running', async () => {
    let resolveConsult: (v: unknown) => void = () => {}
    let answered = false
    stubFetch({
      consult: () => new Promise((res) => {
        resolveConsult = res
      }),
      // The registry persists the assistant turn BEFORE the POST returns, so a
      // session read taken after the answer has landed carries both turns.
      session: () =>
        sessionDetail(
          answered
            ? [
                { role: 'user', content: 'streaming q' },
                {
                  role: 'assistant',
                  content: 'answered after the stream closed',
                  steps: [{ type: 'step', phase: 'plan', kind: 'render_prompt' }],
                },
              ]
            : [{ role: 'user', content: 'streaming q' }],
        ),
    })

    const api = await mountDock()
    openConsult(api)
    await screen.findByTestId('consult-question')

    fireEvent.change(screen.getByTestId('consult-question'), {
      target: { value: 'streaming q' },
    })
    fireEvent.click(screen.getByTestId('consult-submit'))

    expect(FakeEventSource.open).toHaveLength(1)
    const es = FakeEventSource.open[0]
    act(() => {
      es.emit({ type: 'step', phase: 'plan', kind: 'render_prompt' })
    })
    await waitFor(() =>
      expect(screen.getByTestId('consult-live-steps')).toHaveTextContent('1 steps'),
    )

    // Unmount. The stream MUST close — it is the one resource that cannot
    // outlive the component — but the turn must not.
    act(() => {
      api.clear()
    })
    expect(es.closed).toBe(true)
    expect(FakeEventSource.open).toHaveLength(0)

    await act(async () => {
      answered = true
      resolveConsult(answer('answered after the stream closed'))
    })

    openConsult(api)
    await screen.findByTestId('consult-question')
    expect(screen.getByTestId('consult-answer')).toHaveTextContent(
      'answered after the stream closed',
    )
    // The ReAct trace collected before the unmount is on the finished turn —
    // the closed stream cost the live ticker, not the record of the run.
    expect(screen.getByText(/Thinking \(1 steps\)/)).toBeInTheDocument()
  })
})

describe('recovery from a reload', () => {
  /**
   * Leave localStorage exactly as a page killed mid-turn would have, then
   * hydrate the store from it the way a fresh page load does. The stamped
   * `pageLoadId` belongs to that dead page, which is what marks the turn
   * detached: no promise in THIS page will ever answer it.
   */
  function seedInterruptedTurn(pending: PendingTurn, sessionId: string | null) {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        [CONSULT_PANEL_ID]: {
          sessionId,
          transcript: [{ role: 'user', content: pending.question }],
          pins: [],
          pendingTurn: { ...pending, pageLoadId: 'pl-a-previous-page' },
          draft: '',
          scope: '',
          maxRounds: 10,
          updatedAt: Date.now(),
        },
      }),
    )
    useConsultSessions.setState({
      panels: parseConsultPanels(localStorage.getItem(STORAGE_KEY)),
    })
  }

  it('recovers the answer the server recorded while the tab was gone', async () => {
    // The backend guarantee this rests on — that the registry finishes a turn
    // whose client disconnected — is proven in
    // tests/data_pkg/test_consult_disconnect_persistence.py.
    seedInterruptedTurn(pendingTurn({ question: 'q asked before the reload' }), 'sess-1')

    stubFetch({
      session: () =>
        sessionDetail([
          { role: 'user', content: 'q asked before the reload' },
          { role: 'assistant', content: 'the answer that landed anyway' },
        ]),
    })

    const api = await mountDock()
    openConsult(api)
    await screen.findByTestId('consult-question')

    await waitFor(() =>
      expect(screen.getByTestId('consult-answer')).toHaveTextContent(
        'the answer that landed anyway',
      ),
    )
    // And the panel stopped claiming a turn was in flight.
    expect(screen.queryByTestId('consult-live-steps')).not.toBeInTheDocument()
    expect(screen.getByTestId('consult-submit')).toHaveTextContent('Send')
  })

  it('stops waiting on a turn the server never threaded to a session', async () => {
    // The reload beat the FIRST response back: the server minted a session id
    // this browser never learned. Rather than a permanent "Consulting…", the
    // panel says so and hands the composer back.
    seedInterruptedTurn(pendingTurn({ question: 'orphaned first turn' }), null)

    stubFetch({})
    const api = await mountDock()
    openConsult(api)
    await screen.findByTestId('consult-question')

    await waitFor(() =>
      expect(screen.getByTestId('consult-pending-label')).toHaveTextContent(
        /Stopped waiting/,
      ),
    )
    expect(screen.getByTestId('consult-submit')).toHaveTextContent('Send')

    // The operator can clear the stranded turn away.
    fireEvent.click(screen.getByTestId('consult-pending-dismiss'))
    await waitFor(() =>
      expect(screen.queryByTestId('consult-live-steps')).not.toBeInTheDocument(),
    )
  })
})
