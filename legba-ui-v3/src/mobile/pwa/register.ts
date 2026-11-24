/**
 * P-1 — the phone app registers its service worker, and learns when a new
 * version is waiting.
 *
 * Scope `/m/` ONLY (the script is `/m/sw.js`; the desktop at `/` is never
 * controlled). `updateViaCache: 'none'` so an update check never reads the
 * worker script from the HTTP cache.
 *
 * THE UPDATE FLOW. A new worker installs in the background and then WAITS:
 * the browser hands it the page only when every copy of the app is closed,
 * and an installed app is rarely closed. So the page shows "Update available"
 * (`PwaBanners`) and applies it only when the person taps Reload: the waiting
 * worker is told to skip waiting, and the page reloads once the new worker has
 * taken control. Nothing reloads by itself — a `controllerchange` the person
 * did not ask for (the very first install claiming the page) reloads nothing.
 *
 * Update checks: on load (the browser's own), whenever the app comes back to
 * the foreground, and hourly while it stays open.
 */

export const SW_URL = '/m/sw.js'
export const SW_SCOPE = '/m/'
export const MSG_SKIP_WAITING = 'LEGBA_SW_SKIP_WAITING'
export const MSG_CLEAR = 'LEGBA_SW_CLEAR'
const HOURLY_MS = 60 * 60 * 1000

export interface PwaState {
  /** A new version has installed and is waiting for the person's tap. */
  updateReady: boolean
  /** The person tapped Reload; the page reloads when the new worker takes over. */
  applying: boolean
}

let state: PwaState = { updateReady: false, applying: false }
let waiting: ServiceWorker | null = null
let watched: ServiceWorkerRegistration | null = null
const listeners = new Set<() => void>()

function set(next: Partial<PwaState>) {
  state = { ...state, ...next }
  for (const l of listeners) l()
}

export function getPwaState(): PwaState {
  return state
}

export function subscribePwa(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

function markWaiting(worker: ServiceWorker | null, sw: ServiceWorkerContainer) {
  // Only an UPDATE waits for the person: with no controller this is the first
  // install, which activates on its own and has nothing to replace.
  if (!worker || !sw.controller) return
  waiting = worker
  set({ updateReady: true })
}

/** Watch one registration for a version that installs and waits. */
export function watchRegistration(reg: ServiceWorkerRegistration, sw: ServiceWorkerContainer): void {
  watched = reg
  if (reg.waiting) markWaiting(reg.waiting, sw)
  reg.addEventListener('updatefound', () => {
    const next = reg.installing
    if (!next) return
    next.addEventListener('statechange', () => {
      if (next.state === 'installed') markWaiting(next, sw)
    })
  })
}

/** The person tapped Reload: hand the page to the waiting version, then reload once. */
export function applyUpdate(
  sw: ServiceWorkerContainer | undefined = typeof navigator !== 'undefined' ? navigator.serviceWorker : undefined,
  reload: () => void = () => window.location.reload(),
): boolean {
  // The worker waiting NOW (a later update may have replaced the one first seen).
  const target = watched?.waiting ?? waiting
  if (!sw || !target || state.applying) return false
  set({ applying: true })
  sw.addEventListener('controllerchange', () => reload(), { once: true })
  target.postMessage({ type: MSG_SKIP_WAITING })
  return true
}

/**
 * Register the worker (production builds only; the dev server has no
 * `/m/sw.js`). Never throws: an app without a worker still works online.
 */
export async function registerPhoneWorker(opts: { force?: boolean } = {}): Promise<ServiceWorkerRegistration | null> {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return null
  if (!opts.force && !import.meta.env.PROD) return null
  const sw = navigator.serviceWorker
  let reg: ServiceWorkerRegistration | null = null
  try {
    reg = await sw.register(SW_URL, { scope: SW_SCOPE, updateViaCache: 'none' })
  } catch {
    // Offline, register()'s own update check fails; the registration made
    // on an earlier visit still stands — watch that one.
    reg = (await Promise.resolve().then(() => sw.getRegistration(SW_SCOPE)).catch(() => undefined)) ?? null
  }
  // No worker at all (a private window, the edge refused the script): online only.
  if (!reg) return null
  try {
    const found = reg
    watchRegistration(found, sw)
    const check = () => {
      found.update().catch(() => {
        /* offline or the edge refused: the next check tries again */
      })
    }
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') check()
    })
    window.setInterval(() => {
      if (document.visibilityState === 'visible') check()
    }, HOURLY_MS)
    window.addEventListener('online', check)
    return found
  } catch {
    return null
  }
}

/** Test seam: forget the module state between tests. */
export function __resetPwaForTests(): void {
  state = { updateReady: false, applying: false }
  waiting = null
  watched = null
  listeners.clear()
}
