/**
 * M-3g — Settings carries the desktop's drive mode ("Let answers open
 * screens"), with the desktop's default (off) and the same per-viewer store.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SettingsPage from './SettingsPage'
import { resetDriveMode, useDriveMode } from '@/state/driveMode'

beforeEach(() => {
  localStorage.clear()
  resetDriveMode()
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ kind: 'operator', role: 'operator', user: null }), text: async () => '{}' })))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('Settings · Consult', () => {
  it('drive mode starts off, as on the desktop, and the switch turns it on in the shared store', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <SettingsPage />
      </QueryClientProvider>,
    )
    const sw = screen.getByTestId('phone-settings-drive')
    expect(sw.getAttribute('aria-checked')).toBe('false')
    expect(screen.getByTestId('phone-settings-drive-words').textContent).toMatch(/Answers move nothing/)
    fireEvent.click(sw)
    expect(useDriveMode.getState().on).toBe(true)
    expect(localStorage.getItem('legba_consult_drive')).toBe('on')
    expect(screen.getByTestId('phone-settings-drive').getAttribute('aria-checked')).toBe('true')
    expect(screen.getByTestId('phone-settings-drive-words').textContent).toMatch(/Undo puts you back/)
  })
})
