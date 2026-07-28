/**
 * Regression tests for `useElementWidth` — the callback-ref width hook.
 *
 * The bug this pins down (system.timeline blanking on first mount): the
 * measured element mounts AFTER a loading empty-state has already rendered.
 * A ref + []-deps observer effect never observes the late element, so the
 * width sticks at 0. The callback-ref hook must observe the element WHENEVER
 * it attaches — including after a loading→data swap — and must disconnect on
 * detach.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { useElementWidth } from './useElementWidth'

// --- controllable ResizeObserver fake --------------------------------------

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  observed: Element[] = []
  disconnected = false
  constructor(private cb: ResizeObserverCallback) {
    FakeResizeObserver.instances.push(this)
  }
  observe(el: Element) {
    this.observed.push(el)
  }
  unobserve() {}
  disconnect() {
    this.disconnected = true
  }
  /** Simulate the browser reporting a size for the observed element. */
  fire(width: number) {
    this.cb(
      [{ contentRect: { width } } as unknown as ResizeObserverEntry],
      this as unknown as ResizeObserver,
    )
  }
}

const realRO = globalThis.ResizeObserver

beforeEach(() => {
  FakeResizeObserver.instances = []
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ;(globalThis as any).ResizeObserver = FakeResizeObserver
})

afterEach(() => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ;(globalThis as any).ResizeObserver = realRO
  vi.restoreAllMocks()
})

// --- harness: loading empty-state first, measured element later ------------

function Harness({ loading }: { loading: boolean }) {
  const [ref, width] = useElementWidth<HTMLDivElement>()
  return loading ? (
    <div data-testid="empty">loading…</div>
  ) : (
    <div ref={ref} data-testid="plot">
      <span data-testid="width">{width}</span>
    </div>
  )
}

describe('useElementWidth', () => {
  it('observes an element that mounts AFTER a loading first render (the first-mount blank regression)', () => {
    const { rerender } = render(<Harness loading />)
    // Nothing to observe while the empty-state occupies the slot.
    expect(FakeResizeObserver.instances.flatMap((i) => i.observed)).toHaveLength(0)

    // Data lands → the measured element mounts → it MUST be observed.
    rerender(<Harness loading={false} />)
    const observing = FakeResizeObserver.instances.filter((i) => !i.disconnected)
    expect(observing).toHaveLength(1)
    expect(observing[0].observed).toHaveLength(1)

    // The observer reporting a size propagates into the hook's width.
    act(() => observing[0].fire(640))
    expect(screen.getByTestId('width').textContent).toBe('640')
  })

  it('disconnects when the element detaches and re-observes on re-attach', () => {
    const { rerender } = render(<Harness loading={false} />)
    const first = FakeResizeObserver.instances.filter((i) => !i.disconnected)
    expect(first).toHaveLength(1)

    // Back to the empty-state → the observer must be torn down.
    rerender(<Harness loading />)
    expect(first[0].disconnected).toBe(true)

    // And a fresh mount is observed again.
    rerender(<Harness loading={false} />)
    const alive = FakeResizeObserver.instances.filter((i) => !i.disconnected)
    expect(alive).toHaveLength(1)
    act(() => alive[0].fire(320))
    expect(screen.getByTestId('width').textContent).toBe('320')
  })

  it('disconnects on unmount', () => {
    const { unmount } = render(<Harness loading={false} />)
    const alive = FakeResizeObserver.instances.filter((i) => !i.disconnected)
    expect(alive).toHaveLength(1)
    unmount()
    expect(alive[0].disconnected).toBe(true)
  })
})
