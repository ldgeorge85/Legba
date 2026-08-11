/**
 * The events-WS credential must never appear in the URL.
 *
 * `?token=` carried LEGBA_REGISTRY_API_TOKEN — the admin credential for the
 * whole registry API, byte-identical — in the URL, which the browser prints
 * verbatim in console warnings on a failed upgrade and every proxy writes to
 * its access log. It now travels in the `legba.bearer.v1` subprotocol, which
 * is a header.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  WS_BEARER_SUBPROTOCOL,
  encodeBearerSubprotocol,
  registryEventsProtocols,
  registryEventsUrl,
  subscribeRegistryEvents,
} from './ws'

const TOKEN = 'super-secret-registry-token'

describe('registry events WS auth', () => {
  beforeEach(() => {
    localStorage.setItem('legba_token', TOKEN)
  })

  afterEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('builds a URL with NO credential in it', () => {
    const url = registryEventsUrl('descriptor.registered.>')
    expect(url).toContain('/api/v1/registry/events')
    expect(url).toContain('filter=descriptor.registered.%3E')
    expect(url).not.toContain('token')
    expect(url).not.toContain(TOKEN)
  })

  it('offers the bearer subprotocol instead', () => {
    const protocols = registryEventsProtocols(TOKEN)
    expect(protocols[0]).toBe(WS_BEARER_SUBPROTOCOL)
    expect(protocols).toHaveLength(2)
    // base64url, unpadded — the subprotocol grammar allows no '=', '+' or '/'.
    expect(protocols[1]).not.toMatch(/[=+/]/)
    expect(atob(protocols[1].replace(/-/g, '+').replace(/_/g, '/'))).toBe(TOKEN)
  })

  it('encodes credentials that are illegal as a raw subprotocol token', () => {
    for (const raw of ['a,b c=d/e+f', 'x'.repeat(300)]) {
      const encoded = encodeBearerSubprotocol(raw)
      expect(encoded).not.toMatch(/[=+/,\s]/)
    }
  })

  it('offers nothing when no token is stored', () => {
    expect(registryEventsProtocols('')).toEqual([])
  })

  it('connects with the URL and protocols it built — token in neither URL', () => {
    const seen: { url: string; protocols?: string | string[] }[] = []

    class CapturingWS {
      onmessage: ((ev: MessageEvent) => void) | null = null
      onerror: ((ev: Event) => void) | null = null
      onclose: (() => void) | null = null
      constructor(url: string, protocols?: string | string[]) {
        seen.push({ url, protocols })
      }
      close() {}
    }
    vi.stubGlobal('WebSocket', CapturingWS)

    const sub = subscribeRegistryEvents('>', () => {})
    sub.close()

    expect(seen).toHaveLength(1)
    const [call] = seen
    expect(call.url).not.toContain(TOKEN)
    expect(call.url).not.toContain('token=')
    expect(call.protocols).toEqual([
      WS_BEARER_SUBPROTOCOL,
      encodeBearerSubprotocol(TOKEN),
    ])
  })
})
