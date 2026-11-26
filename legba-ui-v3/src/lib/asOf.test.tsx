/**
 * "As recorded": the instant in the address, the strip every region carries
 * while it is set, and the address following the store.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { AS_OF_VIEWS, honoursAsOf, parseAt, searchWithAt, withAt } from './asOf'
import { useShareState } from './shareState'
import { RegionAsOf } from '@/components/AsOfStrip'
import { useWorldState } from '@/v4/world/worldState'

const AT = Date.UTC(2026, 10, 20, 8, 10)
const ISO = new Date(AT).toISOString()

beforeEach(() => {
  useWorldState.setState({ asOfMs: null })
  window.history.replaceState(null, '', '/')
})
afterEach(() => {
  useWorldState.setState({ asOfMs: null })
  window.history.replaceState(null, '', '/')
})

describe('the address', () => {
  it('reads a past instant and refuses a malformed or future one', () => {
    expect(parseAt(`?at=${ISO}`, AT + 1000)).toBe(AT)
    expect(parseAt('?at=yesterday', AT)).toBeNull()
    expect(parseAt(`?at=${ISO}`, AT - 1000)).toBeNull()
    expect(parseAt('', AT)).toBeNull()
  })
  it('writes and clears the instant beside other query keys', () => {
    expect(searchWithAt('?case=a:1', AT)).toBe(`?case=a%3A1&at=${encodeURIComponent(ISO)}`)
    expect(searchWithAt(`?at=${ISO}`, null)).toBe('')
  })
  it('adds the instant to a route only when one is set', () => {
    expect(withAt('/v3/ledger/forecasts', null)).toBe('/v3/ledger/forecasts')
    expect(withAt('/findings?limit=5', AT)).toBe(`/findings?limit=5&at=${encodeURIComponent(ISO)}`)
  })
  it('restores from ?at= on load and follows the store after', () => {
    window.history.replaceState(null, '', `/?at=${encodeURIComponent(ISO)}#ws=board`)
    const { unmount } = renderHook(() => useShareState())
    expect(useWorldState.getState().asOfMs).toBe(AT)
    act(() => useWorldState.getState().setAsOf(AT - 3_600_000))
    expect(window.location.search).toBe(`?at=${encodeURIComponent(new Date(AT - 3_600_000).toISOString())}`)
    expect(window.location.hash).toBe('#ws=board')
    act(() => useWorldState.getState().setAsOf(null))
    expect(window.location.search).toBe('')
    unmount()
  })
})

describe('the strip on every region', () => {
  it('is absent at now: the region renders as it would unwrapped', () => {
    render(
      <RegionAsOf viewId="v4.map">
        <div data-testid="body" />
      </RegionAsOf>,
    )
    expect(screen.queryByTestId('as-of-strip')).toBeNull()
    expect(screen.getByTestId('body').parentElement?.hasAttribute('data-as-of-region')).toBe(false)
  })

  it('a region whose reads cannot answer the instant says it is not time-shifted', () => {
    useWorldState.setState({ asOfMs: AT })
    expect(honoursAsOf('v4.map')).toBe(false)
    render(
      <RegionAsOf viewId="v4.map">
        <div data-testid="body" />
      </RegionAsOf>,
    )
    const strip = screen.getByTestId('as-of-strip')
    expect(strip).toHaveAttribute('data-honoured', '0')
    expect(strip).toHaveTextContent('Not time-shifted: this region shows the record now, not as of 20 Nov 08:10 UTC')
    fireEvent.click(screen.getByTestId('as-of-return'))
    expect(useWorldState.getState().asOfMs).toBeNull()
    expect(screen.queryByTestId('as-of-strip')).toBeNull()
  })

  it('a region listed as honouring it says what it shows', () => {
    const views = AS_OF_VIEWS as Set<string>
    views.add('view:test')
    try {
      expect(honoursAsOf('view:test')).toBe(true)
      useWorldState.setState({ asOfMs: AT })
      render(
        <RegionAsOf viewId="view:test">
          <div />
        </RegionAsOf>,
      )
      expect(screen.getByTestId('as-of-strip')).toHaveTextContent('Showing the record as of 20 Nov 08:10 UTC')
    } finally {
      views.delete('view:test')
    }
  })
})
