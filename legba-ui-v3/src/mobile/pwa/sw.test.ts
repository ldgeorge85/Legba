// @vitest-environment node
/**
 * P-1 — the phone app's service worker, run as written.
 *
 * The REAL `sw.template.js` is filled by the REAL build helper and evaluated
 * against a fake worker global (self, caches, fetch) that records every cache
 * write. The rule under test above all: no API answer, evidence, record text
 * or Consult answer is ever stored — the Cache API ignores Cache-Control, so
 * the refusal must be the worker's own.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { renderServiceWorker } from './swBuild'

const TEMPLATE = readFileSync(join(__dirname, 'sw.template.js'), 'utf8')
const ORIGIN = 'https://legba.test'
const SHELL = [
  '/m/index.html',
  '/m/assets/index-abc123.js',
  '/m/assets/index-def456.css',
  '/m/favicon.svg',
  '/m/icons/icon-192.png',
  '/m/manifest.webmanifest',
]

/** Everything a phone ever asks for that carries data. None of it may be stored. */
const DATA_URLS = [
  '/api/v1/findings?fields=summary&limit=50',
  '/api/v1/findings/7d1c',
  '/api/v1/auth/me',
  '/api/v1/consult',
  '/api/v1/consult/stream/abc',
  '/api/v1/v3/record/evidence/123',
  '/api/v1/v3/record/text/123',
  '/api/v1/lineage/finding/1',
  '/api/v1/journal?limit=10',
  '/api',
  '/ws/events',
  '/tiles/world.pmtiles',
  '/m/assets/lazy-map-999.js', // a lazy chunk: code, but not the shell — fetched, never stored
  '/m/index.html?report=1',
]

interface Fake {
  self: Record<string, unknown> & { __legbaSw: { routeFor: (u: string, m: string, mode: string) => string; assertCacheable: (p: string) => void; CACHE: string } }
  fire: (type: string, ev: Record<string, unknown>) => Promise<unknown>
  writes: string[]
  store: Map<string, Map<string, Response>>
  fetchLog: string[]
  setOnline: (v: boolean) => void
  respond: (url: string, init?: { method?: string; mode?: string }) => Promise<Response | null>
}

function loadWorker(
  precache: string[] = SHELL,
  answer: (url: string) => Response = (url) =>
    new Response(`body of ${url}`, {
      status: 200,
      headers: { 'content-type': url.endsWith('.html') ? 'text/html' : url.endsWith('.css') ? 'text/css' : 'text/javascript' },
    }),
): Fake {
  const src = renderServiceWorker(TEMPLATE, '0123456789abcdef', precache)
  const listeners: Record<string, ((ev: unknown) => void)[]> = {}
  const store = new Map<string, Map<string, Response>>()
  const writes: string[] = []
  const fetchLog: string[] = []
  const nav = { onLine: true }
  const openCache = async (name: string) => {
    if (!store.has(name)) store.set(name, new Map())
    const m = store.get(name)!
    return {
      put: async (key: string, res: Response) => {
        writes.push(String(key))
        m.set(String(key), res)
      },
      add: async (key: string) => {
        throw new Error(`unexpected cache.add(${key})`)
      },
      addAll: async (keys: string[]) => {
        throw new Error(`unexpected cache.addAll(${keys})`)
      },
      match: async (key: string) => m.get(String(key)),
    }
  }
  const caches = {
    open: openCache,
    keys: async () => [...store.keys()],
    delete: async (name: string) => store.delete(name),
    match: async () => undefined,
  }
  const FakeRequest = function (this: Record<string, unknown>, url: string, init: Record<string, unknown> = {}) {
    Object.assign(this, init, { url: new URL(url, ORIGIN + '/m/').href })
  } as unknown as typeof Request
  const fetchImpl = vi.fn(async (req: string | { url: string }) => {
    const url = typeof req === 'string' ? new URL(req, ORIGIN).href : req.url
    fetchLog.push(new URL(url).pathname + new URL(url).search)
    if (!nav.onLine) throw new TypeError('Failed to fetch')
    return answer(new URL(url).pathname)
  })
  const self: Record<string, unknown> = {
    location: new URL(ORIGIN + '/m/sw.js'),
    navigator: nav,
    addEventListener: (type: string, fn: (ev: unknown) => void) => (listeners[type] ??= []).push(fn),
    skipWaiting: vi.fn(async () => {}),
    clients: { claim: vi.fn(async () => {}) },
  }
  new Function('self', 'caches', 'fetch', 'Request', src)(self, caches, fetchImpl, FakeRequest)

  const fire = async (type: string, ev: Record<string, unknown>) => {
    const waits: Promise<unknown>[] = []
    const e = { ...ev, waitUntil: (p: Promise<unknown>) => waits.push(p) }
    for (const fn of listeners[type] ?? []) fn(e)
    return Promise.all(waits)
  }
  const respond = async (url: string, init: { method?: string; mode?: string } = {}) => {
    let answered: Promise<Response> | null = null
    const e = {
      request: { url: new URL(url, ORIGIN).href, method: init.method ?? 'GET', mode: init.mode ?? 'cors' },
      respondWith: (p: Promise<Response>) => {
        answered = p
      },
    }
    for (const fn of listeners.fetch ?? []) fn(e)
    return answered ? await answered : null
  }
  return {
    self: self as Fake['self'],
    fire,
    writes,
    store,
    fetchLog,
    setOnline: (v) => {
      nav.onLine = v
    },
    respond,
  }
}

