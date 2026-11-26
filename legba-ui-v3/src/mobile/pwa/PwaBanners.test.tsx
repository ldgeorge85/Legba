/**
 * P-1 / P-3 — the offline, update and install lines.
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { INSTALL_HINT_KEY, isIosSafari, latestLoadedAt, PwaBanners } from './PwaBanners'
import { __resetPwaForTests, watchRegistration } from './register'

function setOnline(v: boolean) {
  Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => v })
  window.dispatchEvent(new Event(v ? 'online' : 'offline'))
}

function mount(qc = new QueryClient()) {
  render(
    <QueryClientProvider client={qc}>
      <PwaBanners />
    </QueryClientProvider>,
  )
  return qc
}

beforeEach(() => {
  localStorage.clear()
  Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => true })
})
afterEach(() => {
  __resetPwaForTests()
  delete document.documentElement.dataset.pwaBanner
})

describe('offline', () => {
  it('online and nothing to offer: no banner at all', () => {
    mount()
    expect(screen.queryByTestId('pwa-banners')).toBeNull()
  })

  it('says plainly it is offline and when what is on screen was loaded (UTC), and refreshes on reconnect', async () => {
    const qc = new QueryClient()
    qc.setQueryData(['mobile-reads'], [{ id: 'r1' }], { updatedAt: Date.UTC(2026, 10, 14, 7, 41) })
    qc.setQueryData(['auth', 'me'], { state: 'signed_in' }, { updatedAt: Date.UTC(2026, 10, 14, 9, 0) })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    mount(qc)
    act(() => setOnline(false))
    const line = screen.getByTestId('pwa-offline')
    // the sign-in check is not "what was loaded": the read's time is shown
    expect(line).toHaveTextContent('No connection. Showing what was loaded at 07:41 UTC.')
    expect(document.documentElement.dataset.pwaBanner).toBe('1')
    act(() => setOnline(true))
    expect(screen.queryByTestId('pwa-offline')).toBeNull()
    expect(invalidate).toHaveBeenCalled()
  })

  it('offline before anything loaded says that, not a time', () => {
    mount()
    act(() => setOnline(false))
    expect(screen.getByTestId('pwa-offline')).toHaveTextContent('No connection. Nothing has loaded yet in this session.')
  })

  it('latestLoadedAt ignores failed and pending queries', () => {
    const qc = new QueryClient()
    expect(latestLoadedAt(qc)).toBeNull()
    qc.setQueryData(['a'], 1, { updatedAt: 1000 })
    qc.setQueryData(['b'], 1, { updatedAt: 3000 })
    expect(latestLoadedAt(qc)).toBe(3000)
  })
})

describe('update available', () => {
  it('shows "Update available" with Reload, and the tap asks the waiting worker to take over', () => {
    const waiting = Object.assign(new EventTarget(), { postMessage: vi.fn(), state: 'installed' })
    const sw = Object.assign(new EventTarget(), { controller: {} })
    Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: sw })
    try {
      const reg = Object.assign(new EventTarget(), { waiting, installing: null })
      act(() => watchRegistration(reg as unknown as ServiceWorkerRegistration, sw as unknown as ServiceWorkerContainer))
      mount()
      expect(screen.getByTestId('pwa-update')).toHaveTextContent('Update available.')
      // the walk reads the tap off the line (F312: a missed tap vs a stalled hand-over)
      expect(screen.getByTestId('pwa-update')).toHaveAttribute('data-applying', '0')
      fireEvent.click(screen.getByTestId('pwa-update-reload'))
      expect(waiting.postMessage).toHaveBeenCalledWith({ type: 'LEGBA_SW_SKIP_WAITING' })
      expect(screen.getByTestId('pwa-update-reload')).toHaveTextContent('Reloading…')
      expect(screen.getByTestId('pwa-update')).toHaveAttribute('data-applying', '1')
    } finally {
      delete (navigator as unknown as Record<string, unknown>).serviceWorker
    }
  })
})

describe('the install hint', () => {
  it('appears only when the browser offers install, prompts on tap, and "Not now" hides it for good', async () => {
    mount()
    expect(screen.queryByTestId('pwa-install')).toBeNull()
    const prompt = vi.fn(async () => {})
    const ev = Object.assign(new Event('beforeinstallprompt', { cancelable: true }), { prompt })
    act(() => {
      window.dispatchEvent(ev)
    })
    expect(ev.defaultPrevented).toBe(true)
    fireEvent.click(screen.getByText('Install'))
    expect(prompt).toHaveBeenCalled()
    expect(screen.queryByTestId('pwa-install')).toBeNull()

    act(() => {
      window.dispatchEvent(Object.assign(new Event('beforeinstallprompt', { cancelable: true }), { prompt }))
    })
    fireEvent.click(screen.getByText('Not now'))
    expect(localStorage.getItem(INSTALL_HINT_KEY)).toBe('1')
    expect(screen.queryByTestId('pwa-install')).toBeNull()
  })

  it('once dismissed it never comes back on this device', () => {
    localStorage.setItem(INSTALL_HINT_KEY, '1')
    mount()
    act(() => {
      window.dispatchEvent(Object.assign(new Event('beforeinstallprompt', { cancelable: true }), { prompt: vi.fn() }))
    })
    expect(screen.queryByTestId('pwa-install')).toBeNull()
  })

  it('knows iPhone/iPad Safari (the Add to Home Screen line) from the other iOS browsers and from Android', () => {
    const safari = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'
    expect(isIosSafari(safari)).toBe(true)
    expect(isIosSafari(safari.replace('Version/18.0', 'CriOS/130.0'))).toBe(false)
    expect(isIosSafari('Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36')).toBe(false)
    const ipadDesktopUa = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15'
    expect(isIosSafari(ipadDesktopUa, true)).toBe(true)
    expect(isIosSafari(ipadDesktopUa, false)).toBe(false)
  })

  it('on iPhone Safari it explains Add to Home Screen once; never inside the installed app', () => {
    const ua = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(
      'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
    )
    try {
      mount()
      expect(screen.getByTestId('pwa-install-ios')).toHaveTextContent('tap Share, then “Add to Home Screen”')
      fireEvent.click(screen.getByText('Got it'))
      expect(screen.queryByTestId('pwa-install-ios')).toBeNull()
      localStorage.clear()
      Object.defineProperty(navigator, 'standalone', { configurable: true, value: true })
      mount()
      expect(screen.queryByTestId('pwa-install-ios')).toBeNull()
    } finally {
      ua.mockRestore()
      delete (navigator as unknown as Record<string, unknown>).standalone
    }
  })
})
