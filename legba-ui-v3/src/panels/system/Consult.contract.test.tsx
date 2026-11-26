/**
 * U-8 / Q-1 — the console side of the consult TURN CONTRACT.
 *
 *  - SEND: every turn carries the window the user chose (default the server's
 *    declared 168 h) and the grounding mode, in the server's own field names
 *    (`ConsultRequest.window` / `.grounding_mode`).
 *  - RENDER: the response's `contract` is drawn under the answer — window,
 *    coverage with its declared gaps, explanations, grounding, and a cost stop
 *    marked as a PARTIAL answer; a part the server did not send draws nothing.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import ConsultPanel from './Consult'
import { useConsultSessions } from '@/state/consultSession'
import type { PanelRegistration } from '@/types'
import { resetScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'

function reg(): PanelRegistration {
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
  }
}

class FakeEventSource {
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  close() {}
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  useConsultSessions.setState({ panels: {} })
  resetScope()
  useSelection.getState().clear()
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  vi.stubGlobal('crypto', { ...globalThis.crypto, randomUUID: () => '00000000-0000-0000-0000-000000000abc' })
})

function answer(extra: Record<string, unknown> = {}) {
  return vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ answer: 'the answer', finding_id: null, derived_from: [], tool_calls: [], cited_refs: [], ...extra }),
  })
}

async function ask(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal('fetch', fetchMock)
  fireEvent.change(screen.getByTestId('consult-question'), { target: { value: 'what moved?' } })
  fireEvent.click(screen.getByTestId('consult-submit'))
  await waitFor(() => expect(screen.getByTestId('consult-answer')).toBeInTheDocument())
  return JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)
}

const CONTRACT = {
  schema: 'consult_contract/1',
  armed: true,
  window: {
    start: '2026-11-07T08:00:00+00:00', end: '2026-11-14T08:00:00+00:00', hours: 168,
    source: 'console', basis: 'console 168h', deictic: false,
  },
  reach: { cited_dated: 4, in_window: 3, before_window: 1, after_window: 0, narrowed: null, wider_reach: 'undeclared', wider_reason: 'cites a row from 2026-11-01' },
  coverage: {
    schema: 'consult_coverage/1', state: 'measured',
    days: [
      { day: '2026-11-12', signals: 900, sources: 80, findings: 40, events: 12 },
      { day: '2026-11-13', signals: 0, sources: 0, findings: 0, events: 0 },
    ],
    zero_days: ['2026-11-13'], sparse_days: [], sparse_rule: 'signals below 25% of the median day',
    gaps: { paused_sources: [{ id: 'src.a', name: 'Source A' }], error_sources: [] },
  },
  hypotheses: {
    conforming: true, repair: 'not_needed',
    items: [{ explanation: 'A drought cut output', p: 0.6, for: ['finding:f1'], against: [], test: { observable: 'output data', by: '2026-12-01' } }],
    something_else_p: 0.4,
  },
  grounding_mode: 'record_only',
  grounding: { mode: 'record_only', applied: true, counts: { kept_with_rows: 5, cut_no_row: 2, labelled_inference: 1 }, numbers_unsupported: 1, verifier: { state: 'judged', items: [{}] } },
  cost_stop: '> **Stopped for cost:** drilling stopped after round 4 because the turn\'s cost ceiling ($10.00) leaves no room. Spent when drilling stopped: $7.10.',
}

describe('Consult sends the turn contract inputs', () => {
  it("sends the time band's window — its absolute ends — and shows it read-only", async () => {
    const end = Date.parse('2026-11-14T08:00:00Z')
    useWorldState.getState().setWindow(end - 24 * 3_600_000, end)
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    expect(screen.getByTestId('consult-point-window').textContent).toMatch(/window 24h/)
    const body = await ask(answer())
    expect(body.window).toEqual({ start: '2026-11-13T08:00:00.000Z', end: '2026-11-14T08:00:00.000Z', label: 'console time band' })
    expect(body.grounding_mode).toBe('default')
  })

  it('follows the band when it changes; a window an older console stored is ignored; there is no window selector', async () => {
    localStorage.setItem('legba_consult_window', '720h')
    const end = Date.parse('2026-11-14T08:00:00Z')
    useWorldState.getState().setWindow(end - 24 * 3_600_000, end)
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    expect(screen.queryByTestId('consult-window')).toBeNull()
    act(() => useWorldState.getState().setWindow(end - 7 * 86_400_000, end))
    expect(screen.getByTestId('consult-point-window').textContent).toMatch(/window 7d/)
    fireEvent.click(screen.getByTestId('consult-grounding'))
    expect(screen.getByTestId('consult-grounding')).toHaveAttribute('aria-checked', 'true')
    const body = await ask(answer())
    expect(Object.keys(body.window).sort()).toEqual(['end', 'label', 'start'])
    expect(Date.parse(body.window.end) - Date.parse(body.window.start)).toBe(7 * 86_400_000)
    expect(body.grounding_mode).toBe('record_only')
    expect(localStorage.getItem('legba_consult_grounding')).toBe('record_only')
  })
})

describe('Consult renders the turn contract', () => {
  it('draws window, coverage with gaps, explanations, grounding and the partial cost stop', async () => {
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    await ask(answer({ contract: CONTRACT, usage: { cost_usd: 9.4, max_cost_usd: 20 } }))
    const block = await screen.findByTestId('consult-contract')
    expect(within(block).getByTestId('consult-contract-partial').textContent).toMatch(/Partial answer/)
    expect(within(block).getByTestId('consult-contract-window').textContent).toMatch(/168 h · chosen in the console/)
    expect(within(block).getByTestId('consult-contract-window').textContent).toMatch(/3 of 4 dated citations inside the window/)
    expect(within(block).getByTestId('consult-contract-window').textContent).toMatch(/reached wider \(undeclared\)/)
    const cov = within(block).getByTestId('consult-contract-coverage')
    expect(cov.textContent).toMatch(/1 of 2 days hold rows/)
    expect(within(block).getByTestId('consult-contract-gaps').textContent).toMatch(/no rows: 2026-11-13/)
    expect(within(block).getByTestId('consult-contract-gaps').textContent).toMatch(/1 source paused/)
    expect(within(block).getByTestId('consult-contract-hypothesis').textContent).toMatch(/A drought cut output · P 60 %/)
    expect(within(block).getByTestId('consult-contract-hypotheses').textContent).toMatch(/something else: P 40 %/)
    const g = within(block).getByTestId('consult-contract-grounding')
    expect(g.textContent).toMatch(/record only/)
    expect(g.textContent).toMatch(/5 kept · 2 cut · 1 labelled/)
    const cost = within(block).getByTestId('consult-contract-cost')
    expect(cost.textContent).toMatch(/stopped on the cost ceiling — partial answer/)
    expect(cost.textContent).toMatch(/\$9\.40/)
    expect(cost.textContent).toMatch(/Spent when drilling stopped: \$7\.10/)
  })

  it('draws no box for a part the server did not send', async () => {
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    await ask(answer({ contract: { schema: 'consult_contract/1', armed: true, window: CONTRACT.window, coverage: { state: 'unavailable', reason: 'no_read_pool' } } }))
    const block = await screen.findByTestId('consult-contract')
    expect(within(block).getByTestId('consult-contract-coverage').textContent).toMatch(/not measured for this turn — no_read_pool/)
    expect(within(block).queryByTestId('consult-contract-hypotheses')).toBeNull()
    expect(within(block).queryByTestId('consult-contract-grounding')).toBeNull()
    expect(within(block).queryByTestId('consult-contract-partial')).toBeNull()
  })

  it('an answer with no contract (an older registry, an unarmed turn) draws no contract at all', async () => {
    render(<ConsultPanel registration={reg()} scope={{}} mode="personal" />)
    await ask(answer({ contract: { schema: 'consult_contract/1', armed: false, on_screen: { v: 1 } } }))
    expect(screen.queryByTestId('consult-contract')).toBeNull()
  })
})
