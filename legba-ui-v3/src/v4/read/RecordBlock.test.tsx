/**
 * W4 F-3 — a block the subject-group floor KEPT is chipped beside its age:
 * "kept by the region floor · order rank 17", and — past the floor's stale bar —
 * "… · 8.2 d old — not fresh". A block the order carried has no chip.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import RecordBlock from './RecordBlock'
import { flooredWorldRow } from './__fixtures__/floor'
import { projectAssembly } from '@/lib/assemblyModel'

afterEach(() => cleanup())

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

describe('RecordBlock — kept by the floor', () => {
  it('chips the kept block with its order rank, beside its age', () => {
    const a = projectAssembly(flooredWorldRow())!
    const kept = a.blocks.find((b) => b.carried_by_floor)!
    render(wrap(<RecordBlock block={kept} lead={false} refused={false} floorGrain="region" />))
    const chip = screen.getByTestId('read-block-floor')
    expect(chip.textContent).toBe('kept by the region floor · order rank 17')
    const attrib = screen.getByTestId('read-attrib').textContent ?? ''
    expect(attrib).toContain(' old')
  })

  it('says "not fresh" past the stale bar, with the age', () => {
    const a = projectAssembly(flooredWorldRow({ stale: true }))!
    const kept = a.blocks.find((b) => b.carried_by_floor)!
    render(wrap(<RecordBlock block={kept} lead={false} refused={false} floorGrain="region" />))
    expect(screen.getByTestId('read-block-floor').textContent).toBe(
      'kept by the region floor · order rank 17 · 8.2 d old — not fresh',
    )
  })

  it('leaves a block the order carried unchipped', () => {
    const a = projectAssembly(flooredWorldRow())!
    render(wrap(<RecordBlock block={a.blocks[0]} lead={false} refused={false} floorGrain="region" />))
    expect(screen.queryByTestId('read-block-floor')).toBeNull()
  })
})
