/**
 * W11 C-2b-2/entities — an entity write's pending card shows the executor's
 * pre-image (what the record is now, what the change does) and its receipt
 * says "record write", never an activation stamp; the armed turn's block
 * dispatches entity rows here.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import { ConsultWritesBlock } from './ConsultWrites'
import { entityPatchText, entityPendingText, entityReceiptText, isEntityWrite } from './consultWritesEntity'
import type { ConsultWriteRow } from '@/lib/consultWrites'

const KEEPER = '11111111-1111-1111-1111-111111111111'
const LOSER = '22222222-2222-2222-2222-222222222222'

const base: Omit<ConsultWriteRow, 'id' | 'status' | 'verb'> = {
  session_id: 's1',
  request_id: 'r1',
  turn_index: 0,
  class: 'entity',
  object_ref: LOSER,
  tier: 'B',
  args: {},
  expires_at: '2099-01-01T14:32:00Z',
  confirmed_via: null,
  pre_image: null,
  post_image: null,
  reversal: null,
  reverses_write_id: null,
  reversed_by_write_id: null,
  activation: null,
  error: null,
  receipt: null,
  created_at: '2026-10-05T14:00:00Z',
  executed_at: null,
}

const mergePre = {
  version: '1',
  state: 'live',
  sha256: 'ab'.repeat(32),
  loser: { canonical_name: 'Acme Corp', entity_class: 'organization', aliases: ['Acme Corporation'], version: '1' },
  keeper: { canonical_name: 'Acme', entity_class: 'organization', aliases: ['ACME Inc'], version: '4' },
  facts: { open_naming: 12, ids: [], as_of: '2026-10-05T14:00:00Z' },
}

const merge: ConsultWriteRow = {
  ...base,
  id: 'w-merge-000001',
  status: 'pending_confirmation',
  verb: 'merge',
  args: { keeper_id: KEEPER, loser_id: LOSER } as ConsultWriteRow['args'],
  pre_image: mergePre as ConsultWriteRow['pre_image'],
  reversal: { kind: 'unmerge', added_aliases: ['Acme Corp', 'Acme Corporation'] } as ConsultWriteRow['reversal'],
  confirm_code: 'K7QD',
}

describe('entity renderers', () => {
  it('knows an entity row', () => {
    expect(isEntityWrite(merge)).toBe(true)
    expect(isEntityWrite({ class: 'source' })).toBe(false)
  })

  it('the merge card carries the pre-image summary', () => {
    expect(entityPendingText(merge)).toBe(
      'Merge entity Acme Corp (organization) into Acme (organization) · folds 2 aliases · ' +
        '12 open facts name the loser (as of 14:00Z) · unmergeable until compaction',
    )
  })

  it('a count the pre-image did not carry is "not measured", never 0', () => {
    const bare = { ...merge, pre_image: { version: '1' }, reversal: { kind: 'unmerge' } }
    const text = entityPendingText(bare as ConsultWriteRow)
    expect(text).toContain('folds aliases: not recorded')
    expect(text).toContain('open facts naming the loser: not measured')
    expect(text).not.toMatch(/\b0 /)
  })

  it('the edit card names each change and the record as it is now', () => {
    const edit = {
      ...merge,
      verb: 'edit',
      object_ref: KEEPER,
      args: { patch: { canonical_name: 'Acme Holdings', aliases_add: ['Acme Group'], geo_country: 'US' } },
      pre_image: { version: '4', fields: { canonical_name: 'Acme', entity_class: 'organization', aliases: ['ACME Inc'] } },
      reversal: { kind: 'restore_fields' },
    } as unknown as ConsultWriteRow
    expect(entityPendingText(edit)).toBe(
      'Edit entity Acme (organization) v4 · name → “Acme Holdings” · aliases +“Acme Group” · geo_country → US · now: 1 alias',
    )
    expect(entityPatchText({ aliases_remove: ['X'], geo_lat: null })).toBe('aliases −“X” · geo_lat → none')
  })

  it('the unmerge card says it records the pair distinct', () => {
    const un = { ...merge, verb: 'unmerge', reversal: { kind: 'remerge', removed_aliases: ['Acme Corp'] } }
    expect(entityPendingText(un as ConsultWriteRow)).toBe(
      'Unmerge entity Acme Corp (organization) from Acme (organization) · drops 1 folded alias · records the two as distinct',
    )
  })

  it('the receipt is a record write: versions, plane, cost, time — no activation stamp', () => {
    const done = {
      ...merge,
      status: 'executed',
      post_image: { version: '2', state: 'merged' },
      receipt: { plane: 'core', turn_cost_usd_at_write: 0 },
      executed_at: '2026-10-05T14:21:00Z',
    } as ConsultWriteRow
    const line = entityReceiptText(done)
    expect(line).toBe(
      'Written · entity Acme Corp (organization) merged into Acme (organization) v1→v2 · core · $0.00 · 14:21Z · record write (no activation)',
    )
    expect(line).not.toContain('activation pending')
    const unmeasured = entityReceiptText({ ...done, receipt: { plane: null } } as ConsultWriteRow)
    expect(unmeasured).toContain('plane not recorded · cost not measured')
  })
})

describe('the armed turn dispatches entity rows to the entity renderer', () => {
  let rows: ConsultWriteRow[]
  beforeEach(() => {
    rows = [merge]
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(() =>
        Promise.resolve(
          new Response(JSON.stringify({ writes: rows, count: rows.length }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        ),
      ),
    )
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the entity pending card with Confirm and the typed code', async () => {
    render(
      <ConsultWritesBlock
        sessionId="s1"
        requestId="r1"
        entries={[{ write_id: merge.id, status: 'pending_confirmation' }]}
      />,
    )
    await waitFor(() => expect(screen.getByTestId('consult-write-pending')).toBeTruthy())
    const card = screen.getByTestId('consult-write-pending')
    expect(card.textContent).toContain('Merge entity Acme Corp (organization) into Acme (organization)')
    expect(card.textContent).toContain('12 open facts name the loser')
    expect(screen.getByTestId('consult-write-code').textContent).toBe('or type confirm K7QD')
  })
})
