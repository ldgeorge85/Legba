/**
 * Unit tests for the Flow store's edge-density defaults (P0-2f).
 *
 * At live scale the subscription fan-out (~1,180 edges) made the default
 * canvas an illegible moiré. `densityHiddenEdgeKinds` computes which wiring
 * kinds default to hidden for a given edge census, and the store applies it
 * without ever clobbering an operator's explicit toggle choices.
 */

import { describe, it, expect, beforeEach } from 'vitest'
import {
  DENSE_EDGE_COUNT_THRESHOLD,
  densityHiddenEdgeKinds,
  useFlowState,
} from './flowState'

describe('densityHiddenEdgeKinds', () => {
  it('keeps the static baseline (analyst_target) at low densities', () => {
    expect(densityHiddenEdgeKinds({ subscription: 10, analyst_target: 5, grant: 3 })).toEqual([
      'analyst_target',
    ])
    expect(densityHiddenEdgeKinds({})).toEqual(['analyst_target'])
  })

  it('hides any kind past the threshold (the live 1,180-subscription case)', () => {
    expect(
      densityHiddenEdgeKinds({ subscription: 1180, analyst_target: 418, grant: 120 }),
    ).toEqual(['subscription', 'analyst_target'])
  })

  it('threshold is exclusive: exactly-at-threshold stays visible', () => {
    expect(
      densityHiddenEdgeKinds({ subscription: DENSE_EDGE_COUNT_THRESHOLD }),
    ).toEqual(['analyst_target'])
    expect(
      densityHiddenEdgeKinds({ subscription: DENSE_EDGE_COUNT_THRESHOLD + 1 }),
    ).toEqual(['subscription', 'analyst_target'])
  })

  it('can hide every kind on a fully-dense census', () => {
    expect(
      densityHiddenEdgeKinds({ subscription: 999, analyst_target: 999, grant: 999 }),
    ).toEqual(['subscription', 'analyst_target', 'grant'])
  })
})

describe('useFlowState density application', () => {
  beforeEach(() => {
    useFlowState.setState({
      hiddenEdgeKinds: ['analyst_target'],
      densityDefaults: ['analyst_target'],
      edgeKindsTouched: false,
    })
  })

  it('applies density defaults while the operator has not touched the toggles', () => {
    useFlowState
      .getState()
      .applyEdgeDensityDefaults({ subscription: 1180, analyst_target: 418, grant: 120 })
    const s = useFlowState.getState()
    expect(s.hiddenEdgeKinds).toEqual(['subscription', 'analyst_target'])
    expect(s.densityDefaults).toEqual(['subscription', 'analyst_target'])
  })

  it('never clobbers an explicit operator toggle', () => {
    // Operator unhides analyst_target (their call stands)…
    useFlowState.getState().toggleEdgeKind('analyst_target')
    expect(useFlowState.getState().hiddenEdgeKinds).toEqual([])
    // …then a dense census arrives: defaults update, the choice does not.
    useFlowState.getState().applyEdgeDensityDefaults({ subscription: 1180 })
    const s = useFlowState.getState()
    expect(s.hiddenEdgeKinds).toEqual([])
    expect(s.densityDefaults).toEqual(['subscription', 'analyst_target'])
  })

  it('reset restores the density-computed defaults and re-arms auto-apply', () => {
    useFlowState.getState().applyEdgeDensityDefaults({ subscription: 1180 })
    useFlowState.getState().toggleEdgeKind('subscription') // show it anyway
    expect(useFlowState.getState().hiddenEdgeKinds).toEqual(['analyst_target'])
    useFlowState.getState().resetEdgeKinds()
    const s = useFlowState.getState()
    expect(s.hiddenEdgeKinds).toEqual(['subscription', 'analyst_target'])
    expect(s.edgeKindsTouched).toBe(false)
  })

  it('is idempotent: an unchanged census keeps the same array references', () => {
    useFlowState.getState().applyEdgeDensityDefaults({ subscription: 1180 })
    const before = useFlowState.getState().hiddenEdgeKinds
    useFlowState.getState().applyEdgeDensityDefaults({ subscription: 1180 })
    expect(useFlowState.getState().hiddenEdgeKinds).toBe(before)
  })
})
