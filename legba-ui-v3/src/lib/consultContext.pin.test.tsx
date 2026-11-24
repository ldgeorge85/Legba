/**
 * UI-B6 — the pin hook (`usePinScope`) and the ambient pin it feeds.
 *
 *  - the newest live claim is the pin; a released claim never lingers;
 *  - the wall scope is one more claimant, re-claiming only on a NEW scope;
 *  - a surface that reads no object claims nothing;
 *  - a pin of a kind the registry's `pinned_context` refuses (the roster, an
 *    event) rides the `[Pinned …]` prefix only, never the structured field
 *    (the registry would 422 the whole turn).
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { act, cleanup, render, renderHook } from '@testing-library/react'
import {
  boundPinFor,
  pinForSelection,
  pinnedContext,
  pinnedPrefix,
  registerPinFor,
  resetAmbientPins,
  resolveAmbientPin,
  ROSTER_PIN,
  SCOPE_PIN_ORIGIN,
  turnPinLine,
  turnPinOf,
  useAmbientPin,
  useAmbientPins,
  usePinScope,
  type PinObject,
} from '@/lib/consultContext'
import { resetScope, useScope } from '@/state/scope'
import { scopeFromTarget } from '@/lib/scopeFromReport'

function Surface({ object, name }: { object: PinObject | null; name: string }) {
  usePinScope(object, name)
  return null
}

beforeEach(() => {
  resetScope()
  resetAmbientPins()
})

afterEach(() => {
  cleanup()
  resetAmbientPins()
})

describe('the ambient pin — newest live claim wins, released claims never linger', () => {
  it('a mounted surface pins its object; unmounting releases it', () => {
    const { unmount } = render(<Surface object={{ kind: 'analyst', id: 'u1', label: 'Unit one' }} name="rail" />)
    const pin = resolveAmbientPin(useAmbientPins.getState().claims, null)
    expect(pin).toMatchObject({ surface: 'rail', pin: { kind: 'analyst', id: 'u1', label: 'Unit one', origin: SCOPE_PIN_ORIGIN } })
    unmount()
    expect(resolveAmbientPin(useAmbientPins.getState().claims, null)).toBeNull()
  })

  it('a surface that reads no object pins nothing — and stops pinning when its object goes', () => {
    const { rerender } = render(<Surface object={null} name="timeline" />)
    expect(useAmbientPins.getState().claims).toEqual([])
    rerender(<Surface object={{ kind: 'event', id: 'e1' }} name="timeline" />)
    expect(useAmbientPins.getState().claims.map((c) => c.object.id)).toEqual(['e1'])
    rerender(<Surface object={null} name="timeline" />)
    expect(useAmbientPins.getState().claims).toEqual([])
  })

  it('the newest claim is the pin; releasing it falls back to the one before, never to a stale one', () => {
    render(
      <>
        <Surface object={{ kind: 'target', id: 't1' }} name="tile" />
      </>,
    )
    const second = render(<Surface object={{ kind: 'situation', id: 's1' }} name="register" />)
    expect(resolveAmbientPin(useAmbientPins.getState().claims, null)?.pin.id).toBe('s1')
    second.unmount()
    expect(resolveAmbientPin(useAmbientPins.getState().claims, null)?.pin.id).toBe('t1')
  })

  it('the wall scope claims the pin on a NEW scope only — the Navigator re-affirming it changes nothing', () => {
    render(<Surface object={{ kind: 'analyst', id: 'u1' }} name="rail" />)
    act(() => useScope.getState().setScope(scopeFromTarget('country_g20_br', 'Brazil')))
    let a = resolveAmbientPin(useAmbientPins.getState().claims, useScope.getState().scope)
    // The wall scope's pin keeps its own projection: kind report, the summary.
    expect(a).toMatchObject({ surface: 'scope', pin: { kind: 'report', id: 'country_g20_br' } })
    expect(a?.pin.preview?.body).toContain('scoped to this target')
    // A surface claim after it wins …
    render(<Surface object={{ kind: 'source', id: 'src1' }} name="rail2" />)
    a = resolveAmbientPin(useAmbientPins.getState().claims, useScope.getState().scope)
    expect(a?.pin.id).toBe('src1')
    // … and re-affirming the same scope does not take it back.
    act(() => useScope.getState().setScope(scopeFromTarget('country_g20_br', 'Brazil')))
    expect(resolveAmbientPin(useAmbientPins.getState().claims, useScope.getState().scope)?.pin.id).toBe('src1')
    // Clearing the scope drops its claim; the surface's pin stays.
    act(() => useScope.getState().clear())
    expect(useAmbientPins.getState().claims.map((c) => c.surface)).toEqual(['rail', 'rail2'])
  })

  it('a scope set before any claim still pins (the oldest claimant)', () => {
    const scope = scopeFromTarget('country_g20_br', 'Brazil')
    expect(resolveAmbientPin([], scope)?.pin).toMatchObject({ kind: 'report', id: 'country_g20_br' })
    expect(resolveAmbientPin([], null)).toBeNull()
  })

  it('useAmbientPin is live', () => {
    const { result } = renderHook(() => useAmbientPin())
    expect(result.current).toBeNull()
    act(() => useAmbientPins.getState().claim('k', 'accuracy', ROSTER_PIN))
    expect(result.current).toMatchObject({ surface: 'accuracy', pin: { kind: 'roster', id: 'roster' } })
    expect(result.current?.pin.preview?.body).toBe('The operator is reading the accuracy page across the whole roster.')
  })

  it('revision re-asserts a claim (a tile brought to the front) without a new object', () => {
    function Tile({ rev }: { rev: number }) {
      usePinScope({ kind: 'target', id: 't1' }, 'tile', rev)
      return null
    }
    const tile = render(<Tile rev={0} />)
    render(<Surface object={{ kind: 'target', id: 't2' }} name="tile2" />)
    expect(resolveAmbientPin(useAmbientPins.getState().claims, null)?.pin.id).toBe('t2')
    tile.rerender(<Tile rev={1} />)
    expect(resolveAmbientPin(useAmbientPins.getState().claims, null)?.pin.id).toBe('t1')
  })
})

describe('what each surface pins (pure)', () => {
  it('pinForSelection keeps only the kinds the surface reads', () => {
    expect(pinForSelection({ kind: 'target', id: 't', label: 'T' }, ['target'])).toEqual({ kind: 'target', id: 't', label: 'T' })
    expect(pinForSelection({ kind: 'finding', id: 'f' }, ['target'])).toBeNull()
    expect(pinForSelection(null, ['target'])).toBeNull()
  })

  it('the Register pins a situation on its Situations tab and nothing on Narratives', () => {
    const sel = { kind: 'situation' as const, id: 's1' }
    expect(registerPinFor('situations', sel)).toMatchObject({ kind: 'situation', id: 's1' })
    expect(registerPinFor('narratives', sel)).toBeNull()
  })

  it('a bound tile pins its subject or its unit; a singleton pins nothing', () => {
    expect(boundPinFor({ target_id: 'country_watch_ir' }, 'country_watch_ir')).toMatchObject({ kind: 'target', id: 'country_watch_ir' })
    expect(boundPinFor({ analyst_id: 'energy_analyst' })).toMatchObject({ kind: 'analyst', id: 'energy_analyst' })
    expect(boundPinFor({})).toBeNull()
    expect(boundPinFor(undefined)).toBeNull()
  })
})

describe('the wire — a pin the registry cannot resolve never reaches pinned_context', () => {
  it('the roster and an event ride the [Pinned …] prefix only', () => {
    const roster = resolveAmbientPin([{ key: 'a', surface: 'accuracy', object: ROSTER_PIN, seq: 1 }], null)!.pin
    const event = resolveAmbientPin([{ key: 'b', surface: 'timeline', object: { kind: 'event', id: 'e1' }, seq: 1 }], null)!.pin
    const target = resolveAmbientPin([{ key: 'c', surface: 'rail', object: { kind: 'target', id: 't1' }, seq: 1 }], null)!.pin
    expect(pinnedPrefix([roster, event, target])).toBe(
      '[Pinned roster: "the whole roster" (id=roster)]\n[Pinned event: "e1" (id=e1)]\n[Pinned target: "t1" (id=t1)]',
    )
    expect(pinnedContext([roster, event, target]).map((p) => p.kind)).toEqual(['target'])
  })

  it('the census line names the pin, and says so when there was none', () => {
    const a = resolveAmbientPin([{ key: 'c', surface: 'rail', object: { kind: 'target', id: 't1', label: 'Brazil' }, seq: 1 }], null)
    expect(turnPinLine(turnPinOf(a))).toBe('pinned: target · Brazil (rail)')
    expect(turnPinLine(turnPinOf(null))).toBe('pin: none')
  })
})
