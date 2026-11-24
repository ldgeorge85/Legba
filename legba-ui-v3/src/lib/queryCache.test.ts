/**
 * X4 READPATH (W11-F71) — the api layer's request dedupe + 30 s TTL.
 *
 * Driven through the REAL `apiGet` with `fetch` mocked, so the test proves the
 * path every panel takes, not the cache in isolation.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiGet, apiPost } from './api'
import {
  GET_TTL_MS,
  POLL_GRACE_MS,
  cachedGet,
  cachedGetCount,
  dropCachedGets,
  isCachedGet,
  ttlFor,
} from './queryCache'

function okJson(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as unknown as Response
}

/** A fetch whose answers resolve only when the test says so. */
function deferredFetch() {
  const pending: Array<(r: Response) => void> = []
  const fn = vi.fn(() => new Promise<Response>((resolve) => pending.push(resolve)))
  return { fn, release: (body: unknown) => pending.shift()!(okJson(body)) }
}

describe('queryCache through apiGet', () => {
  beforeEach(() => dropCachedGets())
  afterEach(() => vi.restoreAllMocks())

  it('three callers asking the same /findings params while it is in flight → ONE request', async () => {
    const f = deferredFetch()
    vi.stubGlobal('fetch', f.fn)
    const path = '/findings?analyst_id=country_composition&limit=200'
    // The read, the Wall and the feed, each under its own react-query key.
    const read = apiGet<{ data: string[] }>(path)
    const wall = apiGet<{ data: string[] }>(path)
    const feed = apiGet<{ data: string[] }>(path)
    expect(f.fn).toHaveBeenCalledTimes(1)
    f.release({ data: ['a', 'b'] })
    const [a, b, c] = await Promise.all([read, wall, feed])
    expect(a).toEqual({ data: ['a', 'b'] })
    expect(b).toBe(a)
    expect(c).toBe(a)
    expect(f.fn).toHaveBeenCalledTimes(1)
    vi.unstubAllGlobals()
  })

  it('different params are different requests', async () => {
    const fetchMock = vi.fn(async () => okJson({ data: [] }))
    vi.stubGlobal('fetch', fetchMock)
    await Promise.all([apiGet('/findings?limit=50'), apiGet('/findings?limit=500')])
    expect(fetchMock).toHaveBeenCalledTimes(2)
    vi.unstubAllGlobals()
  })

  it('a settled answer is reused inside the TTL and re-asked after it', async () => {
    let t = 1_000_000
    const now = () => t
    const fetcher = vi.fn(async () => ({ n: fetcher.mock.calls.length }))
    const first = await cachedGet('/findings?limit=50', fetcher, now)
    t += GET_TTL_MS - POLL_GRACE_MS - 1
    expect(await cachedGet('/findings?limit=50', fetcher, now)).toBe(first)
    expect(fetcher).toHaveBeenCalledTimes(1)
    // A poll on exactly the TTL (the Live Feed's 30 s) is never served its own
    // previous answer, even a millisecond early.
    t += 1
    const second = await cachedGet('/findings?limit=50', fetcher, now)
    expect(second).not.toBe(first)
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('a failure is shared by its in-flight callers but never cached', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 503, text: async () => 'busy' })
      .mockResolvedValueOnce(okJson({ data: ['ok'] }))
    vi.stubGlobal('fetch', fetchMock)
    const p1 = apiGet('/findings?limit=5')
    const p2 = apiGet('/findings?limit=5')
    await expect(p1).rejects.toMatchObject({ status: 503 })
    await expect(p2).rejects.toMatchObject({ status: 503 })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(await apiGet('/findings?limit=5')).toEqual({ data: ['ok'] })
    expect(fetchMock).toHaveBeenCalledTimes(2)
    vi.unstubAllGlobals()
  })

  it('U-5: the TTL routes are the slow-changing landing reads; other routes keep nothing once settled', async () => {
    expect(isCachedGet('/findings')).toBe(true)
    expect(isCachedGet('/findings?since=x&fields=judgment')).toBe(true)
    expect(isCachedGet('/signals?limit=500')).toBe(true)
    expect(isCachedGet('/registry/descriptors?family=target&head_only=true&limit=500')).toBe(true)
    expect(isCachedGet('/v3/system/source-firing')).toBe(true)
    expect(ttlFor('/registry/projection/geopolitics/bands')).toBe(5 * 60_000)
    expect(isCachedGet('/findings/abc')).toBe(false)
    expect(isCachedGet('/v3/absence?scope=x')).toBe(false)
    // a settled answer on an in-flight-only route is asked again
    const fetchMock = vi.fn(async () => okJson({ data: [] }))
    vi.stubGlobal('fetch', fetchMock)
    await apiGet('/v3/absence?scope=x')
    await apiGet('/v3/absence?scope=x')
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(cachedGetCount()).toBe(0)
    vi.unstubAllGlobals()
  })

  it('U-5: EVERY route shares a request in flight — two panels, one request', async () => {
    const f = deferredFetch()
    vi.stubGlobal('fetch', f.fn)
    const a = apiGet('/v3/absence?scope=country_zz')
    const b = apiGet('/v3/absence?scope=country_zz')
    expect(f.fn).toHaveBeenCalledTimes(1)
    f.release({ items: [] })
    expect(await b).toBe(await a)
    vi.unstubAllGlobals()
  })

  it('U-5: the sign-in routes are never shared, not even in flight', async () => {
    const fetchMock = vi.fn(async () => okJson({ kind: 'operator', role: 'operator' }))
    vi.stubGlobal('fetch', fetchMock)
    await Promise.all([apiGet('/auth/me'), apiGet('/auth/me')])
    expect(fetchMock).toHaveBeenCalledTimes(2)
    vi.unstubAllGlobals()
  })

  it('U-5: nothing defeats the HTTP cache — the path is sent as asked, with no no-store', async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => okJson({ data: [] }))
    vi.stubGlobal('fetch', fetchMock)
    await apiGet('/signals?limit=500')
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/signals?limit=500')
    expect(init?.cache).toBeUndefined()
    vi.unstubAllGlobals()
  })

  it('a write drops the cache, so the next read is fresh', async () => {
    const fetchMock = vi.fn(async () => okJson({ data: [] }))
    vi.stubGlobal('fetch', fetchMock)
    await apiGet('/findings?limit=50')
    expect(cachedGetCount()).toBe(1)
    await apiPost('/watchlist', { id: 'x' })
    expect(cachedGetCount()).toBe(0)
    await apiGet('/findings?limit=50')
    // GET, POST, GET — the second GET was not served from before the write.
    expect(fetchMock).toHaveBeenCalledTimes(3)
    vi.unstubAllGlobals()
  })

  it('an expired answer to a question nobody repeats is swept on the next call', async () => {
    let t = 5_000_000
    const now = () => t
    const fetcher = vi.fn(async () => ({}))
    await cachedGet('/findings?target_id=country_g20_br', fetcher, now)
    expect(cachedGetCount()).toBe(1)
    t += GET_TTL_MS
    await cachedGet('/findings?limit=50', fetcher, now)
    // Only the live question is held; the desk clicked once is gone.
    expect(cachedGetCount()).toBe(1)
  })

  it('dropCachedGets(prefix) drops only the matching paths', async () => {
    const fetcher = vi.fn(async () => ({}))
    await cachedGet('/findings?limit=1', fetcher)
    await cachedGet('/findings?limit=2', fetcher)
    dropCachedGets('/findings?limit=1')
    expect(cachedGetCount()).toBe(1)
    dropCachedGets('/findings')
    expect(cachedGetCount()).toBe(0)
  })
})
