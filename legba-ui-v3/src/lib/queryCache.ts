/**
 * queryCache — ONE request per identical read-path GET (X4 READPATH, W11-F71;
 * widened by U-5, the landing fetch diet).
 *
 * The landing's time to content was registry-bound, not tile-bound: in the
 * first 20 s the landing's panels asked `/findings` 14 times over 11 distinct
 * parameter sets (the GET-only recipe, 2026-10-13) — three of them
 * byte-identical requests for 14 MB
 * (`?analyst_id=country_composition&limit=200`), 18 MB (`?limit=500`) and
 * 3 MB (`?analyst_id=world_assessor&limit=12`) pages, issued by different
 * panels under different react-query keys, so react-query's own per-key
 * dedupe never saw them as one. U-5 (2026-11-13) found the same pattern on
 * every other landing route: `/signals?limit=500` asked three times (the map,
 * the time band, the Wall's strip), `/registry/descriptors?family=target`
 * three times (the shell's registry read, the subject index, the palette),
 * `/v3/system/source-firing` and `/v3/events?limit=500` twice each.
 *
 * This module is keyed on the REQUEST (path + query, exactly as asked), not on
 * a panel's query key:
 *
 *   * IN FLIGHT — EVERY GET (but the sign-in routes): a second caller for the
 *     same path while the first request is still open gets the SAME promise.
 *     One fetch, N consumers. Sharing an open request can never serve an
 *     answer older than the one the second caller would have got.
 *   * TTL — on the routes in {@link ROUTE_TTL_MS} only, a successful answer is
 *     reused for that long from the moment its request STARTED (less
 *     {@link POLL_GRACE_MS}, so a panel polling on exactly the TTL — the Live
 *     Feed's 30 s — always gets a fresh answer and never its own previous
 *     one). Every other route keeps no answer once it settles.
 *   * ERRORS ARE NOT CACHED — callers sharing an in-flight request share its
 *     rejection; the next call asks again.
 *   * WRITES DROP IT — every non-GET in `lib/api.ts` clears the cache, so a
 *     read after a write is never served from before it. An explicit refresh
 *     (every panel's Refresh button, `components/PanelChrome`) calls
 *     {@link dropCachedGets} before it refetches.
 *   * NOTHING DEFEATS THE HTTP CACHE — the request is sent exactly as asked:
 *     no cache-busting parameter, no `cache: 'no-store'`, so the registry's
 *     `ETag` + `Cache-Control: private, max-age=5` on the landing's read
 *     routes still answers a revalidation with a 304.
 *
 * SHARED VALUES ARE READ-ONLY. Consumers of one request receive the same
 * parsed object — the contract react-query already imposes on two panels that
 * share a key. Every consumer of a TTL route copies before sorting (audited at
 * X4 for `/findings`, at U-5 for the routes U-5 added); a consumer that mutates
 * a response in place is a bug whether or not this cache exists.
 */

/** How long a successful `/findings` answer is reused, from its request's start. */
export const GET_TTL_MS = 30_000
/** Reuse stops this much before the TTL, so an exact-TTL poll is fresh. */
export const POLL_GRACE_MS = 1_000

/**
 * Route paths (the part before `?`) whose settled answers are reused, and for
 * how long (ms). Slow-changing reads the landing asks from several places;
 * the profile projection changes only with a profile export.
 */
export const ROUTE_TTL_MS: Readonly<Record<string, number>> = {
  '/findings': GET_TTL_MS,
  '/signals': GET_TTL_MS,
  '/situations': GET_TTL_MS,
  '/v3/events': GET_TTL_MS,
  '/registry/descriptors': GET_TTL_MS,
  '/registry/sources': GET_TTL_MS,
  '/registry/ui_panels': GET_TTL_MS,
  '/v3/system/source-firing': GET_TTL_MS,
  '/v3/system/analyst-cadence': GET_TTL_MS,
  '/v3/eval/country_scorecard': GET_TTL_MS,
  '/v3/eval/grader_roster': GET_TTL_MS,
  '/v3/system/judge-stats': GET_TTL_MS,
}

/** Route PREFIXES whose answers are reused (the projection: one row per section). */
const PREFIX_TTL_MS: ReadonlyArray<readonly [string, number]> = [['/registry/projection/', 5 * 60_000]]

/** Back-compat name: the routes whose settled answers are reused. */
export const CACHED_ROUTES: ReadonlySet<string> = new Set(Object.keys(ROUTE_TTL_MS))

/** Never shared, not even in flight: who the caller is must be asked fresh. */
const UNSHARED_PREFIXES = ['/auth/']

interface Entry {
  promise: Promise<unknown>
  startedAt: number
  settled: boolean
  ttl: number
}

const entries = new Map<string, Entry>()

const reusable = (e: Entry, at: number) => at - e.startedAt < e.ttl - POLL_GRACE_MS

/** Drop every SETTLED entry past its reuse window, so answers to questions
 *  nobody asks again (a desk clicked once) do not stay in memory. */
function sweep(at: number): void {
  for (const [key, e] of entries) {
    if (e.settled && !reusable(e, at)) entries.delete(key)
  }
}

/** The route part of an api path (`/findings?limit=5` → `/findings`). */
function routeOf(path: string): string {
  const q = path.indexOf('?')
  return q === -1 ? path : path.slice(0, q)
}

/** How long a settled answer to `path` is reused (0 = shared in flight only). */
export function ttlFor(path: string): number {
  const route = routeOf(path)
  const exact = ROUTE_TTL_MS[route]
  if (exact !== undefined) return exact
  for (const [prefix, ttl] of PREFIX_TTL_MS) if (route.startsWith(prefix)) return ttl
  return 0
}

/** True when a settled answer to `path` is reused for a while (a TTL route). */
export function isCachedGet(path: string): boolean {
  return ttlFor(path) > 0
}

/** True when `path`'s GET is shared at all (in flight, at least). */
export function isSharedGet(path: string): boolean {
  return !UNSHARED_PREFIXES.some((p) => path.startsWith(p))
}

/**
 * Answer `path` through the cache: the in-flight request, else a fresh-enough
 * answer, else `fetcher()` (whose promise becomes the entry).
 */
export function cachedGet<T>(
  path: string,
  fetcher: () => Promise<T>,
  now: () => number = Date.now,
): Promise<T> {
  if (!isSharedGet(path)) return fetcher()
  sweep(now())
  const hit = entries.get(path)
  if (hit) {
    if (!hit.settled) return hit.promise as Promise<T>
    if (reusable(hit, now())) return hit.promise as Promise<T>
    entries.delete(path)
  }
  const entry: Entry = { promise: Promise.resolve(), startedAt: now(), settled: false, ttl: ttlFor(path) }
  const promise = fetcher().then(
    (value) => {
      entry.settled = true
      // An in-flight-only route keeps nothing once it has answered.
      if (entry.ttl <= 0 && entries.get(path) === entry) entries.delete(path)
      return value
    },
    (err: unknown) => {
      // Never cache a failure: drop the entry (only if it is still ours).
      if (entries.get(path) === entry) entries.delete(path)
      throw err
    },
  )
  entry.promise = promise
  entries.set(path, entry)
  return promise
}

/** Drop cached answers — all of them, or those whose path starts with `prefix`. */
export function dropCachedGets(prefix?: string): void {
  if (prefix === undefined) {
    entries.clear()
    return
  }
  for (const key of [...entries.keys()]) if (key.startsWith(prefix)) entries.delete(key)
}

/** How many paths the cache currently holds (tests + the debug overlay). */
export function cachedGetCount(): number {
  return entries.size
}
