/**
 * C-2b-1 — the write plane's renderers: every line from a ledger row, absence
 * as absence ("cost not measured", "activation pending"), never a zero.
 */
import { describe, expect, it } from 'vitest'

import {
  clockZ,
  expiryLabel,
  pendingCardText,
  receiptText,
  refusalText,
  truthLine,
  type ConsultWriteRow,
} from './consultWrites'

function row(over: Partial<ConsultWriteRow> = {}): ConsultWriteRow {
  return {
    id: '11111111-2222-3333-4444-555555555555',
    session_id: 's',
    request_id: 'r',
    turn_index: 2,
    class: 'source',
    verb: 'retire',
    object_ref: 'source.38north.analysis',
    tier: 'B',
    args: { to_state: 'retired' },
    status: 'pending_confirmation',
    expires_at: '2026-10-04T14:32:00Z',
    confirmed_via: null,
    pre_image: { version: '3f2a9c41aa00bb11', state: 'active' },
    post_image: null,
    reversal: { kind: 'none', why: 'retired is terminal' },
    reverses_write_id: null,
    reversed_by_write_id: null,
    activation: null,
    error: null,
    receipt: null,
    created_at: '2026-10-04T14:17:00Z',
    executed_at: null,
    ...over,
  }
}

describe('the pending card', () => {
  it('says what, on which head, and that a retire cannot be undone', () => {
    expect(pendingCardText(row())).toBe(
      'Retire source source.38north.analysis (head 3f2a…, active) · cannot be undone',
    )
    expect(
      pendingCardText(row({ verb: 'transition', args: { to_state: 'paused' }, reversal: { kind: 'transition' } })),
    ).toBe(
      'Pause source source.38north.analysis (head 3f2a…, active)',
    )
  })

  it('shows the expiry, or that it passed', () => {
    expect(expiryLabel(row(), Date.parse('2026-10-04T14:20:00Z'))).toBe('expires 14:32Z')
    expect(expiryLabel(row(), Date.parse('2026-10-04T14:40:00Z'))).toBe('expired')
    expect(expiryLabel(row({ expires_at: null }))).toBe('expiry not recorded')
  })
})

describe('the receipt', () => {
  it('names the move, the plane, the cost and an UNPROBED activation as pending', () => {
    const text = receiptText(
      row({
        verb: 'update',
        status: 'executed',
        executed_at: '2026-10-04T14:21:09Z',
        receipt: {
          plane: 'core',
          pre: { version: '3f2a9c41', state: 'active' },
          post: { version: '9c41aa00', state: 'active' },
          turn_cost_usd_at_write: 0,
        },
      }),
    )
    expect(text).toBe(
      'Written · source source.38north.analysis updated 3f2a…→9c41… · core · $0.00 · 14:21Z · activation pending',
    )
  })

  it('never invents a cost or a verification', () => {
    const text = receiptText(
      row({
        verb: 'transition',
        status: 'executed',
        executed_at: null,
        receipt: { pre: { version: 'aaaa', state: 'active' }, post: { version: 'bbbb', state: 'paused' } },
        activation: { status: 'parse_failed', rolled_back_to: 'aaaa1111' },
      }),
    )
    expect(text).toContain('moved aaaa…→bbbb… (active→paused)')
    expect(text).toContain('plane not recorded · cost not measured · —')
    expect(text).toContain('activation: parse_failed — rolled back to aaaa…')
  })

  it('reads a unit / composition rerun as the forced run, pending until it is traced (W6)', () => {
    const rerun = (activation: ConsultWriteRow['activation']) =>
      receiptText(
        row({
          class: 'unit',
          verb: 'rerun',
          object_ref: 'unit_x',
          status: 'executed',
          executed_at: '2026-10-04T14:21:09Z',
          reversal: { kind: 'none', why: 'a rerun cannot be undone' },
          receipt: {
            plane: 'core',
            pre: { version: '3f2a9c41', state: 'active' },
            post: { version: '3f2a9c41', state: 'active' },
            turn_cost_usd_at_write: 0,
          },
          activation,
        }),
      )
    expect(rerun(null)).toBe('Written · unit unit_x reran 3f2a…→3f2a… · core · $0.00 · 14:21Z · run pending')
    expect(rerun({ status: 'run_landed', run_started_at: '2026-10-04T14:22:40Z', trace_status: 'success' })).toContain(
      'run landed 14:22Z (success)',
    )
    expect(rerun({ status: 'run_failed', error: 'ConnectError: sidecar down' })).toContain(
      'run failed: ConnectError: sidecar down',
    )
    expect(
      receiptText(
        row({
          verb: 'update',
          status: 'executed',
          receipt: { pre: { version: 'aaaa', state: 'active' }, post: { version: 'bbbb', state: 'active' } },
          activation: { status: 'verified', first_run_at: '2026-10-04T14:30:00Z', at: '2026-10-04T14:31:00Z' },
        }),
      ),
    ).toContain('activation verified 14:30Z')
  })
})

describe('refusals and the truth line', () => {
  it('reads the cause off the ledger row', () => {
    expect(
      refusalText(row({ status: 'refused', error: { cause: 'stale_read', detail: 'head moved since it was read' } })),
    ).toBe('Not written · stale_read: head moved since it was read')
    expect(refusalText(row({ status: 'expired', error: null }))).toBe('Not written · expired')
  })

  it('counts executed / staged / refused, says none when armed and empty, and nothing when unarmed', () => {
    expect(truthLine(null)).toBeNull()
    expect(truthLine([])).toBe('writes: none')
    expect(
      truthLine([
        { write_id: 'a', status: 'executed' },
        { write_id: 'b', status: 'pending_confirmation' },
        { write_id: null, status: 'refused', cause: 'ref_not_read' },
        { write_id: 'c', status: 'failed' },
      ]),
    ).toBe('writes: executed 1 · staged 1 · refused 2')
  })

  it('formats instants as hh:mmZ, absence as a dash', () => {
    expect(clockZ('2026-10-04T09:05:00Z')).toBe('09:05Z')
    expect(clockZ(null)).toBe('—')
    expect(clockZ('nope')).toBe('—')
  })
})
