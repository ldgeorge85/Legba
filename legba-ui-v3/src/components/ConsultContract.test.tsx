/**
 * Q-2 — the contract block shows what the server now says about a turn:
 * how the asker's window words were READ (a numeric date, bare ordinals,
 * early/mid/late), an event-anchored window that was asked for and NOT
 * resolved, the share of a long cited row the number check did not read, and
 * where a RECOVERED turn's terms came from. Absent stays absent.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ConsultContractBlock } from './ConsultContract'
import { parseTurnContract } from '@/lib/consultContract'

const WINDOW = {
  start: '2026-11-07T04:00:00+00:00', end: '2026-11-14T04:00:00+00:00', hours: 168,
  source: 'default', basis: '168h default', deictic: false,
}

function draw(raw: Record<string, unknown>) {
  const c = parseTurnContract({ schema: 'consult_contract/1', armed: true, ...raw })
  if (!c) throw new Error('no contract')
  render(<ConsultContractBlock contract={c} />)
}

describe('ConsultContract', () => {
  it('prints the reading the server took for ambiguous window words', () => {
    draw({
      window: {
        ...WINDOW, source: 'user_words', basis: '3/11 to 10/11',
        reading: 'read as day/month: 3 Nov 2026 to 10 Nov 2026 (month/day was also valid)',
      },
    })
    expect(screen.getByTestId('consult-contract-window-reading').textContent).toContain('read as day/month')
    expect(screen.queryByTestId('consult-contract-window-anchor')).toBeNull()
  })

  it('says plainly that an event-anchored window was not resolved', () => {
    draw({
      window: {
        ...WINDOW,
        anchor: { words: 'since the ceasefire', phrase: 'ceasefire', relation: 'start', resolved: false,
                  reason: 'the platform does not date a named event by guessing' },
      },
    })
    const line = screen.getByTestId('consult-contract-window-anchor').textContent ?? ''
    expect(line).toContain('since the ceasefire')
    expect(line).toContain('not resolved')
    expect(line).toContain('does not date a named event')
  })

  it('draws no anchor line for an anchor the asker dated', () => {
    draw({
      window: {
        ...WINDOW, source: 'user_words',
        anchor: { words: 'since the strike on 3 Nov', resolved: true, date: '2026-11-03' },
      },
    })
    expect(screen.queryByTestId('consult-contract-window-anchor')).toBeNull()
  })

  it('shows how much of a long cited row the number check did not read', () => {
    draw({
      window: WINDOW,
      grounding: {
        mode: 'record_only', applied: true, counts: { kept_with_rows: 2 }, numbers_unsupported: 1,
        number_check: { rows: 3, rows_clipped: 1, chars_unchecked: 12500, row_chars_cap: 200000 },
      },
    })
    expect(screen.getByTestId('consult-contract-number-unchecked').textContent).toContain('12,500 characters unchecked')
  })

  it('draws nothing about the number check when every row was read whole', () => {
    draw({
      window: WINDOW,
      grounding: { mode: 'default', applied: false, counts: {}, number_check: { rows: 2, rows_clipped: 0, chars_unchecked: 0 } },
    })
    expect(screen.queryByTestId('consult-contract-number-unchecked')).toBeNull()
  })

  it('names a recovered turn and where its window and coverage came from', () => {
    draw({
      window: WINDOW,
      recovery: { window_from: 'original_turn', coverage_from: 'original_turn', original_cost_stop: '> stopped' },
    })
    const row = screen.getByTestId('consult-contract-recovery').textContent ?? ''
    expect(row).toContain('window of the original turn')
    expect(row).toContain('stopped drilling for cost')
  })
})
