/**
 * U-4 — Consult's chip on the map: what a turn pulled up, with one Undo; and
 * a Consult `map` step moving the globe's rows through the world layers.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useCasePlayer } from '@/state/casePlayer'
import { useWorldState } from '@/v4/world/worldState'
import { ConsultChip } from './chrome'
import { alignWorldOnce, resetGlobeLayers, useGlobeLayers } from './layerState'

afterEach(() => {
  cleanup()
  useCasePlayer.setState({ player: null })
  resetGlobeLayers()
})

function player(status: string) {
  const step = (action: string, label: string) => ({ seq: 1, action: { action, args: {}, seq: 1 }, state: 'executed', reason: null, label, restsOn: '', text: null })
  return {
    requestId: 'r', mode: 'live', status, steps: [step('map', 'map · events'), step('timeline', 'window 30 days'), step('narrate', 'said something')],
    executedThrough: 2, expectSeq: 3, gapAtSeq: null, sealed: false, summary: null, record: null, ceiling: 9, cursor: 0, note: null,
  }
}

describe("Consult's chip on the map", () => {
  it('lists the view steps a turn ran and undoes them as one', () => {
    const restore = vi.fn()
    useCasePlayer.setState({ player: player('done') as never, restore })
    render(<ConsultChip />)
    const chip = screen.getByTestId('globe-consult-chip')
    expect(chip.textContent).toContain('Pulled up by Consult: map · events · window 30 days')
    expect(chip.textContent).not.toContain('said something')
    fireEvent.click(screen.getByTestId('globe-consult-undo'))
    expect(restore).toHaveBeenCalledTimes(1)
  })

  it('shows nothing when no case ran', () => {
    render(<ConsultChip />)
    expect(screen.queryByTestId('globe-consult-chip')).toBeNull()
  })
})

describe('the globe rows follow the world layers Consult drives', () => {
  it('aligns once, then a map step (wholesale layers) and its undo move the rows', () => {
    alignWorldOnce()
    const before = { ...useWorldState.getState().layers }
    expect(useGlobeLayers.getState().on.events).toBe(false)
    act(() => useWorldState.getState().setLayers(['events']))
    expect(useGlobeLayers.getState().on.events).toBe(true)
    expect(useGlobeLayers.getState().on.change).toBe(false)
    act(() => useWorldState.setState({ layers: before }))
    expect(useGlobeLayers.getState().on.events).toBe(false)
    expect(useGlobeLayers.getState().on.change).toBe(true)
  })
})

describe("the map's actions (the frame's panel contract)", () => {
  it('a layer, the window, the point and a camera request each land in the store the map follows', async () => {
    const { globeActions, useGlobeCamera } = await import('./actions')
    const { useSelection } = await import('@/state/selection')
    globeActions.setLayer('events', true)
    expect(useGlobeLayers.getState().on.events).toBe(true)
    expect(useWorldState.getState().layers.events).toBe(true)
    globeActions.setWindow(1000, 5000)
    expect(useWorldState.getState().windowStartMs).toBe(1000)
    globeActions.select({ kind: 'target', id: 'subject_x' })
    expect(useSelection.getState().selection?.id).toBe('subject_x')
    const n = useGlobeCamera.getState().nonce
    globeActions.flyTo({ kind: 'place', lon: 43, lat: 12 })
    expect(useGlobeCamera.getState()).toMatchObject({ nonce: n + 1, request: { kind: 'place', lon: 43 } })
  })
})
