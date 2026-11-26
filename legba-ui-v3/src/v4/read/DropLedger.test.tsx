/**
 * W4 F-3 — the drop ledger on a world record carrying a subject-group floor:
 * the floor line leads the summary sentence, the "Ranked below the line" note
 * stops claiming a pure prefix when the floor promoted something, and a row the
 * floor displaced says so with its order rank. A record with no floor renders
 * exactly as before.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import DropLedger, { CoverageLine } from './DropLedger'
import worldRow from './__fixtures__/assembly.world.json'
import { flooredWorldRow } from './__fixtures__/floor'
import { projectAssembly, type ReadFindingRow } from '@/lib/assemblyModel'

afterEach(() => cleanup())

describe('DropLedger — the floor', () => {
  it('opens the summary with the floor line, read off assembly.floor', () => {
    render(<DropLedger assembly={projectAssembly(flooredWorldRow())!} />)
    expect(screen.getByTestId('read-floor-line').textContent).toBe(
      '6 regions carried, floor honoured — 1 kept by the floor.',
    )
    const summary = screen.getByTestId('read-drop-summary').textContent ?? ''
    expect(summary.startsWith('6 regions carried, floor honoured')).toBe(true)
  })

  it('corrects the below-the-line note and marks the displaced row', () => {
    render(<DropLedger assembly={projectAssembly(flooredWorldRow())!} />)
    fireEvent.click(screen.getByTestId('read-drop-toggle'))
    const note = screen.getByTestId('read-floor-note').textContent ?? ''
    expect(note).toContain('Selection is the region floor plus a strict prefix of the order')
    expect(note).toContain('one block per region is guaranteed')
    expect(note).toContain('1 below was displaced by the floor.')
    expect(note).not.toContain('cannot happen by construction')
    const displaced = screen.getAllByTestId('read-drop-displaced')
    expect(displaced).toHaveLength(1)
    expect(displaced[0].textContent).toBe(' · displaced by the region floor')
    const row = displaced[0].closest('[data-testid="read-drop-row"]')!
    expect(row.textContent).toContain('#8 by order')
  })

  it('renders a record with no floor exactly as before', () => {
    render(<DropLedger assembly={projectAssembly(worldRow as unknown as ReadFindingRow)!} />)
    expect(screen.queryByTestId('read-floor-line')).toBeNull()
    fireEvent.click(screen.getByTestId('read-drop-toggle'))
    expect(screen.queryByTestId('read-floor-note')).toBeNull()
    expect(screen.queryByTestId('read-drop-displaced')).toBeNull()
    expect(screen.getByTestId('read-drop-detail').textContent).toContain(
      'cannot happen by construction',
    )
  })
})

describe('CoverageLine — W11 stale-read marks (the composition panel)', () => {
  const base = projectAssembly(worldRow as unknown as ReadFindingRow)!

  it('names each quoted read the composition marked stale, with its age and cadence', () => {
    const assembly = {
      ...base,
      coverage: [
        { unit: 'proliferation_watch', unit_name: 'Proliferation watch', status: 'in_basis', read_date: null, age_h: 94.29, cadence_h: 11, stale: true },
        { unit: 'escalation', status: 'in_basis', read_date: null, age_h: 6.36, cadence_h: 11, stale: false },
        { unit: 'military_posture', status: 'in_basis', read_date: null, age_h: 80, stale: null },
      ],
    }
    render(<CoverageLine assembly={assembly} />)
    const note = screen.getByTestId('read-coverage-stale').textContent ?? ''
    expect(note).toContain('Quoted stale (1)')
    expect(note).toContain('Proliferation watch read 94 h old (cadence 11 h)')
    // Fresh and unmeasured reads are not named.
    expect(note).not.toContain('escalation')
    expect(note).not.toContain('military_posture')
  })

  it('Names each unit the register lists carried, unsupported, with its last evidence', () => {
    const assembly = {
      ...base,
      coverage: [
        { unit: 'energy_security', unit_name: 'Energy security', status: 'carried_unsupported', read_date: null, age_h: 4, carried_status: 'high', last_evidence_age_h: 313.4 },
        { unit: 'escalation', status: 'carried_unsupported', read_date: null, age_h: 5, carried_status: 'moderate', last_evidence_age_h: null },
        { unit: 'military_posture', status: 'in_basis', read_date: null, age_h: 6 },
      ],
    }
    render(<CoverageLine assembly={assembly} />)
    const note = screen.getByTestId('read-coverage-carried').textContent ?? ''
    expect(note).toContain('Carried, unsupported (2)')
    expect(note).toContain('Energy security last re-evidenced 313 h ago')
    // No evidencing read in the lookback is said in words, never as 0 h.
    expect(note).toContain('escalation not re-evidenced in the lookback')
    expect(note).not.toContain('military_posture')
  })

  it('prints no carried note when no unit is carried, unsupported', () => {
    const assembly = {
      ...base,
      coverage: [{ unit: 'escalation', status: 'in_basis', read_date: null, age_h: 6.36 }],
    }
    render(<CoverageLine assembly={assembly} />)
    expect(screen.queryByTestId('read-coverage-carried')).toBeNull()
  })

  it('prints no stale note when nothing was marked', () => {
    const assembly = {
      ...base,
      coverage: [{ unit: 'escalation', status: 'in_basis', read_date: null, age_h: 6.36, stale: null }],
    }
    render(<CoverageLine assembly={assembly} />)
    expect(screen.queryByTestId('read-coverage-stale')).toBeNull()
  })
})
