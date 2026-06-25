/**
 * Tests for mode resolution + token decoding.
 *
 * Mode-conditional rendering is load-bearing for L-204; the resolver
 * MUST honor the priority order:
 *   1. ?mode= URL query
 *   2. JWT mode claim
 *   3. VITE env default
 *   4. 'personal' fallback
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { currentMode, isMode, setToken, clearToken, tryDecodeClaims } from './jwt'

function setWindowMode(mode: string | null) {
  const url = new URL('http://localhost:5174/')
  if (mode) url.searchParams.set('mode', mode)
  Object.defineProperty(window, 'location', {
    value: { ...window.location, href: url.toString(), search: url.search },
    writable: true,
  })
}

beforeEach(() => {
  clearToken()
  vi.unstubAllEnvs()
})

describe('isMode', () => {
  it('accepts personal/above_ai/cis', () => {
    expect(isMode('personal')).toBe(true)
    expect(isMode('above_ai')).toBe(true)
    expect(isMode('cis')).toBe(true)
  })
  it('rejects aliases and unknowns', () => {
    expect(isMode('above-ai')).toBe(false)
    expect(isMode('cis_fellowship')).toBe(false)
    expect(isMode('')).toBe(false)
    expect(isMode(null)).toBe(false)
  })
})

describe('currentMode resolution priority', () => {
  it('falls back to personal when nothing is set', () => {
    setWindowMode(null)
    expect(currentMode()).toBe('personal')
  })

  it('honors ?mode= URL param above everything', () => {
    setWindowMode('cis')
    expect(currentMode()).toBe('cis')
  })

  it('reads mode from a JWT claim when no URL override', () => {
    setWindowMode(null)
    // Hand-craft a minimal JWT: header.payload.signature, all b64url-encoded
    const payload = btoa(
      JSON.stringify({ sub: 'u1', mode: 'above_ai', roles: ['analyst'], exp: 9e9 }),
    )
      .replace(/=+$/, '')
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
    const fake = `eyJ.${payload}.sig`
    setToken(fake)
    expect(currentMode()).toBe('above_ai')
  })

  it('ignores unknown mode strings in JWT', () => {
    setWindowMode(null)
    const payload = btoa(
      JSON.stringify({ sub: 'u1', mode: 'super_user', roles: [], exp: 9e9 }),
    )
      .replace(/=+$/, '')
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
    setToken(`eyJ.${payload}.sig`)
    expect(currentMode()).toBe('personal') // falls through
  })
})

describe('tryDecodeClaims', () => {
  it('returns null for empty token', () => {
    expect(tryDecodeClaims(null)).toBeNull()
    expect(tryDecodeClaims('')).toBeNull()
  })
  it('returns null for non-JWT shape', () => {
    expect(tryDecodeClaims('not.a.jwt.token')).toBeNull()
    expect(tryDecodeClaims('flat')).toBeNull()
  })
  it('returns null for unparseable payload', () => {
    expect(tryDecodeClaims('eyJ.notbase64.sig')).toBeNull()
  })
})
