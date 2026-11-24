/**
 * W11 C-2b-2/facts — a fact write renders as its triple and what it closes,
 * from the ledger row alone: the staged card names each row it would close,
 * the receipt names the asserted value and the closed count, and absence reads
 * as absence (never a 0).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import { ConsultWritesBlock } from './ConsultWrites'
import { factCloses, factPendingText, factReceiptText, isFactWrite } from './ConsultWritesFact'
import type { ConsultWriteRow } from '@/lib/consultWrites'

const A = '1a2b3c4d-0000-4000-8000-000000000001'
const B = '5e6f7a8b-0000-4000-8000-000000000002'

const base = {
  session_id: 's1',
  request_id: 'r1',
  turn_index: 0,
  class: 'fact',
  object_ref: 'iran | leader of',
  expires_at: null,
  confirmed_via: null,
  post_image: null,
  reverses_write_id: null,
  reversed_by_write_id: null,
  activation: null,
  error: null,
  created_at: '2026-10-04T14:00:00Z',
  executed_at: null,
} as const

const staged = {
  ...base,
  id: 'w-fact-pending-01',
  status: 'pending_confirmation',
  verb: 'assert',
  tier: 'B',
  args: { subject: 'Iran', predicate: 'leader of', value: 'Jane Roe', confidence: 1, supersede: 'differing' },
  pre_image: {
    version: 'aaaa0000aaaa0000', state: '3 open', subject: 'Iran', predicate: 'leader of',
    open: [{ id: A, value: 'Old A', source_type: 'ingestion' }, { id: B, value: 'Old B', source_type: 'agent' }],
    closes: [A, B],
  },
  reversal: { kind: 'close_and_reopen' },
  receipt: null,
  expires_at: '2099-01-01T14:32:00Z',
  confirm_code: 'K7QD',
} as unknown as ConsultWriteRow

const executed = {
  ...base,
  id: 'w-fact-done-0002',
  status: 'executed',
  verb: 'assert',
  tier: 'A',
  args: { subject: 'Iran', predicate: 'leader of', value: 'Jane Roe', confidence: 1, supersede: 'none' },
  pre_image: { version: 'bbbb', state: '1 open', closes: [] },
  post_image: { version: 'cccc', state: '2 open' },
  reversal: { kind: 'close_and_reopen' },
  receipt: { plane: 'core', turn_cost_usd_at_write: 0, pre: { version: 'bbbb', state: '1 open' },
             post: { version: 'cccc', state: '2 open' } },
  executed_at: '2026-10-04T14:21:00Z',
} as unknown as ConsultWriteRow

const closed = {
  ...base,
  id: 'w-fact-close-003',
  status: 'executed',
  verb: 'close',
  tier: 'A',
  args: { fact_id: A, reason: 'wrong' },
  pre_image: { version: 'dddd', state: '2 open', fact: { id: A, value: 'Old A', source_type: 'ingestion' }, closes: [A] },
  post_image: { version: 'eeee', state: '1 open' },
  reversal: { kind: 'reopen' },
  receipt: null,
  executed_at: '2026-10-04T14:22:00Z',
} as unknown as ConsultWriteRow

describe('the fact renderers', () => {
  it('name the triple and every row a staged assert would close', () => {
    expect(isFactWrite(staged)).toBe(true)
    expect(factCloses(staged).map((b) => b.id)).toEqual([A, B])
    expect(factPendingText(staged)).toBe(
      'Assert fact Iran · leader of = "Jane Roe" (operator, confidence 1.00) · would close 2: '
      + '1a2b3c4d "Old A" (ingestion); 5e6f7a8b "Old B" (agent)',
    )
  })

  it('read the receipt as the asserted value and the closed count, never a digest', () => {
    const line = factReceiptText(executed)
    expect(line).toBe(
      'Written · fact iran | leader of asserted "Jane Roe" (operator) · closed 0 · 1 open → 2 open · '
      + 'core · $0.00 · 14:21Z · read on the next query',
    )
    expect(line).not.toContain('bbbb')
  })

  it('render absence as absence', () => {
    const bare = { ...closed, receipt: null, pre_image: { closes: [B] } } as unknown as ConsultWriteRow
    const line = factReceiptText(bare)
    expect(line).toContain('closed 5e6f7a8b value not recorded (author not recorded)')
    expect(line).toContain('plane not recorded · cost not measured')
    const noConf = { ...staged, args: { subject: 'Iran', predicate: 'leader of', value: 'X' } } as unknown as ConsultWriteRow
    expect(factPendingText(noConf)).toContain('confidence not recorded')
  })
})

describe('the armed block renders fact rows with the fact renderer', () => {
  let rows: ConsultWriteRow[]
  beforeEach(() => {
    rows = [staged, executed, closed]
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() =>
      Promise.resolve(new Response(JSON.stringify({ writes: rows, count: rows.length }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))))
  })
  afterEach(() => vi.unstubAllGlobals())

  it('shows the staged card with its close list and the receipts with Revert', async () => {
    const entries = rows.map((r) => ({ write_id: r.id, status: r.status }))
    render(<ConsultWritesBlock sessionId="s1" requestId="r1" entries={entries} />)
    await waitFor(() => expect(screen.getByTestId('consult-write-pending')).toBeInTheDocument())
    expect(screen.getByTestId('consult-write-pending').textContent).toContain('would close 2')
    const list = screen.getByTestId('consult-write-fact-closes')
    expect(list.querySelectorAll('li')).toHaveLength(2)
    const receipts = screen.getAllByTestId('consult-write-receipt')
    expect(receipts.map((r) => r.textContent ?? '')).toEqual([
      expect.stringContaining('asserted "Jane Roe" (operator)'),
      expect.stringContaining('closed 1a2b3c4d "Old A" (ingestion)'),
    ])
    expect(screen.getAllByTestId('consult-write-revert')).toHaveLength(2)
  })
})
