/**
 * RA-1 — the reader state of the console: what a signed-in reader is shown
 * instead of a request the registry would refuse (403), and that the role
 * is the registry's own answer (`/auth/me`), never a guess.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import { IDENTITY_QUERY_KEY, type IdentityState } from '@/auth/session'
import { below, RANK_ADMIN, RANK_CONTRIBUTOR, rankOf, rankOfState } from '@/auth/roles'
import AlertsWatchesMerged, { DELIVERIES_NEEDS_ADMIN } from '@/panels/merged/AlertsWatches'
import SourceEdits from '@/v4/watch/SourceEdits'
import { alertLogRoute, ALERT_LOG_ROUTE, READER_ALERT_LOG_ROUTE } from '@/v4/watch/AlertLog'
import { resetWatch } from '@/v4/watch/state'
import { SurfaceBar } from '@/components/SurfaceBar'
import { readerDescriptorDetail } from '@/components/inspector/useInspectorDetail'
import { SUBJECT_ROSTER_ROUTE, ANALYST_ROSTER_ROUTE, SOURCE_ROSTER_ROUTE } from '@/lib/deskNames'

function signedIn(role: string): IdentityState {
  return {
    state: 'signed_in',
    identity: {
      kind: 'user', actor: 'user:x', role: role as never, session_rejected: false, signin: true,
      user: { id: 'x', actor: 'user:x', email: null, display_name: 'X', role: role as never, status: 'active', auth_provider: 'password', last_sign_in_at: null },
    },
  }
}

function wrap(ui: ReactElement, identity: IdentityState | null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (identity) qc.setQueryData(IDENTITY_QUERY_KEY, identity)
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const asked: string[] = []
function stubFetch(bodies: Record<string, unknown>) {
  asked.length = 0
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    const u = String(url)
    asked.push(u)
    const hit = Object.keys(bodies).find((k) => u.includes(k))
    return Promise.resolve({ ok: true, status: 200, json: async () => (hit ? bodies[hit] : { data: [] }) })
  }))
}

function reg(): PanelRegistration {
  return {
    id: 'p', panel_id: 'system_alerts_watches', descriptor_id: '(singleton)', descriptor_version: '0'.repeat(64),
    descriptor_family: 'target', analyst_id: null, title: 'Alerts & Watches', mode: 'personal',
    layout_slot: 'x', data_query: {}, binding: {}, retired: false,
    created_at: '2026-06-03T00:00:00Z', retired_at: null,
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  resetWatch()
})

describe('the role a console acts on', () => {
  it('maps every class the registry names onto a rank; unknown is not "below"', () => {
    expect(rankOf({ role: 'reader' })).toBe(1)
    expect(rankOf({ role: 'read' })).toBe(1)
    expect(rankOf({ role: 'reader-own-rows' })).toBe(1)
    expect(rankOf({ role: 'contributor' })).toBe(2)
    expect(rankOf({ role: 'admin' })).toBe(3)
    expect(rankOf({ role: 'operator' })).toBe(3)
    expect(rankOf({ role: 'nonsense' as never })).toBe(null)
    expect(rankOfState({ state: 'signed_out' })).toBe(null)
    expect(rankOfState({ state: 'unavailable', message: 'x' })).toBe(null)
    expect(below(null, RANK_ADMIN)).toBe(false)
    expect(below(1, RANK_CONTRIBUTOR)).toBe(true)
    expect(below(2, RANK_ADMIN)).toBe(true)
    expect(below(3, RANK_ADMIN)).toBe(false)
  })

  it('reads the reader roster and, below admin, the reader alert log', () => {
    for (const r of [SUBJECT_ROSTER_ROUTE, ANALYST_ROSTER_ROUTE, SOURCE_ROSTER_ROUTE]) {
      expect(r.startsWith('/v3/roster?family=')).toBe(true)
      expect(r).not.toContain('/registry/')
    }
    expect(alertLogRoute(true)).toBe(READER_ALERT_LOG_ROUTE)
    expect(alertLogRoute(false)).toBe(ALERT_LOG_ROUTE)
  })

  it('draws a descriptor for a reader without asking for it', () => {
    const d = readerDescriptorDetail({ kind: 'target', id: 't1', label: 'T one' } as never)
    expect(d.id).toBe('t1')
    expect(String(d.body.configuration)).toMatch(/admin/)
  })
})

describe('a reader sees a reason, never a refused request', () => {
  it('the operator side: the button is not drawn for a reader; nothing switches', () => {
    stubFetch({})
    const onSwitch = vi.fn()
    render(wrap(
      <SurfaceBar mode="personal" active="board" onSwitch={onSwitch} onReset={() => {}} onCopyLink={() => {}} onOpenPalette={() => {}} />,
      signedIn('reader'),
    ))
    expect(screen.queryByTestId('to-operator-side')).toBeNull()
    expect(onSwitch).not.toHaveBeenCalled()
  })

  it('a contributor (and a caller of unknown role) still crosses', () => {
    for (const who of [signedIn('contributor'), null]) {
      stubFetch({})
      const onSwitch = vi.fn()
      const { unmount } = render(wrap(
        <SurfaceBar mode="personal" active="board" onSwitch={onSwitch} onReset={() => {}} onCopyLink={() => {}} onOpenPalette={() => {}} />,
        who,
      ))
      const btn = screen.getByTestId('to-operator-side')
      expect(btn).not.toBeDisabled()
      fireEvent.click(btn)
      expect(onSwitch).toHaveBeenCalledWith('engine')
      unmount()
    }
  })

  it('Alerts & Watches: Deliveries says why, and the escalations are never asked', async () => {
    stubFetch({ '/v3/watchlist': [] })
    render(wrap(<AlertsWatchesMerged registration={reg()} scope={{}} mode="personal" />, signedIn('reader')))
    fireEvent.click(screen.getByTestId('alerts-watches-tab-deliveries'))
    expect(await screen.findByTestId('alerts-watches-deliveries-reader')).toHaveTextContent(DELIVERIES_NEEDS_ADMIN)
    expect(asked.some((u) => u.includes('/v3/system/escalations'))).toBe(false)
  })

  it('Watch edits: a reader gets the open-flag count, not the flags (no request)', async () => {
    stubFetch({ '/v3/evidence/drift?': { available: true, classified: 5, open_flags_total: 3, flags: { open: 3 } } })
    render(wrap(<SourceEdits />, signedIn('reader')))
    expect(await screen.findByTestId('watch-edits-flags-reader')).toHaveTextContent('3 open flags')
    await waitFor(() => expect(asked.some((u) => u.includes('/v3/evidence/drift?'))).toBe(true))
    expect(asked.some((u) => u.includes('/v3/evidence/drift/flags'))).toBe(false)
  })

  it('Watch edits: an admin still reads the flags', async () => {
    stubFetch({
      '/v3/evidence/drift/flags': { available: true, flags: [] },
      '/v3/evidence/drift?': { available: true, classified: 5, open_flags_total: 3, flags: { open: 3 } },
    })
    render(wrap(<SourceEdits />, signedIn('admin')))
    await waitFor(() => expect(asked.some((u) => u.includes('/v3/evidence/drift/flags'))).toBe(true))
    expect(screen.queryByTestId('watch-edits-flags-reader')).toBe(null)
  })
})
