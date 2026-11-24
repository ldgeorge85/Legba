/**
 * X4/D4 (W11-F76's phone half) — the phone surface has the workstation's door.
 *
 *   - `GET /auth/me` → 401: the SIGN-IN screen, and no report request is ever
 *     sent (the phone no longer renders a page of bare 401s);
 *   - `GET /auth/me` → an identity: the reports navigator;
 *   - a session that ends while the page is open (the next read answers 401):
 *     the sign-in screen, with the reason;
 *   - a way back to the desk (`/?desktop=1`);
 *   - the class-name guard: no mobile class is a Tailwind margin utility.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import MobileApp from './MobileApp'
import { SignInGate } from '@/auth/SignInGate'
import { purgeStoredReads } from './mobileCache'
import { __resetReadTelemetry } from '@/lib/readTelemetry'
import { consultActions } from '@/state/consultSession'
import { MOBILE_PANEL_ID } from './mobileConsult'

const OPERATOR = { kind: 'operator', actor: 'operator:bearer:abc', role: 'operator', user: null, session_rejected: false }

const requested: string[] = []

function stub(routes: { me: number; findings?: number }) {
  requested.length = 0
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const u = String(url)
      requested.push(u)
      const reply = (status: number, body: unknown) =>
        ({
          ok: status < 400,
          status,
          json: async () => body,
          text: async () => JSON.stringify(body),
        }) as unknown as Response
      if (u.includes('/auth/me')) return routes.me === 200 ? reply(200, OPERATOR) : reply(routes.me, { detail: 'no credential' })
      if (u.includes('/findings')) {
        return (routes.findings ?? 200) === 200 ? reply(200, { data: [] }) : reply(routes.findings!, { detail: 'expired' })
      }
      if (u.includes('/journal')) return reply(200, { consolidation: null, entries: [], next_cursor: null, calibration: {} })
      return reply(200, { data: [] })
    }),
  )
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

beforeEach(() => {
  window.history.replaceState(null, '', '/')
  localStorage.clear()
  purgeStoredReads()
  __resetReadTelemetry()
  consultActions().reset(MOBILE_PANEL_ID)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the phone surface behind the sign-in gate', () => {
  it('me → 401: the sign-in screen, and no report request is sent', async () => {
    stub({ me: 401 })
    render(
      wrap(
        <SignInGate>
          <MobileApp />
        </SignInGate>,
      ),
    )
    await screen.findByTestId('sign-in-screen')
    expect(screen.queryByTestId('reports-list')).toBeNull()
    expect(requested.some((u) => u.includes('/findings'))).toBe(false)
  })

  it('me → an identity: the navigator renders', async () => {
    stub({ me: 200 })
    render(
      wrap(
        <SignInGate>
          <MobileApp />
        </SignInGate>,
      ),
    )
    await screen.findByTestId('reports-list')
    expect(screen.queryByTestId('sign-in-screen')).toBeNull()
  })

  it('a session that ends mid-use (the read answers 401) shows the sign-in screen with the reason', async () => {
    stub({ me: 200, findings: 401 })
    render(
      wrap(
        <SignInGate>
          <MobileApp />
        </SignInGate>,
      ),
    )
    const screenEl = await screen.findByTestId('sign-in-screen')
    expect(screenEl.textContent ?? '').toMatch(/session has ended/i)
  })

  it('the navigator offers the way back to the desk, remembered for the tab', async () => {
    stub({ me: 200 })
    render(wrap(<MobileApp />))
    const link = await screen.findByTestId('desktop-view')
    expect(link.getAttribute('href')).toBe('/?desktop=1')
    await waitFor(() => expect(screen.getByTestId('reports-list')).toBeInTheDocument())
  })
})

describe('no mobile class is a Tailwind utility', () => {
  const files = import.meta.glob('/src/mobile/**/*.{tsx,css}', { query: '?raw', import: 'default', eager: true }) as Record<
    string,
    string
  >
  // tailwind.config.ts `spacing` adds `row` and `density*`: `m-row` compiled to
  // `margin: var(--row-h)` and pushed the whole navigator 27 px off a 390 px screen.
  const COLLIDES = /(?<![\w-])\.?m-(row|density|density-gap|density-row|density-section)(?![\w-])/

  it('the sources never spell m-row / m-density* (the margin utility that bit the navigator)', () => {
    const hits = Object.entries(files)
      .filter(([path]) => !path.endsWith('.test.tsx'))
      .filter(([, src]) => COLLIDES.test(src.replace(/\/\*[\s\S]*?\*\//g, '')))
      .map(([path]) => path)
    expect(hits).toEqual([])
  })

  it('the navigator card is m-card', () => {
    const list = Object.entries(files).find(([p]) => p.endsWith('ReportsList.tsx'))![1]
    expect(list).toContain('className="m-card"')
  })
})