describe('the routing rules', () => {
  const w = loadWorker()
  const route = (u: string, method = 'GET', mode = 'cors') => w.self.__legbaSw.routeFor(ORIGIN + u, method, mode)

  it('never answers anything under /api/, /ws/ or /tiles/ — in any mode, by any method', () => {
    for (const u of DATA_URLS.filter((x) => !x.startsWith('/m/'))) {
      for (const mode of ['cors', 'navigate', 'no-cors', 'same-origin']) {
        for (const method of ['GET', 'POST', 'HEAD']) expect(route(u, method, mode), `${method} ${u} ${mode}`).toBe('pass')
      }
    }
  })

  it('answers only inside its scope /m/: the desktop at / is never touched', () => {
    expect(route('/', 'GET', 'navigate')).toBe('pass')
    expect(route('/index.html')).toBe('pass')
    expect(route('/assets/index-abc123.js')).toBe('pass')
    expect(route('/favicon.svg')).toBe('pass')
    expect(route('/mobile/')).toBe('pass')
    expect(route('/m/', 'GET', 'navigate')).toBe('navigate')
    expect(route('/m/?report=abc', 'GET', 'navigate')).toBe('navigate')
  })

  it('serves from the shell only the files the build listed, exactly', () => {
    for (const p of SHELL) expect(route(p)).toBe('shell')
    expect(route('/m/assets/lazy-map-999.js')).toBe('pass')
    expect(route('/m/assets/index-abc123.js?v=2')).toBe('pass')
    expect(route('/m/assets/index-abc123.js', 'POST')).toBe('pass')
  })

  it('never answers another origin', () => {
    expect(w.self.__legbaSw.routeFor('https://elsewhere.test/m/assets/index-abc123.js', 'GET', 'cors')).toBe('pass')
  })

  it('refuses to cache anything outside the shell list', () => {
    const { assertCacheable } = w.self.__legbaSw
    for (const u of DATA_URLS) expect(() => assertCacheable(u), u).toThrow(/refusing to cache/)
    expect(() => assertCacheable('https://elsewhere.test/m/index.html')).toThrow(/refusing to cache/)
    expect(() => assertCacheable('/m/index.html')).not.toThrow()
  })
})

