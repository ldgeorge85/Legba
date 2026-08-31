/**
 * THE HEADLINE METRIC, ON THE REAL BINDING PATH (D2e).
 *
 * `brief_read` is the number the oracle wager is decided on: "on how many of
 * the last 90 days did the operator open the morning read at all?" Everything
 * else this train ships is supporting evidence. So this file does not mount a
 * stand-in — it mounts the REAL `<App/>`, exactly as `main.tsx` does, boots it
 * into the real Morning Read workspace through the real `seedWorkspace` walk,
 * and asserts the events that actually reach the queue.
 *
 * The failure this guards against is the quiet one: an instrument that was
 * wired up wrong reports zero, and zero is indistinguishable from the finding
 * the wager expects. A green test here is what lets a zero at day 90 be read
 * as evidence rather than as a bug.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, waitFor, cleanup, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { App } from '@/App'
import { PreferencesProvider } from '@/components/density/PreferencesProvider'
import { DEDUPE_MS, __pendingReadEvents, __resetReadTelemetry } from '@/lib/readTelemetry'
import { LANDING_WORKSPACE, findWorkspace } from '@/lib/workspaces'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <PreferencesProvider>{ui}</PreferencesProvider>
    </QueryClientProvider>
  )
}

function emitted() {
  return __pendingReadEvents()
}

function kinds() {
  return emitted().map((e) => e.event_kind)
}

beforeEach(() => {
  __resetReadTelemetry()
  sessionStorage.clear()
  localStorage.clear()
  window.location.hash = ''
  // The shell fetches the descriptor registry (an ARRAY) and several paged
  // reads (an ENVELOPE) on boot. Serving the right empty shape to each keeps
  // `synthesizeBoundRegistrations` from throwing an unhandled rejection that
  // would make this file's output unreadable — nothing here asserts panel
  // data, only the telemetry the boot emits.
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string) => {
      const body = String(url).includes('/registry/')
        ? []
        : { data: [], items: [], next_cursor: null }
      return {
        ok: true,
        status: 200,
        json: async () => body,
        text: async () => JSON.stringify(body),
      } as unknown as Response
    }),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('boot — the Morning Read landing', () => {
  it('emits brief_read when the landing workspace mounts', async () => {
    render(wrap(<App />))
    await waitFor(() => expect(kinds()).toContain('brief_read'))
    const brief = emitted().filter((e) => e.event_kind === 'brief_read')
    expect(brief).toHaveLength(1)
    expect(brief[0].workspace).toBe(LANDING_WORKSPACE)
    // No subject: the morning read is the product, not a record.
    expect(brief[0].subject_id).toBeNull()
  })

  it('emits workspace_open FIRST, so the stance is set before the seed walk', async () => {
    render(wrap(<App />))
    await waitFor(() => expect(kinds().length).toBeGreaterThan(2))
    expect(kinds()[0]).toBe('workspace_open')
    expect(kinds()[1]).toBe('brief_read')
  })

  it('tags every seeded panel with the stance it belongs to', async () => {
    render(wrap(<App />))
    const seeded = findWorkspace(LANDING_WORKSPACE)!.seed.map((p) => p.kind)
    await waitFor(() =>
      expect(kinds().filter((k) => k === 'panel_open')).toHaveLength(seeded.length),
    )
    const opens = emitted().filter((e) => e.event_kind === 'panel_open')
    expect(opens.map((e) => e.subject_id)).toEqual(seeded)
    for (const e of opens) expect(e.workspace).toBe(LANDING_WORKSPACE)
  })

  it('shares one session nonce across the whole boot', async () => {
    render(wrap(<App />))
    await waitFor(() => expect(kinds()).toContain('brief_read'))
    const nonces = new Set(emitted().map((e) => e.session_nonce))
    expect(nonces.size).toBe(1)
  })
})

describe('switching stance', () => {
  it('emits workspace_open for the destination and no second brief_read', async () => {
    render(wrap(<App />))
    await waitFor(() => expect(kinds()).toContain('brief_read'))

    // Alt+2 — the Desk stance (train A's keybinding, not a synthetic call).
    fireEvent.keyDown(window, { key: '2', altKey: true })
    await waitFor(() =>
      expect(
        emitted().filter((e) => e.event_kind === 'workspace_open'),
      ).toHaveLength(2),
    )

    const switches = emitted().filter((e) => e.event_kind === 'workspace_open')
    expect(switches[1].workspace).toBe('desk')
    // The morning read was opened once this session, not twice.
    expect(kinds().filter((k) => k === 'brief_read')).toHaveLength(1)
    // And the Desk stance's panels are tagged to Desk, not to Morning Read.
    const deskSeed = findWorkspace('desk')!.seed.map((p) => p.kind)
    const deskOpens = emitted().filter(
      (e) => e.event_kind === 'panel_open' && e.workspace === 'desk',
    )
    expect(deskOpens.map((e) => e.subject_id)).toEqual(deskSeed)
  })

  it('emits brief_read again when the operator RETURNS to the morning read', async () => {
    render(wrap(<App />))
    await waitFor(() => expect(kinds()).toContain('brief_read'))
    fireEvent.keyDown(window, { key: '2', altKey: true })
    await waitFor(() =>
      expect(emitted().filter((e) => e.event_kind === 'workspace_open')).toHaveLength(2),
    )
    // Wait out the mount-hazard dedupe window on purpose. Leaving Morning Read
    // and bouncing straight back inside a second is a remount, not a second
    // morning, and the emitter is RIGHT to collapse it — so a test that
    // asserted an immediate re-emit would be pinning a bug.
    await new Promise((r) => setTimeout(r, DEDUPE_MS + 50))
    fireEvent.keyDown(window, { key: '1', altKey: true })
    await waitFor(() =>
      expect(kinds().filter((k) => k === 'brief_read')).toHaveLength(2),
    )
  }, 10_000)
})
