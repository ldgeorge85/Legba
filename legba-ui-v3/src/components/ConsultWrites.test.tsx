/**
 * C-2b-1 — the armed turn's block renders FROM THE LEDGER: the amber pending
 * card (Confirm / Cancel / the typed-code hint / expiry), the receipt with
 * Revert, the struck-through reversed receipt, and the refusals — and the
 * operator's acts go straight to the executor's routes.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { ConsultWritesBlock } from './ConsultWrites'
import type { ConsultWriteRow } from '@/lib/consultWrites'

const base: Omit<ConsultWriteRow, 'id' | 'status' | 'verb'> = {
  session_id: 's1',
  request_id: 'r1',
  turn_index: 0,
  class: 'source',
  object_ref: 'source.feed',
  tier: 'A',
  args: {},
  expires_at: null,
  confirmed_via: null,
  pre_image: { version: 'aaaa0000', state: 'active' },
  post_image: { version: 'bbbb0000', state: 'active' },
  reversal: { kind: 'rollback' },
  reverses_write_id: null,
  reversed_by_write_id: null,
  activation: null,
  error: null,
  receipt: { plane: 'core', turn_cost_usd_at_write: 0 },
  created_at: '2026-10-04T14:00:00Z',
  executed_at: '2026-10-04T14:21:00Z',
}

let rows: ConsultWriteRow[]
let fetchMock: ReturnType<typeof vi.fn>

function json(body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }))
}

beforeEach(() => {
  rows = [
    { ...base, id: 'w-pending-0001', status: 'pending_confirmation', verb: 'transition', tier: 'B',
      args: { to_state: 'paused' }, expires_at: '2099-01-01T14:32:00Z', confirm_code: 'K7QD',
      post_image: null, receipt: null, executed_at: null, reversal: { kind: 'transition' } },
    { ...base, id: 'w-receipt-0002', status: 'executed', verb: 'update' },
    { ...base, id: 'w-reversed-003', status: 'executed', verb: 'update', reversed_by_write_id: 'w-revert-00004' },
    { ...base, id: 'w-revert-00004', status: 'executed', verb: 'revert', request_id: null, turn_index: 3,
      reverses_write_id: 'w-reversed-003', executed_at: '2026-10-04T14:40:00Z' },
    { ...base, id: 'w-refused-0005', status: 'refused', verb: 'update',
      error: { cause: 'stale_read', detail: 'head moved since it was read' }, post_image: null, receipt: null },
  ]
  fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    if (String(url).includes('/consult/writes?')) return json({ writes: rows, count: rows.length })
    if (init?.method === 'POST') return json({ status: 'executed', write_id: 'x', line: 'Written · …' })
    return json({})
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => vi.unstubAllGlobals())

describe('an unarmed turn', () => {
  it('renders nothing and reads nothing', () => {
    const { container } = render(<ConsultWritesBlock sessionId="s1" requestId="r1" entries={null} />)
    expect(container).toBeEmptyDOMElement()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('an armed turn', () => {
  const entries = [
    { write_id: 'w-pending-0001', status: 'pending_confirmation' },
    { write_id: 'w-receipt-0002', status: 'executed' },
    { write_id: null, status: 'refused', cause: 'ref_not_read', summary: 'source x was not inspected first' },
  ]

  it('renders every line from the ledger, the gate refusal from the bag', async () => {
    render(<ConsultWritesBlock sessionId="s1" requestId="r1" entries={entries} />)
    await waitFor(() => expect(screen.getByTestId('consult-write-pending')).toBeInTheDocument())
    expect(String(fetchMock.mock.calls[0][0])).toContain('/consult/writes?session_id=s1')
    expect(screen.getByTestId('consult-writes-armed')).toHaveTextContent('changes allowed')
    const card = screen.getByTestId('consult-write-pending')
    expect(card).toHaveTextContent('Pause source source.feed (head aaaa…, active)')
    expect(screen.getByTestId('consult-write-code')).toHaveTextContent('or type confirm K7QD')
    expect(card).toHaveTextContent('expires 14:32Z')
    expect(screen.getByTestId('consult-write-receipt')).toHaveTextContent(
      'Written · source source.feed updated aaaa…→bbbb… · core · $0.00 · 14:21Z · activation pending',
    )
    const reversed = screen.getByTestId('consult-write-reversed')
    expect(reversed.querySelector('.line-through')).not.toBeNull()
    expect(reversed).toHaveTextContent('Reverted 14:40Z by w-w-revert (your turn 4)')
    const refused = screen.getAllByTestId('consult-write-refused').map((e) => e.textContent)
    expect(refused).toContain('Not written · stale_read: head moved since it was read')
    expect(refused).toContain('Not written · ref_not_read: source x was not inspected first')
  })

  it('Confirm, Cancel and Revert call the executor routes, then re-read the ledger', async () => {
    render(<ConsultWritesBlock sessionId="s1" requestId="r1" entries={entries} />)
    await waitFor(() => expect(screen.getByTestId('consult-write-confirm')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('consult-write-confirm'))
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([u, i]) => String(u).endsWith('/consult/writes/w-pending-0001/confirm') && i?.method === 'POST')).toBe(true),
    )
    fireEvent.click(screen.getByTestId('consult-write-revert'))
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith('/consult/writes/w-receipt-0002/revert'))).toBe(true),
    )
    fireEvent.click(screen.getByTestId('consult-write-cancel'))
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith('/consult/writes/w-pending-0001/cancel'))).toBe(true),
    )
    const reads = fetchMock.mock.calls.filter(([u]) => String(u).includes('/consult/writes?')).length
    expect(reads).toBeGreaterThanOrEqual(4)
  })

  it('an armed turn that wrote nothing shows the stamp and no cards', () => {
    render(<ConsultWritesBlock sessionId="s1" requestId="r1" entries={[]} />)
    expect(screen.getByTestId('consult-writes-armed')).toBeInTheDocument()
    expect(screen.queryByTestId('consult-write-pending')).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
