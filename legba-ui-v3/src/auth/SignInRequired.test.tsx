/**
 * A-1 — the console with the registry's sign-in switch ON
 * (`LEGBA_AUTH_SIGNIN=1`). The edge hands the browser no credential, so:
 *
 *   - no session cookie → `GET /auth/me` answers 401 → the sign-in screen,
 *     with no way back into the shell (nothing to go back to);
 *   - a signed-in person passes; the chip shows the new role names and
 *     marks the owner;
 *   - a credential stored in this browser (`admin-token`, the break-glass
 *     one) is named as such, never as "the operator";
 *   - the sign-in form posts only email + password (the cookie is HttpOnly:
 *     the client never reads or sends a token);
 *   - with the switch OFF the answer carries no `signin`, and
 *     `signInRequired` is false — the October behaviour, pinned by
 *     SignInGate.test.tsx, is untouched.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { SignInGate } from './SignInGate'
import { IdentityChip } from './IdentityChip'
import { signInRequired, type Identity } from './session'

const OWNER: Identity = {
  kind: 'user',
  actor: 'user:9a1b',
  role: 'admin',
  is_owner: true,
  signin: true,
  service: null,
  session_rejected: false,
  user: {
    id: '9a1b0000-0000-0000-0000-000000000000',
    actor: 'user:9a1b',
    email: 'owner@example.org',
    display_name: 'Ada',
    role: 'admin',
    status: 'active',
    auth_provider: 'local_password',
    last_sign_in_at: '2026-11-14T08:00:00Z',
    is_owner: true,
  },
}

const CONTRIBUTOR: Identity = {
  ...OWNER,
  actor: 'user:c0de',
  role: 'contributor',
  is_owner: false,
  user: { ...OWNER.user!, id: 'c0de', actor: 'user:c0de', display_name: 'Cy', role: 'contributor', is_owner: false },
}

const BREAK_GLASS: Identity = {
  kind: 'admin-token',
  actor: 'admin-token:bearer:0123456789ab',
  role: 'admin',
  is_owner: false,
  signin: true,
  service: null,
  session_rejected: false,
  user: null,
}

type Reply = { status: number; body: unknown }
const calls: Array<{ url: string; init?: RequestInit }> = []

function stubFetch(routes: Record<string, Reply>) {
  calls.length = 0
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), init })
      const hit = Object.entries(routes).find(([k]) => String(url).endsWith(k))
      const { status, body } = hit ? hit[1] : { status: 200, body: {} }
      return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => body,
        text: async () => JSON.stringify(body),
      } as unknown as Response
    }),
  )
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const SHELL = <div data-testid="the-shell">workstation</div>

beforeEach(() => {
  window.location.hash = ''
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.location.hash = ''
})

describe('sign-in required', () => {
  it('no session → 401 → the sign-in screen, and no way around it', async () => {
    stubFetch({ '/api/v1/auth/me': { status: 401, body: { detail: 'missing Authorization: Bearer <token>' } } })
    render(wrap(<SignInGate>{SHELL}</SignInGate>))
    await waitFor(() => expect(screen.getByTestId('sign-in-screen')).toBeInTheDocument())
    expect(screen.queryByTestId('the-shell')).toBeNull()
    expect(screen.queryByTestId('sign-in-cancel')).toBeNull()
  })

  it('the form posts only email + password; no token leaves the client', async () => {
    stubFetch({
      '/api/v1/auth/me': { status: 401, body: {} },
      '/api/v1/auth/sign-in': { status: 200, body: { user: OWNER.user } },
    })
    render(wrap(<SignInGate>{SHELL}</SignInGate>))
    await waitFor(() => expect(screen.getByTestId('sign-in-screen')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('sign-in-email'), { target: { value: 'owner@example.org' } })
    fireEvent.change(screen.getByTestId('sign-in-password'), { target: { value: 'a long passphrase' } })
    fireEvent.click(screen.getByTestId('sign-in-submit'))
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/auth/sign-in'))).toBe(true))
    const post = calls.find((c) => c.url.endsWith('/auth/sign-in'))!
    expect(JSON.parse(String(post.init?.body))).toEqual({
      email: 'owner@example.org',
      password: 'a long passphrase',
    })
    const headers = new Headers(post.init?.headers as HeadersInit | undefined)
    expect(headers.get('Authorization')).toBeNull()
  })

  it('a signed-in owner passes; the chip shows the role and the owner mark', async () => {
    stubFetch({ '/api/v1/auth/me': { status: 200, body: OWNER } })
    render(wrap(<SignInGate>{SHELL}</SignInGate>))
    await waitFor(() => expect(screen.getByTestId('the-shell')).toBeInTheDocument())
    cleanup()
    render(wrap(<IdentityChip />))
    await waitFor(() => expect(screen.getByTestId('identity-name')).toHaveTextContent('Ada'))
    expect(screen.getByTestId('identity-chip')).toHaveTextContent('· admin')
    expect(screen.getByTestId('identity-owner')).toBeInTheDocument()
    expect(screen.getByTestId('identity-sign-out')).toBeInTheDocument()
  })

  it('a contributor carries no owner mark', async () => {
    stubFetch({ '/api/v1/auth/me': { status: 200, body: CONTRIBUTOR } })
    render(wrap(<IdentityChip />))
    await waitFor(() => expect(screen.getByTestId('identity-name')).toHaveTextContent('Cy'))
    expect(screen.getByTestId('identity-chip')).toHaveTextContent('· contributor')
    expect(screen.queryByTestId('identity-owner')).toBeNull()
  })

  it('names a stored break-glass token as such, never as the operator', async () => {
    stubFetch({ '/api/v1/auth/me': { status: 200, body: BREAK_GLASS } })
    render(wrap(<IdentityChip storedToken />))
    await waitFor(() =>
      expect(screen.getByTestId('identity-name')).toHaveTextContent('admin token · break-glass'),
    )
    expect(screen.getByTestId('identity-chip')).not.toHaveTextContent('operator')
    expect(screen.getByTestId('identity-stored-token')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('identity-sign-in'))
    expect(window.location.hash).toBe('#signin')
  })

  it('signInRequired reads the registry flag; absent (switch off) is false', () => {
    expect(signInRequired(OWNER)).toBe(true)
    const off: Identity = {
      kind: 'operator',
      actor: 'operator:bearer:abc123abc123',
      role: 'operator',
      user: null,
      session_rejected: false,
    }
    expect(signInRequired(off)).toBe(false)
  })
})
