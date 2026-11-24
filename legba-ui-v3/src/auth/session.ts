/**
 * X3-B3 — who the registry thinks is calling, and the four sign-in calls.
 *
 * The identity is the registry's, never the client's: `GET /api/v1/auth/me`
 * answers for whatever credential reached it —
 *
 *   - `user`      a signed-in person (the `legba_session` cookie; HttpOnly, so
 *                 this code never sees it — the browser attaches it);
 *   - `operator`  the operator's bearer (the caddy perimeter injects it for a
 *                 browser that has no session cookie — "mode A");
 *   - `service`   the fleet's token (never a browser, listed for completeness);
 *   - `anonymous` a dev registry with no token configured.
 *
 * A 401 means "no credential the registry accepts" — the sign-in screen. Any
 * other failure is NOT a sign-out: the gate lets the shell render as before
 * (the registry still enforces every route; the gate is only the door).
 */

import { ApiError, apiGet, apiPost, readErrorBody } from '@/lib/api'
import { CLEAR_DEVICE_HEADER, clearPhoneDevice, onPhoneSurface } from '@/auth/clearPhoneDevice'

/**
 * A-1 — with the registry's sign-in switch on (`LEGBA_AUTH_SIGNIN=1`) the
 * answer carries `signin: true`, the roles are `reader` / `contributor` /
 * `admin`, `is_owner` marks the owner, and the non-person kinds are
 * `service`, `admin-token` (the break-glass credential) and `dev`. The edge
 * then hands a browser no credential at all: no cookie → 401 → sign-in.
 */
export type IdentityKind = 'user' | 'operator' | 'service' | 'anonymous' | 'admin-token' | 'dev'
export type RoleClass =
  | 'read'
  | 'reader-own-rows'
  | 'operator'
  | 'reader'
  | 'contributor'
  | 'admin'

export interface AuthUser {
  id: string
  actor: string
  email: string | null
  display_name: string
  role: RoleClass
  status: 'invited' | 'active' | 'disabled'
  auth_provider: string
  last_sign_in_at: string | null
  /** Present only with sign-in on. */
  is_owner?: boolean
}

export interface Identity {
  kind: IdentityKind
  actor: string
  role: RoleClass
  user: AuthUser | null
  session_rejected: boolean
  /** Present (true) only with the registry's sign-in switch on. */
  signin?: boolean
  is_owner?: boolean
  /** The service a service credential names (sign-in on). */
  service?: string | null
}

/** Whether the registry runs with sign-in required (A-1). */
export function signInRequired(identity: Identity): boolean {
  return identity.signin === true
}

export type IdentityState =
  | { state: 'signed_in'; identity: Identity }
  | { state: 'signed_out' }
  | { state: 'unavailable'; message: string }

export const IDENTITY_QUERY_KEY = ['auth', 'me'] as const

export async function fetchIdentity(): Promise<IdentityState> {
  try {
    const identity = await apiGet<Identity>('/auth/me')
    if (!identity || typeof identity.kind !== 'string' || typeof identity.role !== 'string') {
      return { state: 'unavailable', message: 'GET /auth/me answered an unknown shape' }
    }
    return { state: 'signed_in', identity }
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return { state: 'signed_out' }
    return { state: 'unavailable', message: err instanceof Error ? err.message : String(err) }
  }
}

/** The refusal the registry names (`detail.error` / `detail.message`), as text. */
export function refusalText(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body as { detail?: unknown } | null | undefined
    const detail = body && typeof body === 'object' ? body.detail : undefined
    if (detail && typeof detail === 'object' && 'message' in detail) {
      const message = (detail as { message?: unknown }).message
      if (typeof message === 'string' && message) return message
    }
    if (typeof detail === 'string' && detail) return detail
    if (err.status === 422) return 'the form is incomplete'
    return `the registry refused (${err.status})`
  }
  return err instanceof Error ? err.message : 'the request failed'
}

export async function signIn(email: string, password: string): Promise<AuthUser> {
  const res = await apiPost<{ user: AuthUser }>('/auth/sign-in', { email, password })
  return res.user
}

export async function acceptInvite(
  token: string,
  password: string,
  displayName: string,
): Promise<AuthUser> {
  const res = await apiPost<{ user: AuthUser }>('/auth/accept', {
    token,
    password,
    display_name: displayName.trim() || null,
  })
  return res.user
}

/**
 * Revoke this browser's session (the cookie is cleared by the response).
 *
 * P-1: on the phone (`/m/`) it also asks the registry for
 * `Clear-Site-Data: "storage"` and drops the phone app's worker and caches
 * (`clearPhoneDevice`) — even when the request fails, so a signed-out phone
 * keeps nothing. The desktop's sign-out is unchanged.
 */
export async function signOut(): Promise<void> {
  const phone = onPhoneSurface()
  try {
    const res = await fetch('/api/v1/auth/sign-out', {
      method: 'POST',
      headers: phone
        ? { Accept: 'application/json', [CLEAR_DEVICE_HEADER]: 'phone' }
        : { Accept: 'application/json' },
    })
    if (!res.ok) throw new ApiError(res.status, await readErrorBody(res))
  } finally {
    if (phone) await clearPhoneDevice()
  }
}

// ---------------------------------------------------------------------------
// The address: `#invite=<token>` (the operator's invite link — a FRAGMENT, so
// the token never reaches an access log) and `#signin` (ask for the screen).
// ---------------------------------------------------------------------------

export function inviteTokenFromHash(hash: string): string | null {
  const body = hash.startsWith('#') ? hash.slice(1) : hash
  for (const part of body.split('&')) {
    const [key, ...rest] = part.split('=')
    if (key === 'invite') {
      const value = decodeURIComponent(rest.join('='))
      return value || null
    }
  }
  return null
}

export function wantsSignIn(hash: string): boolean {
  const body = hash.startsWith('#') ? hash.slice(1) : hash
  return body.split('&').some((part) => part === 'signin')
}

/** The label a person is shown as: the name they gave, else their email. */
export function userLabel(user: AuthUser): string {
  return user.display_name.trim() || user.email || user.id.slice(0, 8)
}

/** Password floor — mirrors `legba.data.users.PASSWORD_MIN_CHARS`. */
export const PASSWORD_MIN_CHARS = 12
