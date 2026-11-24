/**
 * P-1 / P-3 — the phone app's three quiet lines, above everything else:
 *
 *   - OFFLINE (P-3): "No connection. Showing what was loaded at 14:52 UTC."
 *     The time is the newest answer this session loaded (in memory — nothing
 *     of the record is stored on the device, M-0a). Nothing pretends to be
 *     current: the line stays until the connection returns, and then every
 *     query on screen is refreshed. "Try again" refreshes by hand (an
 *     installed app has no browser reload).
 *   - UPDATE (P-1): "Update available" + Reload. A new version waits until
 *     the person taps; nothing reloads by itself (`register.ts`).
 *   - INSTALL (P-1): only where the browser offers install — Android's
 *     `beforeinstallprompt` — or, on iPhone/iPad Safari, one short line on how
 *     to Add to Home Screen. Never inside the installed app; one tap on
 *     "Not now" / "Got it" hides it for good on this device. It never nags.
 */
import { useCallback, useEffect, useState, useSyncExternalStore } from 'react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { IDENTITY_QUERY_KEY } from '@/auth/session'
import { loadedAtLabel } from '../mobileCache'
import { applyUpdate, getPwaState, subscribePwa } from './register'
import './pwa.css'

export const INSTALL_HINT_KEY = 'legba_m_install_hint_dismissed'

// ── connection ───────────────────────────────────────────────────────────────

function subscribeOnline(fn: () => void): () => void {
  window.addEventListener('online', fn)
  window.addEventListener('offline', fn)
  return () => {
    window.removeEventListener('online', fn)
    window.removeEventListener('offline', fn)
  }
}

export function useOnline(): boolean {
  return useSyncExternalStore(subscribeOnline, () => navigator.onLine !== false)
}

/** The newest successful answer this session loaded (the sign-in check aside). */
export function latestLoadedAt(qc: QueryClient): number | null {
  let latest = 0
  for (const q of qc.getQueryCache().getAll()) {
    if (q.queryKey[0] === IDENTITY_QUERY_KEY[0] && q.queryKey[1] === IDENTITY_QUERY_KEY[1]) continue
    if (q.state.status === 'success' && q.state.dataUpdatedAt > latest) latest = q.state.dataUpdatedAt
  }
  return latest || null
}

function useLatestLoadedAt(qc: QueryClient): number | null {
  const subscribe = useCallback((fn: () => void) => qc.getQueryCache().subscribe(fn), [qc])
  return useSyncExternalStore(subscribe, () => latestLoadedAt(qc))
}

// ── install hint ─────────────────────────────────────────────────────────────

interface InstallPromptEvent extends Event {
  prompt: () => Promise<void>
}

function readDismissed(): boolean {
  try {
    return localStorage.getItem(INSTALL_HINT_KEY) === '1'
  } catch {
    return false
  }
}

function writeDismissed(): void {
  try {
    localStorage.setItem(INSTALL_HINT_KEY, '1')
  } catch {
    /* storage disabled: the hint simply shows again next time */
  }
}

/** Installed (standalone) already — no hint, ever. */
export function isStandalone(): boolean {
  const nav = navigator as Navigator & { standalone?: boolean }
  if (nav.standalone === true) return true
  try {
    return window.matchMedia('(display-mode: standalone)').matches
  } catch {
    return false
  }
}

/** iPhone / iPad Safari, where install is Share → Add to Home Screen. */
export function isIosSafari(ua: string = navigator.userAgent, touchMac = false): boolean {
  const ios = /iPhone|iPad|iPod/.test(ua) || (touchMac && /Macintosh/.test(ua))
  return ios && /Safari\//.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS/.test(ua)
}

