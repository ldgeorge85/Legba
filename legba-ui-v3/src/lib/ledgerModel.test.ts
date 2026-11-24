import { describe, expect, it } from 'vitest'
import {
  ARM_COLUMNS,
  armCell,
  dueIn,
  ledgerSummary,
  ledgerTableRows,
  type LedgerForecast,
  type LedgerForecastsResponse,
} from './ledgerModel'

const NOW = new Date('2026-11-10T12:00:00Z')

function fc(over: Partial<LedgerForecast>): LedgerForecast {
  return {
    id: over.slug ?? 'x',
    slug: 'FX1',
    question: 'A fictional question',
    resolver_kind: 'official_result',
    due_at: '2026-12-15T23:59:59+00:00',
    status: 'open',
    numbers: [],
    ...over,
  }
}

function res(forecasts: LedgerForecast[]): LedgerForecastsResponse {
  return { version: '2026-11/st0', available: true, forecasts }
}

describe('ledgerTableRows — the Ledger by what resolves next', () => {
  it('sorts open first by due date, then closed rows', () => {
    const rows = ledgerTableRows(
      res([
        fc({ slug: 'late', due_at: '2027-01-03T23:59:59+00:00' }),
        fc({ slug: 'closed', due_at: '2026-11-01T00:00:00+00:00', status: 'void' }),
        fc({ slug: 'soon', due_at: '2026-11-29T23:59:59+00:00' }),
      ]),
      NOW,
    )
    expect(rows.map((r) => r.slug)).toEqual(['soon', 'late', 'closed'])
    expect(rows[0].dueIn).toBe('in 19 d')
    expect(rows[2].dueIn).toBe('—')
  })

  it('one cell per arm: a number, a stated absence with its reason, or not stated', () => {
    const [row] = ledgerTableRows(
      res([
        fc({
          sealed_at: '2026-11-11T00:00:00+00:00',
          numbers: [
            { issuer_kind: 'operator', issuer_ref: 'operator', p: 0.25, sealed: true },
            {
              issuer_kind: 'tower',
              issuer_ref: 'tower',
              p: null,
              absent_reason: 'no scheduled analyst emitted a probability',
              sealed: true,
            },
            { issuer_kind: 'frontier_arm', issuer_ref: 'llm.anthropic.opus_4_7', p: 0.4, rationale: 'r', sealed: true },
          ],
        }),
      ]),
      NOW,
    )
    expect(row.arms.map((a) => a.kind)).toEqual(ARM_COLUMNS.map((c) => c.kind))
    const by = Object.fromEntries(row.arms.map((a) => [a.kind, a]))
    expect(by.operator.text).toBe('0.25')
    expect(by.tower).toMatchObject({ text: 'absent', state: 'absent' })
    expect(by.tower.title).toContain('no scheduled analyst')
    expect(by.consult).toMatchObject({ text: '—', state: 'not_stated' })
    expect(by.frontier_arm.title).toContain('llm.anthropic.opus_4_7')
    expect(row.state).toBe('sealed')
    expect(row.stateLabel).toBe('sealed')
  })

  it('states: open-not-sealed, overdue, resolved with its outcome; Brier is a placeholder, never a number', () => {
    const rows = ledgerTableRows(
      res([
        fc({ slug: 'a', due_at: '2026-11-20T00:00:00+00:00' }),
        fc({ slug: 'b', due_at: '2026-11-01T00:00:00+00:00', overdue: true, sealed_at: '2026-10-01T00:00:00Z' }),
        fc({
          slug: 'c',
          status: 'resolved',
          resolution: { id: 'r', outcome: 'yes', resolved_at: '2026-12-16T09:00:00Z', early: false },
        }),
        fc({ slug: 'd', status: 'void', resolution: { id: 'v', outcome: 'void' } }),
      ]),
      NOW,
    )
    const by = Object.fromEntries(rows.map((r) => [r.slug, r]))
    expect(by.a.stateLabel).toBe('open · not sealed')
    expect(by.b.state).toBe('overdue')
    expect(by.b.dueIn).toBe('10 d past due')
    expect(by.c.resolution).toBe('YES · 2026-12-16')
    expect(by.c.brier).toBe('not scored yet')
    expect(by.d.brier).toBe('—')
    for (const r of rows) expect(r.brier).not.toMatch(/\d/)
  })

  it('several numbers of one kind show the first and a count', () => {
    const cell = armCell('operator', [
      { issuer_kind: 'operator', issuer_ref: 'a', p: 0.2, sealed: false },
      { issuer_kind: 'operator', issuer_ref: 'b', p: 0.3, sealed: false },
    ])
    expect(cell.text).toBe('0.20 (+1)')
    expect(cell.title).toContain('not sealed')
  })

  it('an absent or unreadable response maps to no rows, and the summary says so', () => {
    expect(ledgerTableRows(null, NOW)).toEqual([])
    expect(ledgerTableRows({ version: 'v', available: false, forecasts: [] }, NOW)).toEqual([])
    expect(ledgerSummary([])).toBe('no forecasts on the ledger')
  })

  it('the summary names what resolves next', () => {
    const rows = ledgerTableRows(res([fc({ slug: 'F4', due_at: '2026-11-29T23:59:59+00:00' })]), NOW)
    expect(ledgerSummary(rows)).toBe('1 forecasts · 1 open (0 sealed) · next: F4 in 19 d')
  })

  it('dueIn', () => {
    expect(dueIn('2026-11-10T20:00:00Z', NOW)).toBe('due today')
    expect(dueIn('garbage', NOW)).toBe('—')
  })
})
