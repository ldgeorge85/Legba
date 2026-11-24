import '@testing-library/jest-dom/vitest'
import { beforeEach } from 'vitest'
import { setSubjectIndex, subjectIndexFrom } from '@/lib/subjectIndex'
import { dropCachedGets } from '@/lib/queryCache'
import subjectRoster from '@/test/subjectRoster.fixture.json'

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

// JSDOM implements neither `Element.scrollIntoView` nor the pointer-capture
// trio, all of which the Radix primitives in `src/ui/` call (Select scrolls
// the highlighted option into view and checks pointer capture on its trigger).
// No-op stubs: the tests assert DOM state and the keyboard/click contract,
// never scroll position or capture.
if (typeof Element !== 'undefined') {
  const proto = Element.prototype as unknown as Record<string, unknown>
  if (typeof proto.scrollIntoView !== 'function') proto.scrollIntoView = function scrollIntoView() {}
  if (typeof proto.hasPointerCapture !== 'function') proto.hasPointerCapture = () => false
  if (typeof proto.setPointerCapture !== 'function') proto.setPointerCapture = function setPointerCapture() {}
  if (typeof proto.releasePointerCapture !== 'function')
    proto.releasePointerCapture = function releasePointerCapture() {}
}

// THE SUBJECT ROSTER (UI-B2). A subject's display name and roster leg are read
// off its descriptor (`lib/subjectIndex.ts`), which the shell fetches once
// (`GET /registry/descriptors?family=target`). A unit test renders a surface
// with that listing already read: the fixture is the repository's own target
// descriptors (`test/subjectRoster.fixture.json`), seeded before every test. A
// test about the unread / other-profile path replaces it with
// `setSubjectIndex`. The leaf imports nothing, so a test that mocks
// `profile_ui.json` still loads the profile AFTER its mock.
const SUBJECT_ROSTER = subjectIndexFrom(subjectRoster.rows)
setSubjectIndex(SUBJECT_ROSTER)
beforeEach(() => setSubjectIndex(SUBJECT_ROSTER))

// X4 READPATH — the api layer's GET cache (`lib/queryCache`) is module state:
// one test's mocked `/findings` answer must never be another test's.
beforeEach(() => dropCachedGets())
