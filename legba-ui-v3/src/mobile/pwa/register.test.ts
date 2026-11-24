/**
 * P-1 — the update flow: a new version waits, the page says so, and the
 * person's tap (and only that) hands over and reloads.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { __resetPwaForTests, applyUpdate, getPwaState, MSG_SKIP_WAITING, registerPhoneWorker, SW_SCOPE, SW_URL, watchRegistration } from './register'

class FakeWorker extends EventTarget {
  state = 'installing'
  postMessage = vi.fn()
  setState(s: string) {
    this.state = s
    this.dispatchEvent(new Event('statechange'))
  }
}

function fakeContainer(controller: unknown) {
  const c = new EventTarget() as EventTarget & { controller: unknown; register: ReturnType<typeof vi.fn> }
  c.controller = controller
  return c
}

function fakeRegistration(waiting: FakeWorker | null = null) {
  const r = new EventTarget() as EventTarget & { waiting: FakeWorker | null; installing: FakeWorker | null; update: ReturnType<typeof vi.fn> }
  r.waiting = waiting
  r.installing = null
  r.update = vi.fn(async () => {})
  return r
}

afterEach(() => __resetPwaForTests())

describe('a new version waits for the person', () => {
  it('an update that installs while a version controls the page raises "update ready"', () => {
    const sw = fakeContainer({})
    const reg = fakeRegistration()
    watchRegistration(reg as unknown as ServiceWorkerRegistration, sw as unknown as ServiceWorkerContainer)
    expect(getPwaState().updateReady).toBe(false)
    const next = new FakeWorker()
    reg.installing = next
    reg.dispatchEvent(new Event('updatefound'))
    next.setState('installed')
    expect(getPwaState().updateReady).toBe(true)
  })

  it('a version already waiting at load raises it too', () => {
    watchRegistration(fakeRegistration(new FakeWorker()) as unknown as ServiceWorkerRegistration, fakeContainer({}) as unknown as ServiceWorkerContainer)
    expect(getPwaState().updateReady).toBe(true)
  })

  it('the FIRST install (nothing controls the page yet) is not an update', () => {
    const reg = fakeRegistration()
    watchRegistration(reg as unknown as ServiceWorkerRegistration, fakeContainer(null) as unknown as ServiceWorkerContainer)
    const first = new FakeWorker()
    reg.installing = first
    reg.dispatchEvent(new Event('updatefound'))
    first.setState('installed')
    expect(getPwaState().updateReady).toBe(false)
  })
})

describe('applying it', () => {
  it('Reload posts skip-waiting to the WAITING worker and reloads only once the new one controls the page', () => {
    const sw = fakeContainer({})
    const waiting = new FakeWorker()
    watchRegistration(fakeRegistration(waiting) as unknown as ServiceWorkerRegistration, sw as unknown as ServiceWorkerContainer)
    const reload = vi.fn()
    expect(applyUpdate(sw as unknown as ServiceWorkerContainer, reload)).toBe(true)
    expect(waiting.postMessage).toHaveBeenCalledWith({ type: MSG_SKIP_WAITING })
    expect(getPwaState().applying).toBe(true)
    expect(reload).not.toHaveBeenCalled()
    sw.dispatchEvent(new Event('controllerchange'))
    expect(reload).toHaveBeenCalledTimes(1)
    // a second tap does nothing more
    expect(applyUpdate(sw as unknown as ServiceWorkerContainer, reload)).toBe(false)
  })

  it('no silent reloads: with nothing waiting, Reload does nothing, and a controllerchange alone reloads nothing', () => {
    const sw = fakeContainer({})
    const reload = vi.fn()
    expect(applyUpdate(sw as unknown as ServiceWorkerContainer, reload)).toBe(false)
    sw.dispatchEvent(new Event('controllerchange'))
    expect(reload).not.toHaveBeenCalled()
  })
})

describe('registration', () => {
  it('registers /m/sw.js with scope /m/ and update checks that bypass the HTTP cache', async () => {
    const reg = fakeRegistration()
    const register = vi.fn(async () => reg)
    Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: Object.assign(fakeContainer(null), { register }) })
    try {
      expect(await registerPhoneWorker({ force: true })).toBe(reg)
      expect(register).toHaveBeenCalledWith(SW_URL, { scope: SW_SCOPE, updateViaCache: 'none' })
      expect(SW_URL).toBe('/m/sw.js')
      expect(SW_SCOPE).toBe('/m/')
      document.dispatchEvent(new Event('visibilitychange'))
      expect(reg.update).toHaveBeenCalled()
    } finally {
      delete (navigator as unknown as Record<string, unknown>).serviceWorker
    }
  })

  it('never throws: a refused registration leaves an online-only app', async () => {
    Object.defineProperty(navigator, 'serviceWorker', {
      configurable: true,
      value: Object.assign(fakeContainer(null), { register: vi.fn(async () => { throw new Error('401') }) }),
    })
    try {
      expect(await registerPhoneWorker({ force: true })).toBeNull()
    } finally {
      delete (navigator as unknown as Record<string, unknown>).serviceWorker
    }
  })

  it('offline, register() fails but the registration from an earlier visit is still watched', async () => {
    const reg = fakeRegistration(new FakeWorker())
    Object.defineProperty(navigator, 'serviceWorker', {
      configurable: true,
      value: Object.assign(fakeContainer({}), {
        register: vi.fn(async () => { throw new TypeError('Failed to fetch') }),
        getRegistration: vi.fn(async () => reg),
      }),
    })
    try {
      expect(await registerPhoneWorker({ force: true })).toBe(reg)
      expect(getPwaState().updateReady).toBe(true)
    } finally {
      delete (navigator as unknown as Record<string, unknown>).serviceWorker
    }
  })

  it('Reload hands over to the worker waiting NOW, not a stale one seen earlier', () => {
    const sw = fakeContainer({})
    const first = new FakeWorker()
    const reg = fakeRegistration(first)
    watchRegistration(reg as unknown as ServiceWorkerRegistration, sw as unknown as ServiceWorkerContainer)
    const second = new FakeWorker()
    reg.waiting = second
    expect(applyUpdate(sw as unknown as ServiceWorkerContainer, vi.fn())).toBe(true)
    expect(second.postMessage).toHaveBeenCalledWith({ type: MSG_SKIP_WAITING })
    expect(first.postMessage).not.toHaveBeenCalled()
  })

  it('does nothing in a dev or test build unless forced', async () => {
    expect(await registerPhoneWorker()).toBeNull()
  })
})
