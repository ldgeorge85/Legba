/**
 * The Users page: offered to admins only (the catalog and ⌘K), silent for
 * anyone else, an explanation while sign-in is off, the invite link shown
 * once with the no-email note, and every change behind a one-sentence
 * confirmation with the registry's refusals in plain words.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { IDENTITY_QUERY_KEY, type IdentityState } from '@/auth/session'
import { RANK_ADMIN, RANK_CONTRIBUTOR, RANK_READER } from '@/auth/roles'
import { ADMIN_ONLY_VIEWS, USERS_VIEW, offeredToRank } from '@/auth/adminViews'
import { catalogEntries } from '@/components/CatalogPopover'
import { adminViewEntries } from '@/components/CommandPalette'
import { declaredContract } from '@/panel-registry/contracts'
import '@/panels/users/register'
import UsersView from './UsersView'
import type { AccountsAnswer } from './api'
import { blockedReason, confirmSentence, countActiveAdmins, inviteLink, personName } from './model'

function signedIn(role: string): IdentityState {
  return {
    state: 'signed_in',
    identity: {
      kind: 'user', actor: 'user:a1', role: role as never, session_rejected: false, signin: true,
      user: { id: 'a1', actor: 'user:a1', email: 'ada@x.org', display_name: 'Ada', role: role as never, status: 'active', auth_provider: 'local_password', last_sign_in_at: null },
    },
  }
}

function wrap(ui: ReactElement, identity: IdentityState | null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (identity) qc.setQueryData(IDENTITY_QUERY_KEY, identity)
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const NOW = new Date().toISOString()
const LATER = new Date(Date.now() + 5 * 86_400_000).toISOString()

function accounts(): AccountsAnswer {
  return {
    signin: true,
    you: 'a1',
    roles: ['reader', 'contributor', 'admin'],
    users: [
      { id: 'o1', actor: 'user:o1', email: 'olga@x.org', display_name: 'Olga', role: 'admin', status: 'active', is_owner: true, created_at: NOW, last_sign_in_at: NOW, last_seen_at: NOW, live_sessions: 1 },
      { id: 'a1', actor: 'user:a1', email: 'ada@x.org', display_name: 'Ada', role: 'admin', status: 'active', is_owner: false, created_at: NOW, last_sign_in_at: NOW, last_seen_at: NOW, live_sessions: 2 },
      { id: 'c1', actor: 'user:c1', email: 'cato@x.org', display_name: '', role: 'contributor', status: 'active', is_owner: false, created_at: NOW, last_sign_in_at: null, last_seen_at: null, live_sessions: 1 },
    ],
    invites: [
      { user_id: 'i1', email: 'nell@x.org', display_name: '', role: 'reader', invited_by: 'user:a1', created_at: NOW, expires_at: LATER, expired: false },
    ],
  }
}

interface Call {
  url: string
  method: string
  body: unknown
}
const calls: Call[] = []

function respond(status: number, body: unknown) {
  return Promise.resolve({
    ok: status < 400,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  })
}

function stubRegistry(over: Partial<Record<string, { status: number; body: unknown }>> = {}) {
  calls.length = 0
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const method = (init?.method ?? 'GET').toUpperCase()
      const u = String(url)
      calls.push({ url: u, method, body: init?.body ? JSON.parse(String(init.body)) : null })
      const key = `${method} ${u.replace('/api/v1', '')}`
      const hit = over[key]
      if (hit) return respond(hit.status, hit.body)
      if (key === 'GET /auth/admin/users') return respond(200, accounts())
      if (key === 'POST /auth/admin/invites') {
        return respond(201, {
          invite: { user_id: 'i2', email: 'wes@x.org', display_name: '', role: 'contributor', expires_at: LATER },
          invite_path: '/#invite=TOKEN-abc',
          expires_at: LATER,
        })
      }
      return respond(200, { changed: true })
    }),
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('who is offered the Users page', () => {
  it('is registered as an operator view on the stage', () => {
    const c = declaredContract(USERS_VIEW)
    expect(c).toMatchObject({ side: 'operator', home: 'stage', name: 'Users' })
    expect(ADMIN_ONLY_VIEWS.has(USERS_VIEW)).toBe(true)
  })

  it('is left out of the catalog below admin and listed for an admin', () => {
    const kinds = (rank: 1 | 2 | 3 | null) => catalogEntries('personal', 'operator', rank).map((e) => e.kind)
    expect(kinds(RANK_READER)).not.toContain(USERS_VIEW)
    expect(kinds(RANK_CONTRIBUTOR)).not.toContain(USERS_VIEW)
    expect(kinds(RANK_ADMIN)).toContain(USERS_VIEW)
    // Never on the reader side's catalog, whatever the rank.
    expect(catalogEntries('personal', 'reader', RANK_ADMIN).map((e) => e.kind)).not.toContain(USERS_VIEW)
  })

  it('is left out of ⌘K below admin and found by "invite" for an admin', () => {
    expect(adminViewEntries(RANK_READER)).toEqual([])
    expect(adminViewEntries(RANK_CONTRIBUTOR)).toEqual([])
    const admin = adminViewEntries(RANK_ADMIN)
    expect(admin.map((e) => e.key)).toEqual([`panel:${USERS_VIEW}`])
    expect(admin[0].search.toLowerCase()).toContain('invite')
    expect(offeredToRank('view:forecasts', RANK_READER)).toBe(true)
  })
})

describe('the page', () => {
  it('asks nothing of the registry for a contributor and says it needs an admin', async () => {
    stubRegistry()
    render(wrap(<UsersView />, signedIn('contributor')))
    expect(screen.getByTestId('users-needs-admin')).toBeTruthy()
    await new Promise((r) => setTimeout(r, 20))
    expect(calls.filter((c) => c.url.includes('/auth/admin/'))).toEqual([])
  })

  it('explains how to turn sign-in on instead of an empty table', async () => {
    stubRegistry({
      'GET /auth/admin/users': {
        status: 409,
        body: { detail: { error: 'signin_not_enabled', message: 'Sign-in is not enabled on this instance' } },
      },
    })
    render(wrap(<UsersView />, signedIn('operator')))
    const off = await screen.findByTestId('users-signin-off')
    expect(off.textContent).toContain('LEGBA_AUTH_SIGNIN=1')
    expect(off.textContent).toContain('users_bootstrap')
    expect(screen.queryByTestId('users-table')).toBeNull()
  })

  it('lists accounts with role and state, and pending invites with who sent them', async () => {
    stubRegistry()
    render(wrap(<UsersView />, signedIn('admin')))
    const rows = await screen.findAllByTestId('users-row')
    expect(rows).toHaveLength(3)
    expect(screen.getByTestId('users-head').textContent).toContain('3 accounts · 1 pending invite')
    expect(screen.getAllByTestId('users-owner')).toHaveLength(1)
    // The owner's role cannot be changed here.
    const selects = screen.getAllByTestId('users-role-select') as HTMLSelectElement[]
    expect(selects[0].disabled).toBe(true)
    expect(selects[2].value).toBe('contributor')
    const invite = screen.getByTestId('users-invite-row')
    expect(invite.textContent).toContain('nell@x.org')
    expect(invite.textContent).toContain('by Ada')
  })

  it('shows the invite link once, with copy, and says no email is sent', async () => {
    stubRegistry()
    const writeText = vi.fn(() => Promise.resolve())
    Object.assign(navigator, { clipboard: { writeText } })
    render(wrap(<UsersView />, signedIn('admin')))
    fireEvent.click(await screen.findByTestId('users-invite-open'))
    fireEvent.change(screen.getByTestId('users-invite-email'), { target: { value: 'wes@x.org' } })
    fireEvent.click(screen.getByTestId('users-invite-role-contributor'))
    fireEvent.click(screen.getByTestId('users-invite-submit'))
    const value = (await screen.findByTestId('users-invite-link-value')) as HTMLInputElement
    expect(value.value).toBe(`${window.location.origin}/#invite=TOKEN-abc`)
    expect(screen.getByTestId('users-invite-no-email').textContent).toMatch(/No email is sent/)
    const post = calls.find((c) => c.method === 'POST' && c.url.endsWith('/auth/admin/invites'))
    expect(post?.body).toEqual({ email: 'wes@x.org', role: 'contributor', display_name: '' })
    fireEvent.click(screen.getByTestId('users-invite-copy'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/#invite=TOKEN-abc`))
    fireEvent.click(screen.getByTestId('users-invite-done'))
    expect(screen.queryByTestId('users-invite-link')).toBeNull()
    expect(document.body.textContent).not.toContain('TOKEN-abc')
  })

  it('states the effect in one sentence before disabling, then sends it', async () => {
    stubRegistry()
    render(wrap(<UsersView />, signedIn('admin')))
    const rows = await screen.findAllByTestId('users-row')
    fireEvent.click(rows[2].querySelector('[data-testid="users-disable"]')!)
    expect(screen.getByTestId('users-confirm-sentence').textContent).toBe(
      'cato@x.org will be signed out everywhere at once and will not be able to sign in until enabled again.',
    )
    expect(calls.some((c) => c.method === 'POST' && c.url.includes('/disable'))).toBe(false)
    fireEvent.click(screen.getByTestId('users-confirm-go'))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url.endsWith('/auth/admin/users/c1/disable'))).toBe(true))
  })

  it('confirms a role change and shows a refusal in plain words', async () => {
    stubRegistry({ 'POST /auth/admin/users/c1/role': { status: 409, body: { detail: { error: 'last_admin', message: 'x' } } } })
    render(wrap(<UsersView />, signedIn('admin')))
    const rows = await screen.findAllByTestId('users-row')
    fireEvent.change(rows[2].querySelector('select')!, { target: { value: 'reader' } })
    expect(screen.getByTestId('users-confirm-sentence').textContent).toContain('cato@x.org will become reader')
    fireEvent.click(screen.getByTestId('users-confirm-go'))
    expect((await screen.findByTestId('users-confirm-error')).textContent).toBe(
      'This is the only active admin. Make someone else an admin first.',
    )
  })

  it('withdraws a pending invite after a confirmation', async () => {
    stubRegistry()
    render(wrap(<UsersView />, signedIn('admin')))
    fireEvent.click(await screen.findByTestId('users-revoke'))
    expect(screen.getByTestId('users-confirm-sentence').textContent).toContain('will stop working')
    fireEvent.click(screen.getByTestId('users-confirm-go'))
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE' && c.url.endsWith('/auth/admin/invites/i1'))).toBe(true))
  })
})

describe('the rules the page mirrors', () => {
  const base = accounts().users
  it('never offers to demote or disable the owner or the last admin', () => {
    expect(blockedReason(base[0], { kind: 'disable' }, 2)).toMatch(/owner/)
    expect(blockedReason(base[0], { kind: 'role', role: 'reader' }, 2)).toMatch(/owner/)
    expect(blockedReason(base[1], { kind: 'role', role: 'reader' }, 2)).toBeNull()
    expect(blockedReason(base[1], { kind: 'disable' }, 1)).toMatch(/only active admin/)
    expect(blockedReason(base[2], { kind: 'disable' }, 1)).toBeNull()
    expect(countActiveAdmins(base)).toBe(2)
  })

  it('says each effect in one plain sentence', () => {
    for (const a of [{ kind: 'disable' }, { kind: 'enable' }, { kind: 'end_sessions' }, { kind: 'revoke' }, { kind: 'role', role: 'admin' }] as const) {
      const s = confirmSentence(a, 'Cato')
      expect(s.endsWith('.')).toBe(true)
      expect(s.split('. ').length).toBe(1)
    }
    expect(confirmSentence({ kind: 'end_sessions' }, 'Ada', true)).toContain('including this one')
    expect(personName(base[2])).toBe('cato@x.org')
    expect(inviteLink('https://legba.example/', '/#invite=t')).toBe('https://legba.example/#invite=t')
  })
})
