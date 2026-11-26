/**
 * "As recorded" — reading the record as the system held it at an earlier
 * instant.
 *
 * The instant lives in the shared world store (`worldState.asOfMs`; null means
 * now) and rides the address as `?at=<ISO time>`, so a view of the past can
 * be shared and reopened. The time band's marker is the one control that sets
 * it.
 *
 * A region honours it only when every read it makes can be answered as of
 * the instant. Those regions are listed in {@link AS_OF_VIEWS}; every other
 * region says plainly that it is not time-shifted rather than showing today's
 * data under a past date.
 *
 * Routes that answer `at` today: `GET /findings` (every weight) and
 * `GET /v3/ledger/forecasts`. A region joins {@link AS_OF_VIEWS} once each of
 * its reads passes the instant through {@link withAt} and keys its query on it.
 */
import { useWorldState } from '@/v4/world/worldState'
import { dayTime } from '@/lib/timeBand'

/** The address key. */
export const AT_PARAM = 'at'

/**
 * The views whose reads all honour the instant — each passes it through
 * {@link withAt} and keys its query on it. No region claims a past it
 * cannot show: the Board list, the subject page and the Watch log read the
 * since route, situations, events or signals, which cannot answer `at`, and
 * keep the "not time-shifted" strip.
 *
 *   view:forecasts         the Ledger's forecasts (`GET /v3/ledger/forecasts`)
 *   analyst.cross_target   a unit's findings across subjects (`GET /findings`)
 *   analyst.outputs        a unit's findings (`GET /findings`; its live tail
 *                          is off while an instant is set)
 */
export const AS_OF_VIEWS: ReadonlySet<string> = new Set<string>(['view:forecasts', 'analyst.cross_target', 'analyst.outputs'])

/** Does this view (by id, or `<id>:<record>`) show the record as of the instant? */
export function honoursAsOf(viewId: string): boolean {
  if (AS_OF_VIEWS.has(viewId)) return true
  const idx = viewId.indexOf(':', viewId.indexOf('.') + 1)
  return idx > 0 && AS_OF_VIEWS.has(viewId.slice(0, idx))
}

/** `?at=` → epoch ms; null when absent, unparseable, or not in the past. */
export function parseAt(search: string, now = Date.now()): number | null {
  try {
    const raw = new URLSearchParams(search.replace(/^\?/, '')).get(AT_PARAM)
    if (!raw) return null
    const t = Date.parse(raw)
    if (Number.isNaN(t) || t >= now) return null
    return t
  } catch {
    return null
  }
}

/** The search string with `at` set to the instant, or removed for now. */
export function searchWithAt(search: string, atMs: number | null): string {
  const p = new URLSearchParams(search.replace(/^\?/, ''))
  if (atMs === null) p.delete(AT_PARAM)
  else p.set(AT_PARAM, new Date(atMs).toISOString())
  const q = p.toString()
  return q ? `?${q}` : ''
}

/** Mirror the instant into the address bar without a history entry. */
export function writeAtParam(atMs: number | null): void {
  try {
    const { pathname, search, hash } = window.location
    const next = searchWithAt(search, atMs)
    if (next !== search) window.history.replaceState(null, '', `${pathname}${next}${hash}`)
  } catch {
    // history unavailable — sharing is best-effort, never fatal.
  }
}

/** A route path with `at=<ISO>` added when an instant is set. */
export function withAt(path: string, atMs: number | null): string {
  if (atMs === null) return path
  const sep = path.includes('?') ? '&' : '?'
  return `${path}${sep}${AT_PARAM}=${encodeURIComponent(new Date(atMs).toISOString())}`
}

/** `20 Nov 08:10 UTC` — the strip's and the marker's form. */
export function asOfWords(atMs: number): string {
  return `${dayTime(atMs)} UTC`
}

/** React: the instant (null = now). */
export function useAsOf(): number | null {
  return useWorldState((s) => s.asOfMs)
}
