/**
 * CitedRecords — a cited record opens the walk its true kind names (roll 84
 * live walk: a situation cited as `signal` asked `/lineage/signal/<id>` → 404).
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { CitedRecords, UNRESOLVED } from '@/components/CitedRecords'
import { sheetRecordFor, useRecordSheet } from '@/lib/recordSheet'
import { useSelection } from '@/state/selection'

beforeEach(() => {
  useSelection.getState().clear()
  useRecordSheet.setState({ ref: null, cite: null, open: false })
})

function open() {
  fireEvent.click(screen.getByTestId('consult-cited-toggle'))
}

describe('CitedRecords', () => {
  it('a cited situation selects the situation, so the sheet walks /lineage/situation', () => {
    render(<CitedRecords refs={[{ kind: 'situation', id: 's-1', description: 'Hormuz mine warfare' }]} />)
    open()
    fireEvent.click(screen.getByTestId('record-link'))
    const sel = useSelection.getState().selection
    expect(sel).toMatchObject({ kind: 'situation', id: 's-1' })
    expect(sheetRecordFor(sel, useRecordSheet.getState())?.ref.kind).toBe('situation')
  })

  it('a cited hypothesis keeps its row kind for the sheet (selected as a finding)', () => {
    render(<CitedRecords refs={[{ kind: 'hypothesis', id: 'h-1', description: 'A thesis' }]} />)
    open()
    fireEvent.click(screen.getByTestId('record-link'))
    const sel = useSelection.getState().selection
    expect(sel).toMatchObject({ kind: 'finding', id: 'h-1', instanceKey: 'hypothesis' })
    expect(sheetRecordFor(sel, useRecordSheet.getState())?.ref.kind).toBe('hypothesis')
  })

  it('an id the store does not hold is shown without a link', () => {
    render(
      <CitedRecords
        refs={[
          { kind: UNRESOLVED, id: 'gone-1', description: 'A row since removed' },
          { kind: UNRESOLVED, id: 'gone-2' },
        ]}
      />,
    )
    open()
    expect(screen.queryByTestId('record-link')).toBeNull()
    expect(screen.getAllByTestId('consult-cited-unresolved')).toHaveLength(2)
    expect(screen.getByText('A row since removed')).toBeTruthy()
  })
})
