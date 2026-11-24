/**
 * U-10 — a read's quoted sentence opens the Record sheet on ITS citation: the
 * head finding the block quotes, with the sentence's first marker as `cite`.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import RecordBlock, { leadSpan } from './RecordBlock'
import { flooredWorldRow } from './__fixtures__/floor'
import { projectAssembly } from '@/lib/assemblyModel'
import { firstCitedOrdinal } from '@/lib/recordChain'
import { useRecordSheet } from '@/lib/recordSheet'
import { useSelection } from '@/state/selection'

afterEach(() => cleanup())

describe('RecordBlock — the record of its sentence', () => {
  it('opens the sheet on the head finding and the lead sentence citation', () => {
    const a = projectAssembly(flooredWorldRow())!
    // An invented quoted sentence carrying the head's own marker [2].
    const base = a.blocks[0]
    const block = { ...base, spans: base.spans.map((sp) => ({ ...sp, text: 'Cranes stood idle at the north quay [2].' })) }
    expect(firstCitedOrdinal(leadSpan(block)?.text)).toBe(2)
    const qc = new QueryClient()
    render(
      <QueryClientProvider client={qc}>
        <RecordBlock block={block} lead={false} refused={false} />
      </QueryClientProvider>,
    )
    fireEvent.click(screen.getByTestId('read-record-sentence'))
    expect(useSelection.getState().selection).toMatchObject({ kind: 'finding', id: block.finding_id, origin: 'read' })
    expect(useRecordSheet.getState().ref).toMatchObject({ kind: 'finding', id: block.finding_id })
    expect(useRecordSheet.getState().cite).toBe(2)
  })

  it('reads the first marker of a sentence in either dialect', () => {
    expect(firstCitedOrdinal('Cranes idle [4][5].')).toBe(4)
    expect(firstCitedOrdinal('Cranes idle [[ref:2]].')).toBe(2)
    expect(firstCitedOrdinal('No marker here.')).toBeNull()
  })
})