describe('install, fetch and activate', () => {
  it('install stores exactly the shell list and nothing else, and does not skip waiting', async () => {
    const w = loadWorker()
    await w.fire('install', {})
    expect(w.writes).toEqual(SHELL)
    expect(w.self.skipWaiting).not.toHaveBeenCalled()
    expect([...w.store.keys()]).toEqual(['legba-m-shell-0123456789abcdef'])
  })

  it('THE RULE: after install, no request of any kind adds anything to any cache, and no /api/ response is ever stored', async () => {
    const w = loadWorker()
    await w.fire('install', {})
    const before = w.writes.length
    for (const online of [true, false]) {
      w.setOnline(online)
      for (const u of [...DATA_URLS, ...SHELL, '/m/', '/']) {
        for (const mode of ['cors', 'navigate']) {
          for (const method of ['GET', 'POST']) {
            const res = await w.respond(u, { method, mode }).catch(() => null)
            if (u.startsWith('/api') || u.startsWith('/ws') || u.startsWith('/tiles')) expect(res, `${method} ${u} ${mode}`).toBeNull()
          }
        }
      }
    }
    expect(w.writes.length).toBe(before)
    for (const cache of w.store.values()) for (const key of cache.keys()) expect(key).not.toMatch(/\/api\/|\/ws\/|\/tiles\//)
  })

  it('a build that listed an /api/ path cannot install: the refusal fires before any write of it', async () => {
    const w = loadWorker(['/m/index.html', '/api/v1/findings'])
    await expect(w.fire('install', {})).rejects.toThrow(/refusing to cache \/api\/v1\/findings/)
    expect(w.writes).toEqual(['/m/index.html'])
    expect(w.fetchLog).not.toContain('/api/v1/findings')
  })

  it('install refuses a 401 (the sign-in wall) or an HTML page standing in for a script', async () => {
    const w401 = loadWorker(SHELL, (u) => new Response('no', { status: u.endsWith('.js') ? 401 : 200, headers: { 'content-type': 'text/html' } }))
    await expect(w401.fire('install', {})).rejects.toThrow(/answered 401/)
    const wHtml = loadWorker(SHELL, () => new Response('<!doctype html>', { status: 200, headers: { 'content-type': 'text/html' } }))
    await expect(wHtml.fire('install', {})).rejects.toThrow(/answered an HTML page/)
  })

  it('a shell file is answered from the cache, with no network', async () => {
    const w = loadWorker()
    await w.fire('install', {})
    w.fetchLog.length = 0
    w.setOnline(false)
    const res = await w.respond('/m/assets/index-abc123.js')
    expect(await res!.text()).toBe('body of /m/assets/index-abc123.js')
    expect(w.fetchLog).toEqual([])
  })

  it('online, a navigation is left to the browser (a perimeter prompt still works); offline it gets the cached shell page', async () => {
    const w = loadWorker()
    await w.fire('install', {})
    expect(await w.respond('/m/', { mode: 'navigate' })).toBeNull()
    w.setOnline(false)
    const page = await w.respond('/m/?report=abc', { mode: 'navigate' })
    expect(await page!.text()).toBe('body of /m/index.html')
  })

  it('activate drops older versions of its own caches, keeps anything else, and claims the page', async () => {
    const w = loadWorker()
    w.store.set('legba-m-shell-old0000000000000', new Map())
    w.store.set('someone-else', new Map())
    await w.fire('install', {})
    await w.fire('activate', {})
    expect([...w.store.keys()].sort()).toEqual(['legba-m-shell-0123456789abcdef', 'someone-else'])
    expect((w.self.clients as { claim: () => void }).claim).toHaveBeenCalled()
  })
})

describe('messages from the page', () => {
  it('LEGBA_SW_SKIP_WAITING (the person tapped Reload) is the only way a new version takes over', async () => {
    const w = loadWorker()
    await w.fire('install', {})
    await w.fire('activate', {})
    expect(w.self.skipWaiting).not.toHaveBeenCalled()
    await w.fire('message', { data: { type: 'something-else' } })
    expect(w.self.skipWaiting).not.toHaveBeenCalled()
    await w.fire('message', { data: { type: 'LEGBA_SW_SKIP_WAITING' } })
    expect(w.self.skipWaiting).toHaveBeenCalledTimes(1)
  })

  it('LEGBA_SW_CLEAR (sign-out) deletes every cache it made and says so', async () => {
    const w = loadWorker()
    await w.fire('install', {})
    w.store.set('legba-m-shell-old0000000000000', new Map())
    const postMessage = vi.fn()
    await w.fire('message', { data: { type: 'LEGBA_SW_CLEAR' }, source: { postMessage } })
    expect([...w.store.keys()]).toEqual([])
    expect(postMessage).toHaveBeenCalledWith({ type: 'LEGBA_SW_CLEARED' })
  })
})

describe('the source', () => {
  it('loads nothing from elsewhere and never skips waiting on its own', () => {
    expect(TEMPLATE).not.toMatch(/importScripts\s*\(/)
    expect(TEMPLATE).not.toMatch(/https?:\/\//)
    // skipWaiting appears exactly once, inside the message handler
    expect(TEMPLATE.match(/self\.skipWaiting\(\)/g)).toHaveLength(1)
    expect(TEMPLATE).not.toMatch(/pushManager|addEventListener\('push'/)
  })
})
