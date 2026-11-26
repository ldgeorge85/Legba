/**
 * The Users page's reads and writes — `/api/v1/auth/admin/*` (admin only; the
 * registry refuses everyone else and every service credential).
 *
 * The invite link comes back in the create answer and nowhere else: the
 * registry stores only a hash of it, so the page shows it once and cannot
 * show it again.
 */
import { ApiError, apiDelete, apiPost, readErrorBody } from '@/lib/api'

export type UserRole = 'reader' | 'contributor' | 'admin'
export type UserState = 'active' | 'disabled'

export interface AccountRow {
  id: string
  actor: string
  email: string | null
  display_name: string
  role: UserRole
  status: UserState
  is_owner?: boolean
  created_at: string | null
  last_sign_in_at: string | null
  last_seen_at: string | null
  live_sessions: number
}

export interface PendingInvite {
  user_id: string
  email: string | null
  display_name: string
  role: UserRole
  invited_by: string | null
  created_at: string | null
  expires_at: string | null
  expired: boolean
}

export interface AccountsAnswer {
  signin: true
  users: AccountRow[]
  invites: PendingInvite[]
  roles: UserRole[]
  /** The signed-in admin's own account id (null for the break-glass credential). */
  you: string | null
}

export interface InviteMade {
  invite: { user_id: string; email: string | null; display_name: string; role: UserRole; expires_at: string }
  /** `/#invite=<token>` — the accept screen reads it from the address. */
  invite_path: string
  expires_at: string
}

export const USERS_QUERY_KEY = ['auth', 'admin', 'users'] as const

/**
 * The list. Fetched directly (not through the shared GET cache): an admin's
 * change must show at once, and this answer is never shared with another read.
 */
export async function fetchAccounts(): Promise<AccountsAnswer> {
  const token = localStorage.getItem('legba_token')
  const res = await fetch('/api/v1/auth/admin/users', {
    headers: { Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    cache: 'no-store',
  })
  if (!res.ok) throw new ApiError(res.status, await readErrorBody(res))
  return (await res.json()) as AccountsAnswer
}

export function createInvite(email: string, role: UserRole, displayName: string): Promise<InviteMade> {
  return apiPost<InviteMade>('/auth/admin/invites', { email: email.trim(), role, display_name: displayName.trim() })
}

export function revokeInvite(userId: string): Promise<{ revoked: boolean }> {
  return apiDelete(`/auth/admin/invites/${encodeURIComponent(userId)}`)
}

export function changeRole(userId: string, role: UserRole): Promise<unknown> {
  return apiPost(`/auth/admin/users/${encodeURIComponent(userId)}/role`, { role })
}

export function disableUser(userId: string): Promise<unknown> {
  return apiPost(`/auth/admin/users/${encodeURIComponent(userId)}/disable`, {})
}

export function enableUser(userId: string): Promise<unknown> {
  return apiPost(`/auth/admin/users/${encodeURIComponent(userId)}/enable`, {})
}

export function endSessions(userId: string): Promise<{ sessions_ended: number }> {
  return apiPost(`/auth/admin/users/${encodeURIComponent(userId)}/end-sessions`, {})
}

/** The registry's refusal code (`detail.error`), when it named one. */
export function refusalCode(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null
  const body = err.body as { detail?: unknown } | null | undefined
  const detail = body && typeof body === 'object' ? body.detail : undefined
  if (detail && typeof detail === 'object' && 'error' in detail) {
    const code = (detail as { error?: unknown }).error
    return typeof code === 'string' ? code : null
  }
  return null
}

/** Plain words for each refusal the page can meet. */
const PLAIN: Record<string, string> = {
  signin_not_enabled: 'Sign-in is not enabled on this instance.',
  role_forbidden: 'Managing accounts needs an admin.',
  owner_protected: 'The owner always stays an active admin. Changing the owner is done on the host.',
  last_admin: 'This is the only active admin. Make someone else an admin first.',
  user_exists: 'That email already has an account. Change its role or enable it instead of inviting it.',
  user_invited: 'This person has not accepted their invite yet. Withdraw the invite and send a new one.',
  user_unknown: 'That account no longer exists. The list has been refreshed.',
  invite_unknown: 'That invite was already accepted or withdrawn. The list has been refreshed.',
  email_invalid: 'That is not an email address.',
  no_credential: 'This account never set a password, so it cannot be enabled. Send a new invite instead.',
  cross_site_refused: 'The registry refused a change sent from another site. Reload the console and try again.',
}

/** What went wrong, in one plain sentence. */
export function usersErrorText(err: unknown): string {
  const code = refusalCode(err)
  if (code && PLAIN[code]) return PLAIN[code]
  if (err instanceof ApiError) {
    if (code === null && typeof err.body === 'object' && err.body && 'detail' in err.body) {
      const detail = (err.body as { detail?: unknown }).detail
      if (typeof detail === 'string' && detail.includes('service_principal_refused')) {
        return 'A service credential cannot manage accounts. Sign in as an admin.'
      }
    }
    if (err.status === 401) return 'Your session has ended. Sign in again.'
    if (err.status === 403) return 'Managing accounts needs an admin.'
    if (err.status === 422) return 'The form is incomplete.'
    if (err.status >= 500) return 'The registry could not answer. Try again in a moment.'
    return `The registry refused the change (${err.status}).`
  }
  return 'The request did not reach the registry. Check the connection and try again.'
}
