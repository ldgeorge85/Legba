import '@testing-library/jest-dom/vitest'

// JSDOM doesn't implement WebSocket or fetch by default. The tests that
// touch these provide their own mocks; we just stub here so imports don't
// crash on load.
if (typeof globalThis.WebSocket === 'undefined') {
  class StubWS {
    onopen: (() => void) | null = null
    onmessage: (() => void) | null = null
    onerror: (() => void) | null = null
    onclose: (() => void) | null = null
    close() {}
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ;(globalThis as any).WebSocket = StubWS
}

// JSDOM doesn't implement ResizeObserver, which recharts' ResponsiveContainer
// instantiates on mount (chart panels: Findings sparkline, Timeline). Stub it
// so those panels can render under test; the chart geometry isn't asserted
// (covered by the pure data-layer tests), only the surrounding DOM.
if (typeof globalThis.ResizeObserver === 'undefined') {
  class StubResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ;(globalThis as any).ResizeObserver = StubResizeObserver
}
