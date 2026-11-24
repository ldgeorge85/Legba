/**
 * X3-B3 — the sign-in gate, the two screens and the identity chip.
 *
 * The gate renders exactly ONE of: the shell, the sign-in screen, the
 * invite-accept screen, a "checking" line — from the registry's answer to
 * `GET /api/v1/auth/me` and the address. Pinned here:
 *
 *   - the perimeter operator passes as before (me → operator → the shell);
 *   - no accepted credential (me → 401) → the sign-in screen;
 *   - a registry failure that is NOT a 401 → the shell (the gate is the door,
 *     not the lock — the registry enforces every route);
 *   - `#invite=<token>` → the accept screen; `#signin` → the sign-in screen;
 *   - the forms send what the registry expects and print its refusal;
 *   - the chip names the person and their role class, or the perimeter.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { SignInGate } from './SignInGate'
import { InviteAcceptScreen, SignInScreen } from './SignInScreen'
import { IdentityChip } from './IdentityChip'
import { inviteTokenFromHash, refusalText, wantsSignIn, type Identity } from './session'
import { ApiError } from '@/lib/api'

const OPERATOR: Identity = {
  kind: 'operator',
  actor: 'operator:bearer:abc123abc123',
  role: 'operator',
  user: null,
  session_rejected: false,
}

const BOB: Identity = {
  kind: 'user',
  actor: 'user:7f0c',
  role: 'reader-own-rows',
  session_rejected: false,
  user: {
    id: '7f0c0000-0000-0000-0000-000000000000',
    actor: 'user:7f0c',
    email: 'bob@example.org',
    display_name: 'Bob',
    role: 'reader-own-rows',
    status: 'active',
    auth_provider: 'local_password',
    last_sign_in_at: '2026-10-08T12:00:00Z',
  },
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

describe('the gate', () => {
  it('lets the perimeter operator through, exactly as before', async () => {
    stubFetch({ '/api/v1/auth/me': { status: 200, body: OPERATOR } })
    render(wrap(<SignInGate>{SHELL}</SignInGate>))
    expect(screen.getByTestId('sign-in-checking')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('the-shell')).toBeInTheDocument())
    expect(screen.queryByTestId('sign-in-screen')).toBeNull()
  })

  it('shows the sign-in screen when the registry accepts no credential (401)', async () => {
    stubFetch({ '/api/v1/auth/me': { status: 401, body: { detail: 'missing Authorization' } } })
    render(wrap(<SignInGate>{SHELL}</SignInGate>))
    await waitFor(() => expect(screen.getByTestId('sign-in-screen')).toBeInTheDocument())
    expect(screen.queryByTestId('the-shell')).toBeNull()
  })

  it('does NOT lock the shell on a registry failure that is not a 401', async () => {
    stubFetch({ '/api/v1/auth/me': { status: 502, body: 'bad gateway' } })
    render(wrap(<SignInGate>{SHELL}</SignInGate>))
    await waitFor(() => expect(screen.getByTestId('the-shell')).toBeInTheDocument())
  })

  it('a signed-in person passes', async () => {
    stubFetch({ '/api/v1/auth/me': { status: 200, body: BOB } })
    render(wrap(<SignInGate>{SHELL}</SignInGate>))
    await waitFor(() => expect(screen.getByTestId('the-shell')).toBeInTheDocument())
  })

  it('#invite=<token> opens the accept screen whoever is asking', async () => {
    stubFetch({ '/api/v1/auth/me': { status: 200, body: OPERATOR } })
    window.location.hash = '#invite=tok-123'
    render(wrap(<SignInGate>{SHELL}</SignInGate>))
    expect(screen.getByTestId('invite-accept-screen')).toBeInTheDocument()
    expect(screen.queryByTestId('the-shell')).toBeNull()
  })

  it('#signin opens the sign-in screen, with a way back for the operator', async () => {
    stubFetch({ '/api/v1/auth/me': { status: 200, body: OPERATOR } })
    window.location.hash = '#signin'
    render(wrap(<SignInGate>{SHELL}</SignInGate>))
    expect(screen.getByTestId('sign-in-screen')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('sign-in-cancel')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('sign-in-cancel'))
    await waitFor(() => expect(screen.getByTestId('the-shell')).toBeInTheDocument())
  })
})

describe('the forms', () => {
  it('sign-in posts email + password and reports success', async () => {
    stubFetch({ '/api/v1/auth/sign-in': { status: 200, body: { user: BOB.user } } })
    const onDone = vi.fn()
    render(wrap(<SignInScreen onDone={onDone} />))
    const submit = screen.getByTestId('sign-in-submit')
    expect(submit).toBeDisabled()
    fireEvent.change(screen.getByTestId('sign-in-email'), { target: { value: ' bob@example.org ' } })
    fireEvent.change(screen.getByTestId('sign-in-password'), { target: { value: 'correct horse battery' } })
    fireEvent.click(submit)
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
    const post = calls.find((c) => c.url.endsWith('/api/v1/auth/sign-in'))!
    expect(post.init?.method).toBe('POST')
    expect(JSON.parse(String(post.init?.body))).toEqual({
      email: 'bob@example.org',
      password: 'correct horse battery',
    })
  })

  it('sign-in prints the registry refusal by its message', async () => {
    stubFetch({
      '/api/v1/auth/sign-in': {
        status: 401,
        body: { detail: { error: 'invalid_credentials', message: 'email or password is wrong' } },
      },
    })
    const onDone = vi.fn()
    render(wrap(<SignInScreen onDone={onDone} />))
    fireEvent.change(screen.getByTestId('sign-in-email'), { target: { value: 'bob@example.org' } })
    fireEvent.change(screen.getByTestId('sign-in-password'), { target: { value: 'nope nope nope' } })
    fireEvent.click(screen.getByTestId('sign-in-submit'))
    await waitFor(() =>
      expect(screen.getByTestId('auth-refusal')).toHaveTextContent('email or password is wrong'),
    )
    expect(onDone).not.toHaveBeenCalled()
  })

  it('accept needs the password twice and at least 12 characters', async () => {
    stubFetch({ '/api/v1/auth/accept': { status: 200, body: { user: BOB.user } } })
    const onDone = vi.fn()
    render(wrap(<InviteAcceptScreen token="tok-123" onDone={onDone} />))
    const submit = screen.getByTestId('invite-submit')
    fireEvent.change(screen.getByTestId('invite-password'), { target: { value: 'short' } })
    expect(screen.getByText('5 of 12 characters')).toBeInTheDocument()
    expect(submit).toBeDisabled()
    fireEvent.change(screen.getByTestId('invite-password'), { target: { value: 'correct horse battery' } })
    fireEvent.change(screen.getByTestId('invite-confirm'), { target: { value: 'correct horse batterx' } })
    expect(screen.getByText('the two passwords differ')).toBeInTheDocument()
    expect(submit).toBeDisabled()
    fireEvent.change(screen.getByTestId('invite-confirm'), { target: { value: 'correct horse battery' } })
    fireEvent.change(screen.getByTestId('invite-name'), { target: { value: '  Bob  ' } })
    expect(submit).not.toBeDisabled()
    fireEvent.click(submit)
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
    const post = calls.find((c) => c.url.endsWith('/api/v1/auth/accept'))!
    expect(JSON.parse(String(post.init?.body))).toEqual({
      token: 'tok-123',
      password: 'correct horse battery',
      display_name: 'Bob',
    })
  })

  it('accept prints a used / expired invite by name', async () => {
    stubFetch({
      '/api/v1/auth/accept': {
        status: 401,
        body: { detail: { error: 'invite_used', message: 'this invite link was already used' } },
      },
    })
    render(wrap(<InviteAcceptScreen token="tok-123" onDone={vi.fn()} />))
    fireEvent.change(screen.getByTestId('invite-password'), { target: { value: 'correct horse battery' } })
    fireEvent.change(screen.getByTestId('invite-confirm'), { target: { value: 'correct horse battery' } })
    fireEvent.click(screen.getByTestId('invite-submit'))
    await waitFor(() =>
      expect(screen.getByTestId('auth-refusal')).toHaveTextContent('this invite link was already used'),
    )
  })
})

describe('the identity chip', () => {
  it('names the person and their role class, with sign out', async () => {
    stubFetch({ '/api/v1/auth/me': { status: 200, body: BOB } })
    render(wrap(<IdentityChip />))
    expect(screen.getByTestId('identity-chip')).toHaveTextContent('identity —')
    await waitFor(() => expect(screen.getByTestId('identity-name')).toHaveTextContent('Bob'))
    expect(screen.getByTestId('identity-chip')).toHaveTextContent('· reader-own-rows')
    expect(screen.getByTestId('identity-sign-out')).toBeInTheDocument()
  })

  it('names the perimeter operator, with a way to sign in as a person', async () => {
    stubFetch({ '/api/v1/auth/me': { status: 200, body: OPERATOR } })
    render(wrap(<IdentityChip />))
    await waitFor(() =>
      expect(screen.getByTestId('identity-name')).toHaveTextContent('operator · perimeter'),
    )
    fireEvent.click(screen.getByTestId('identity-sign-in'))
    expect(window.location.hash).toBe('#signin')
  })

  it('says unavailable — never a made-up identity — when the read fails', async () => {
    stubFetch({ '/api/v1/auth/me': { status: 500, body: 'boom' } })
    render(wrap(<IdentityChip />))
    await waitFor(() =>
      expect(screen.getByTestId('identity-chip')).toHaveTextContent('identity unavailable'),
    )
  })
})

describe('the address + refusal helpers', () => {
  it('reads the invite token and the sign-in ask from the fragment', () => {
    expect(inviteTokenFromHash('#invite=abc-_123')).toBe('abc-_123')
    expect(inviteTokenFromHash('#ws=watch&invite=x%2Dy')).toBe('x-y')
    expect(inviteTokenFromHash('#ws=watch')).toBeNull()
    expect(inviteTokenFromHash('#invite=')).toBeNull()
    expect(wantsSignIn('#signin')).toBe(true)
    expect(wantsSignIn('#ws=watch&signin')).toBe(true)
    expect(wantsSignIn('#signinx')).toBe(false)
  })

  it('prints detail.message, else a status — never an empty refusal', () => {
    expect(refusalText(new ApiError(403, { detail: { error: 'user_disabled', message: 'disabled' } }))).toBe(
      'disabled',
    )
    expect(refusalText(new ApiError(503, { detail: 'down' }))).toBe('down')
    expect(refusalText(new ApiError(500, 'x'))).toBe('the registry refused (500)')
  })
})
