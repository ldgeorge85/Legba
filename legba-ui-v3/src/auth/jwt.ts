/**
 * JWT integration with the legba-registry bearer-token surface.
 *
 * Today the registry uses a single shared bearer token from
 * `LEGBA_REGISTRY_API_TOKEN`; in dev mode any token (or none) is accepted.
 * The future DID-bearer + OAuth2 surface (per L-113 brief) lands later.
 *
 * For L-204 we keep the contract minimal:
 *  - `setToken(t)` writes the bearer to localStorage.
 *  - `clearToken()` clears it.
 *  - `currentMode()` returns the active deployment mode (URL `?mode=` >
 *    parsed JWT claim > VITE env default > 'personal').
 *
 * The JWT is treated as opaque on the wire; if it's a real JWT we
 * best-effort decode `mode` + `roles` from the payload, but the registry
 * is the source of truth, not the client.
 */

import type { AuthClaims, Mode } from '@/types'

const TOKEN_KEY = 'legba_token'

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

/** Best-effort JWT payload decoder. Returns null for non-JWT / malformed. */
export function tryDecodeClaims(token: string | null): AuthClaims | null {
  if (!token) return null
  const parts = token.split('.')
  if (parts.length !== 3) return null
  try {
    const payload = atob(parts[1].replace(/-/g, '+').replace(/_/g, '/'))
    const parsed = JSON.parse(payload) as Partial<AuthClaims>
    if (!parsed.sub || !parsed.mode) return null
    return {
      sub: parsed.sub,
      mode: parsed.mode,
      roles: parsed.roles ?? ['viewer'],
      exp: parsed.exp ?? 0,
    }
  } catch {
    return null
  }
}

/**
 * Resolve the current deployment mode.
 *
 * Priority (highest first):
 *   1. `?mode=` URL query param (operator override for testing).
 *   2. JWT claim from the stored token.
 *   3. `VITE_LEGBA_DEFAULT_MODE` env at build time.
 *   4. 'personal' (the daily-driver default).
 */
export function currentMode(): Mode {
  const url = new URL(window.location.href)
  const fromUrl = url.searchParams.get('mode')
  if (fromUrl && isMode(fromUrl)) return fromUrl

  const claims = tryDecodeClaims(getToken())
  if (claims && isMode(claims.mode)) return claims.mode

  const fromEnv = (import.meta.env.VITE_LEGBA_DEFAULT_MODE as string | undefined) ?? ''
  if (isMode(fromEnv)) return fromEnv

  return 'personal'
}

export function isMode(s: string | null | undefined): s is Mode {
  return s === 'personal' || s === 'above_ai' || s === 'cis'
}

/** Whether the current user has the operator/admin role for gated panels. */
export function isOperator(): boolean {
  const claims = tryDecodeClaims(getToken())
  if (!claims) return true // dev-mode default — registry isn't enforcing yet
  return claims.roles.includes('admin') || claims.roles.includes('operator')
}
