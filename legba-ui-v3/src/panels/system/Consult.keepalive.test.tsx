/**
 * W2 S-batch — the Consult keepalive indicator.
 *
 * The relay sends a NAMED `keepalive` event through a quiet stretch (an SSE
 * comment never reaches script). The panel shows "synthesising · Ns" on the
 * last round while keepalives vouch for the silence, "working · Ns" when the
 * drilling has not visibly ended, and nothing at all for a silence no
 * keepalive has vouched for.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

import ConsultPanel from './Consult'
import { useConsultSessions } from '@/state/consultSession'
import { resetScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import {
  KEEPALIVE_STALE_MS,
  drillingEnded,
  parseKeepalive,
  quietRound,
} from '@/lib/consultLiveness'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'c-keepalive',
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

/** An EventSource stub that ALSO dispatches named events, as a browser does. */
class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  closed = false
  listeners: Record<string, Array<(e: Event) => void>> = {}
  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }
  addEventListener(type: string, fn: (e: Event) => void) {
    ;(this.listeners[type] ??= []).push(fn)
  }
  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent)
  }
  keepalive(idle: number) {
    const e = { data: JSON.stringify({ type: 'keepalive', idle_s: idle }) } as MessageEvent
    for (const fn of this.listeners.keepalive ?? []) fn(e as unknown as Event)
  }
  close() {
    this.closed = true
  }
}

beforeEach(() => {
  localStorage.clear()
  useConsultSessions.setState({ panels: {} })
  resetScope()
  useSelection.getState().clear()
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  vi.stubGlobal('crypto', {
    ...globalThis.crypto,
    randomUUID: () => '00000000-0000-0000-0000-00000000beef',
  })
  // The POST never resolves: the turn stays in flight for the whole test.
  vi.stubGlobal('fetch', vi.fn().mockImplementation(() => new Promise(() => {})))
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('consultLiveness — pure', () => {
  const live = { requestId: 'r', lastFrameAt: 1_000, keepaliveAt: 26_000 }

  it('reads only the relay’s keepalive payload', () => {
    expect(parseKeepalive('{"type":"keepalive","idle_s":25.0}')).toEqual({ idle_s: 25 })
    expect(parseKeepalive('{"type":"step"}')).toBeNull()
    expect(parseKeepalive('not json')).toBeNull()
  })

  it('claims nothing for a silence no keepalive vouched for, or a stale one', () => {
    expect(quietRound({ ...live, keepaliveAt: null }, [], null, 30_000)).toBeNull()
    expect(quietRound(live, [], null, 26_000 + KEEPALIVE_STALE_MS + 1)).toBeNull()
    expect(quietRound(null, [], null, 30_000)).toBeNull()
  })

  it('counts the silence since the last frame, and says synthesising only once drilling ended', () => {
    const tool = { type: 'step', phase: 'act', kind: 'tool_call', round: 8 }
    expect(quietRound(live, [tool], 30, 43_000)).toEqual({
      text: 'working · 42s',
      synthesising: false,
      seconds: 42,
    })
    expect(quietRound(live, [tool], 8, 43_000)?.text).toBe('synthesising · 42s')
    expect(
      quietRound(live, [tool, { type: 'step', kind: 'wall_budget_reached' }], 30, 43_000)?.text,
    ).toBe('synthesising · 42s')
    expect(drillingEnded([], null)).toBe(false)
  })
})

describe('ConsultPanel — the keepalive indicator', () => {
  it('shows synthesising · Ns on the last round while keepalives arrive, and drops it on the next frame', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    fireEvent.change(screen.getByTestId('consult-question'), { target: { value: 'go' } })
    fireEvent.click(screen.getByTestId('consult-submit'))
    const es = FakeEventSource.instances[0]
    expect(es).toBeTruthy()

    act(() => {
      es.emit({ type: 'step', phase: 'plan', kind: 'render_prompt', max_rounds: 8 })
      es.emit({ type: 'step', phase: 'act', kind: 'tool_call', tool: 'search_signals', round: 8 })
    })
    // Silence with no keepalive yet: nothing is claimed.
    expect(screen.queryByTestId('consult-quiet-round')).toBeNull()

    act(() => {
      vi.advanceTimersByTime(25_000)
      es.keepalive(25)
    })
    await waitFor(() =>
      expect(screen.getByTestId('consult-quiet-round').textContent).toMatch(/^synthesising · 2[5-6]s$/),
    )
    expect(screen.getByTestId('consult-quiet-round').getAttribute('data-synthesising')).toBe('true')

    // It ticks between keepalives.
    act(() => {
      vi.advanceTimersByTime(10_000)
    })
    await waitFor(() =>
      expect(screen.getByTestId('consult-quiet-round').textContent).toMatch(/^synthesising · 3[5-6]s$/),
    )

    // The next frame ends the quiet stretch.
    act(() => {
      es.emit({ type: 'step', phase: 'reflect', kind: 'llm_call' })
    })
    await waitFor(() => expect(screen.queryByTestId('consult-quiet-round')).toBeNull())
  })

  it('a client without addEventListener still gets its step frames', async () => {
    class BareEventSource {
      static last: BareEventSource | null = null
      url: string
      onmessage: ((e: MessageEvent) => void) | null = null
      onerror: ((e: Event) => void) | null = null
      constructor(url: string) {
        this.url = url
        BareEventSource.last = this
      }
      close() {}
    }
    vi.stubGlobal('EventSource', BareEventSource as unknown as typeof EventSource)
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    fireEvent.change(screen.getByTestId('consult-question'), { target: { value: 'go' } })
    fireEvent.click(screen.getByTestId('consult-submit'))
    act(() => {
      BareEventSource.last!.onmessage?.({
        data: JSON.stringify({ type: 'step', phase: 'plan', kind: 'render_prompt' }),
      } as MessageEvent)
    })
    await waitFor(() =>
      expect(screen.getByTestId('consult-live-steps')).toHaveTextContent('Thinking… (1 steps)'),
    )
  })
})
