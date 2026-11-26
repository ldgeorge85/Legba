/**
 * ONE POINT, asserted on the mounted shell: the top bar names the point the
 * regions filter by; Escape clears it everywhere (after closing an open Record
 * sheet first); picking a subject anywhere goes to the subject tab, which is
 * never empty and has no placeholder region; the Users page opens on Engine.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { App, LAST_SUBJECT_KEY } from '@/App'
import { PreferencesProvider } from '@/components/density/PreferencesProvider'
import { __resetReadTelemetry } from '@/lib/readTelemetry'
import { drive, getShell } from '@/lib/applyAction'
import { subjectReadActions } from '@/lib/subjectRead'
import { resetCasePlayer } from '@/state/casePlayer'
import { resetScope, useScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { resetFrameState } from '@/state/frame'
import { clearViewUndo } from '@/lib/panelActions'
import { dockIds, stageView } from '@/test/frameProbe'
import { findSurface, surfaceViews } from '@/lib/surfaces'

vi.mock('@/panels/v4/MapPanel', () => ({
  default: () => <div data-testid="stub-world-map" />,
}))

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <PreferencesProvider>{ui}</PreferencesProvider>
    </QueryClientProvider>
  )
}

const surface = () => screen.getByTestId('frame').getAttribute('data-surface')
const barLabel = () => screen.getByTestId('subject-switcher-label').textContent ?? ''
const consultPoint = () => screen.queryByTestId('consult-point-name')?.textContent ?? '(consult not drawn)'
const SLOW = { timeout: 10000 }

beforeEach(() => {
  __resetReadTelemetry()
  sessionStorage.clear()
  localStorage.clear()
  window.location.hash = ''
  resetScope()
  resetCasePlayer()
  resetFrameState()
  clearViewUndo()
  useSelection.getState().clear()
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string) => {
      const body = String(url).includes('/registry/') ? [] : { data: [], items: [], next_cursor: null }
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as unknown as Response
    }),
  )
})

afterEach(() => {
  cleanup()
  resetCasePlayer()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

async function boot() {
  render(wrap(<App />))
  await waitFor(() => expect(getShell()).not.toBeNull())
  await waitFor(() => expect(dockIds()).toContain('v4.map'))
}

describe('one point: the top bar, the sheet and Consult agree', { timeout: 30000 }, () => {
  it('a subject made the point is named by the bar and by Consult', async () => {
    await boot()
    act(() => useSelection.getState().select({ kind: 'target', id: 'country_g20_br', label: 'Brazil', origin: 'map' }))
    await waitFor(() => expect(barLabel()).toBe('Brazil'))
    await waitFor(() => expect(consultPoint()).toBe('Brazil'), SLOW)
    // a subject is the point, not a record: the sheet does not open on it
    expect(screen.queryByTestId('record-sheet')).toBeNull()
  })

  it('Escape clears the point everywhere: the bar, the scope, the selection and Consult', async () => {
    await boot()
    act(() => useSelection.getState().select({ kind: 'target', id: 'country_g20_br', label: 'Brazil', origin: 'map' }))
    await waitFor(() => expect(barLabel()).toBe('Brazil'))
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(barLabel()).toMatch(/^Choose a /))
    expect(useScope.getState().scope).toBeNull()
    expect(useSelection.getState().selection).toBeNull()
    await waitFor(() => expect(consultPoint()).toBe('everything in view'), SLOW)
  })

  it('with the Record sheet open, the first Escape closes the sheet and keeps the point; the next clears it', async () => {
    await boot()
    act(() => useSelection.getState().select({ kind: 'target', id: 'country_g20_ru', label: 'Russia', origin: 'map' }))
    act(() => useSelection.getState().select({ kind: 'finding', id: 'f1', label: 'a finding', origin: 'board' }))
    await waitFor(() => expect(screen.getByTestId('record-sheet')).toBeInTheDocument())
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('record-sheet')).toBeNull())
    expect(barLabel()).toBe('Russia')
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(barLabel()).toMatch(/^Choose a /))
    expect(useScope.getState().scope).toBeNull()
  })

  it('Escape typed in a box keeps the point', async () => {
    await boot()
    act(() => useSelection.getState().select({ kind: 'target', id: 'country_g20_br', label: 'Brazil', origin: 'map' }))
    await waitFor(() => expect(barLabel()).toBe('Brazil'))
    const box = await screen.findByTestId('consult-question', {}, SLOW)
    box.focus()
    fireEvent.keyDown(box, { key: 'Escape' })
    expect(useScope.getState().scope?.id).toBe('country_g20_br')
  })

  it('a cleared scope is a cleared bar: the bar never names a subject the regions are not filtered by', async () => {
    await boot()
    act(() => useSelection.getState().select({ kind: 'target', id: 'country_g20_br', label: 'Brazil', origin: 'map' }))
    await waitFor(() => expect(barLabel()).toBe('Brazil'))
    // "Show every subject" on the Board list once cleared only the scope
    act(() => useScope.getState().setScope(null))
    await waitFor(() => expect(barLabel()).toMatch(/^Choose a /))
  })
})

describe('a subject has a home', { timeout: 30000 }, () => {
  it('picking a subject on the Board goes to the subject tab, which follows it — not a copy on the Board stage', async () => {
    await boot()
    expect(surface()).toBe('board')
    act(() => {
      drive(subjectReadActions('country_g20_br', 'Brazil'), { origin: 'rail' })
    })
    await waitFor(() => expect(surface()).toBe('subject'))
    expect(stageView()).toBe('subject.page')
    await waitFor(() => expect(screen.getByTestId('subject-page')).toHaveAttribute('data-subject', 'country_g20_br'), SLOW)
    // no breadcrumb trail of subjects: the page's own view
    expect(screen.queryByTestId('stage-trail')).toBeNull()
    expect(barLabel()).toBe('Brazil')
  })

  it('picking a subject from Watch lands on the subject tab, not under the alert log', async () => {
    await boot()
    act(() => getShell()!.switchStance('watch'))
    await waitFor(() => expect(surface()).toBe('watch'))
    act(() => {
      drive(subjectReadActions('country_g20_ua', 'Ukraine'), { origin: 'switcher' })
    })
    await waitFor(() => expect(surface()).toBe('subject'))
    expect(stageView()).toBe('subject.page')
    act(() => getShell()!.switchStance('watch'))
    await waitFor(() => expect(surface()).toBe('watch'))
    expect(screen.queryByTestId('stage-trail')).toBeNull()
  })

  it('the subject tab opens on the last subject looked at; it has no placeholder region', async () => {
    localStorage.setItem(LAST_SUBJECT_KEY, 'country_g20_de')
    await boot()
    act(() => getShell()!.switchStance('subject'))
    await waitFor(() => expect(surface()).toBe('subject'))
    await waitFor(() => expect(screen.getByTestId('subject-page')).toHaveAttribute('data-subject', 'country_g20_de'), SLOW)
    expect(useScope.getState().scope?.id).toBe('country_g20_de')
    expect(surfaceViews(findSurface('subject')!)).not.toContain('slot:subject_forecasts')
    expect(dockIds()).not.toContain('slot:subject_forecasts')
    expect(screen.queryByText(/Not built yet/)).toBeNull()
  })
})
