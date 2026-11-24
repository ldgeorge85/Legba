/**
 * P-1 — sign-out on the phone drops the app's worker and caches and asks the
 * registry for Clear-Site-Data; sign-out on the desktop is unchanged.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CLEAR_DEVICE_HEADER, onPhoneSurface } from './clearPhoneDevice'
import { signOut } from './session'

function atPath(p: string) {
  window.history.replaceState(null, '', p)
}

function fakeDevice() {
  const active = { postMessage: vi.fn() }
  const reg = { active, waiting: null, installing: null, unregister: vi.fn(async () => true) }
  const getRegistration = vi.fn(async () => reg)
  Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: { getRegistration } })
  const names = new Set(['legba-m-shell-0123', 'legba-m-shell-old', 'not-ours'])
  const caches = { keys: vi.fn(async () => [...names]), delete: vi.fn(async (n: string) => names.delete(n)) }
  vi.stubGlobal('caches', caches)
  return { active, reg, getRegistration, names }
}

afterEach(() => {
  vi.unstubAllGlobals()
  delete (navigator as unknown as Record<string, unknown>).serviceWorker
  atPath('/')
})

describe('onPhoneSurface', () => {
  it('is /m and /m/… only', () => {
    expect(onPhoneSurface('/m')).toBe(true)
    expect(onPhoneSurface('/m/')).toBe(true)
    expect(onPhoneSurface('/m/x')).toBe(true)
    expect(onPhoneSurface('/')).toBe(false)
    expect(onPhoneSurface('/mobile')).toBe(false)
  })
})

describe('signOut', () => {
  it('on the phone: asks for Clear-Site-Data, tells the worker to drop its caches, deletes them, unregisters', async () => {
    atPath('/m/')
    const dev = fakeDevice()
    const fetchMock = vi.fn(async () => new Response('{"signed_out":true}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await signOut()
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/v1/auth/sign-out')
    expect((init.headers as Record<string, string>)[CLEAR_DEVICE_HEADER]).toBe('phone')
    expect(dev.getRegistration).toHaveBeenCalledWith('/m/')
    expect(dev.active.postMessage).toHaveBeenCalledWith({ type: 'LEGBA_SW_CLEAR' })
    expect(dev.reg.unregister).toHaveBeenCalled()
    expect([...dev.names]).toEqual(['not-ours'])
  })

  it('on the phone, even a refused sign-out request still clears the device', async () => {
    atPath('/m/')
    const dev = fakeDevice()
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{"detail":"x"}', { status: 500 })))
    await expect(signOut()).rejects.toBeTruthy()
    expect([...dev.names]).toEqual(['not-ours'])
  })

  it('on the desktop: no header, nothing on the device touched', async () => {
    atPath('/')
    const dev = fakeDevice()
    const fetchMock = vi.fn(async () => new Response('{"signed_out":true}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await signOut()
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(init.headers).toEqual({ Accept: 'application/json' })
    expect(dev.getRegistration).not.toHaveBeenCalled()
    expect(dev.names.size).toBe(3)
  })
})
