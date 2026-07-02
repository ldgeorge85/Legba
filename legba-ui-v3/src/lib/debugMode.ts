/**
 * Debug chrome toggle (S7-T1 item 5).
 *
 * The developer-facing plumbing — the panel-header `from <descriptor>@<version>`
 * provenance stamp, the `N panels registered` status-bar counter, and the
 * density (T/C/Cf) control — is noise for an operator. It hides behind this flag,
 * which defaults OFF. Flip it with `?debug=1` in the URL, `localStorage.legba_debug`,
 * or the Ctrl/Cmd+Shift+D shortcut (wired in App). Backed by a tiny external
 * store so any `useDebugMode()` subscriber re-renders when it toggles.
 */
import { useSyncExternalStore } from 'react'

const KEY = 'legba_debug'
const listeners = new Set<() => void>()

export function isDebugMode(): boolean {
  try {
    if (localStorage.getItem(KEY) === '1') return true
    return new URLSearchParams(window.location.search).get('debug') === '1'
  } catch {
    return false
  }
}

export function setDebugMode(on: boolean): void {
  try {
    if (on) localStorage.setItem(KEY, '1')
    else localStorage.removeItem(KEY)
  } catch {
    // localStorage may be unavailable (private mode / SSR) — ignore.
  }
  for (const l of listeners) l()
}

export function toggleDebugMode(): void {
  setDebugMode(!isDebugMode())
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}

/** Reactive read of the debug flag — re-renders the caller on toggle. */
export function useDebugMode(): boolean {
  return useSyncExternalStore(subscribe, isDebugMode, () => false)
}