function useInstallHint(): { kind: 'android' | 'ios' | null; install: () => void; dismiss: () => void } {
  const [prompt, setPrompt] = useState<InstallPromptEvent | null>(null)
  const [dismissed, setDismissed] = useState(readDismissed)
  const [standalone] = useState(isStandalone)
  const [ios] = useState(() => isIosSafari(navigator.userAgent, navigator.maxTouchPoints > 1))

  useEffect(() => {
    const onPrompt = (e: Event) => {
      e.preventDefault() // our own quiet line instead of the browser's bar
      setPrompt(e as InstallPromptEvent)
    }
    const onInstalled = () => {
      setPrompt(null)
      writeDismissed()
      setDismissed(true)
    }
    window.addEventListener('beforeinstallprompt', onPrompt)
    window.addEventListener('appinstalled', onInstalled)
    return () => {
      window.removeEventListener('beforeinstallprompt', onPrompt)
      window.removeEventListener('appinstalled', onInstalled)
    }
  }, [])

  const dismiss = () => {
    writeDismissed()
    setDismissed(true)
    setPrompt(null)
  }
  const install = () => {
    const p = prompt
    setPrompt(null)
    p?.prompt().catch(() => {
      /* the browser declined to show its sheet */
    })
  }
  let kind: 'android' | 'ios' | null = null
  if (!standalone && !dismissed) kind = prompt ? 'android' : ios ? 'ios' : null
  return { kind, install, dismiss }
}

// ── the banners ──────────────────────────────────────────────────────────────

export function PwaBanners() {
  const qc = useQueryClient()
  const online = useOnline()
  const loadedAt = useLatestLoadedAt(qc)
  const pwa = useSyncExternalStore(subscribePwa, getPwaState)
  const hint = useInstallHint()

  // Back online: refresh what is on screen (P-3).
  useEffect(() => {
    const on = () => {
      void qc.invalidateQueries()
    }
    window.addEventListener('online', on)
    return () => window.removeEventListener('online', on)
  }, [qc])

  const any = !online || pwa.updateReady || hint.kind !== null
  useEffect(() => {
    // The shell's own top inset steps aside while a banner holds the top.
    const root = document.documentElement
    if (any) root.dataset.pwaBanner = '1'
    else delete root.dataset.pwaBanner
    return () => {
      delete root.dataset.pwaBanner
    }
  }, [any])
  if (!any) return null

  return (
    <div className="pwa-banners" data-testid="pwa-banners">
      {!online && (
        <div className="pwa-line" role="status" data-testid="pwa-offline">
          <span className="pwa-text">
            {loadedAtLabel(loadedAt)}
            {loadedAt ? '' : ' Nothing has loaded yet in this session.'}
          </span>
          <button type="button" className="pwa-action" onClick={() => void qc.invalidateQueries()}>
            Try again
          </button>
        </div>
      )}
      {pwa.updateReady && (
        // data-applying: the tap was taken (the walk records it beside the
        // worker states, so a failed hand-over tells a missed tap from a stall)
        <div className="pwa-line" role="status" data-testid="pwa-update" data-applying={pwa.applying ? '1' : '0'}>
          <span className="pwa-text">Update available.</span>
          <button
            type="button"
            className="pwa-action pwa-action-point"
            data-testid="pwa-update-reload"
            disabled={pwa.applying}
            onClick={() => applyUpdate()}
          >
            {pwa.applying ? 'Reloading…' : 'Reload'}
          </button>
        </div>
      )}
      {hint.kind === 'android' && (
        <div className="pwa-line" data-testid="pwa-install">
          <span className="pwa-text">Install Legba on this phone.</span>
          <button type="button" className="pwa-action pwa-action-point" onClick={hint.install}>
            Install
          </button>
          <button type="button" className="pwa-action" onClick={hint.dismiss}>
            Not now
          </button>
        </div>
      )}
      {hint.kind === 'ios' && (
        <div className="pwa-line" data-testid="pwa-install-ios">
          <span className="pwa-text">
            To install: tap Share, then &ldquo;Add to Home Screen&rdquo;.
          </span>
          <button type="button" className="pwa-action" onClick={hint.dismiss}>
            Got it
          </button>
        </div>
      )}
    </div>
  )
}
