/**
 * M-3g — Consult inside the phone shell, end to end in the page:
 *
 *   5  "Ask about this" on a detail page switches to the Consult tab with the
 *      point shown above the composer
 *   6  a citation in an answer opens the Record's peek (`#rec=…&peek=1`);
 *      Back returns to the thread, its scroll kept
 *   4  a `panel_action` a live case presents navigates the phone through its
 *      frame port; the Undo toast on the screen it opened puts it back
 *   7  a bare `#rec=` opened cold is the Record's page with its parent seeded
 *      underneath
 * Invented rows, through a stubbed `fetch`; no turn is sent.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import PhoneShell from './PhoneShell'
import { usePhoneShell } from './phoneState'
import { phoneScreenOf } from './phoneAddress'
import { useConsultSessions } from '@/state/consultSession'
import { casePlayer, resetCasePlayer } from '@/state/casePlayer'
import { useSelection } from '@/state/selection'
import { useRecordSheet } from '@/lib/recordSheet'
import { MOBILE_PANEL_ID } from '@/mobile/mobileConsult'

function res(status: number, body: unknown): Response {
  return { ok: status < 400, status, json: async () => body, text: async () => JSON.stringify(body) } as unknown as Response
}

const REF = 'a1b2c3d4-0000-4000-8000-000000000001'

beforeEach(() => {
  localStorage.clear()
  useConsultSessions.setState({ panels: {} })
  usePhoneShell.setState({ watchSegment: 'alerts', lastScreen: null, asking: null })
  useSelection.getState().clear()
  useRecordSheet.setState({ ref: null, cite: null, open: false })
  resetCasePlayer()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const u = String(url)
      if (u.includes('/v3/me/seen')) return res(404, { detail: { error: 'seen_cursor_unavailable', reason: 'signin_off' } })
      if (u.includes('/auth/me')) return res(200, { kind: 'operator', actor: 'operator:bearer:x', role: 'operator', user: null, session_rejected: false })
      if (u.includes('/lineage/')) return res(200, { root: { kind: 'signal', id: REF, title: 'Example record' }, nodes: [], edges: [] })
      return res(200, { data: [] })
    }),
  )
})

afterEach(() => {
  cleanup()
  resetCasePlayer()
  vi.unstubAllGlobals()
})

function mount(hash: string) {
  window.history.replaceState(null, '', `/m/${hash}`)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <PhoneShell />
    </QueryClientProvider>,
  )
}

function seedAnswer() {
  useConsultSessions.setState({
    panels: {
      [MOBILE_PANEL_ID]: {
        ...useConsultSessions.getState().panel(MOBILE_PANEL_ID),
        transcript: [
          { role: 'user', content: 'what changed?' },
          { role: 'assistant', content: 'A strike closed the port [a1b2c3d4].', citedRefs: [{ kind: 'signal', id: REF, description: 'Example record' }] },
        ],
      },
    },
  })
}

describe('Consult inside the phone shell', () => {
  it('5 · "Ask about this" on a subject page opens Consult with the point above the composer', async () => {
    mount('#ws=subject&screen=subject%3Aexample_x')
    const ask = await screen.findByTestId('phone-ask-about-this', {}, { timeout: 5000 })
    fireEvent.click(ask)
    await waitFor(() => expect(screen.getByTestId('phone-shell').getAttribute('data-tab')).toBe('consult'))
    const label = await screen.findByTestId('phone-asking-label', {}, { timeout: 5000 })
    expect(label.textContent).toBeTruthy()
    expect(usePhoneShell.getState().asking?.pins[0]).toMatchObject({ kind: 'target', id: 'example_x' })
    // the screen the person came from is the on-screen report's screen
    expect(usePhoneShell.getState().lastScreen?.page).toEqual({ kind: 'subject', id: 'example_x' })
  })

  it('6 · a citation opens the Record peek; Back returns to the thread with its scroll kept', async () => {
    seedAnswer()
    mount('#screen=consult')
    const turn = await screen.findByTestId('consult-turn-assistant', {}, { timeout: 5000 })
    // the thread's own "to the bottom" runs on the next frame; the reader scrolls after it
    await act(() => new Promise<void>((r) => requestAnimationFrame(() => r())))
    const thread = screen.getByTestId('consult-transcript')
    thread.scrollTop = 37
    fireEvent.click(within(turn).getByTestId('citation-chip'))
    await waitFor(() => expect(window.location.hash).toContain('peek=1'))
    expect(window.location.hash).toContain(`rec=signal%3A${REF}`)
    expect(await screen.findByTestId('record-peek')).toBeInTheDocument()
    expect(phoneScreenOf(window.location.hash).tab).toBe('consult')
    act(() => window.history.back())
    await waitFor(() => expect(screen.queryByTestId('record-peek')).toBeNull())
    expect(screen.getByTestId('consult-turn-assistant')).toBe(turn)
    expect(screen.getByTestId('consult-transcript').scrollTop).toBe(37)
  })

  it('4 · a panel_action from a live case navigates; the Undo toast puts the phone back', async () => {
    mount('#screen=consult')
    await screen.findByTestId('phone-consult')
    act(() => {
      casePlayer().beginLive('r-live', MOBILE_PANEL_ID)
      casePlayer().ingest({
        type: 'step',
        kind: 'case_action',
        case_action: { seq: 1, action: 'panel_action', args: { view: 'subject.page:example_x', action: 'open', args: {} }, cites: [], rests_on: [] },
      })
    })
    await waitFor(() => expect(phoneScreenOf(window.location.hash).page).toEqual({ kind: 'subject', id: 'example_x' }))
    const toast = await screen.findByTestId('phone-drive-toast')
    expect(toast.textContent).toMatch(/Consult opened this/)
    fireEvent.click(within(toast).getByTestId('phone-drive-undo'))
    await waitFor(() => expect(phoneScreenOf(window.location.hash)).toMatchObject({ tab: 'consult', page: null }))
    expect(screen.queryByTestId('phone-drive-toast')).toBeNull()
  })

  it('7 · a bare #rec= opened cold is the Record page, the landing seeded under it', async () => {
    const before = window.history.length
    mount(`#rec=signal:${REF}`)
    expect(await screen.findByTestId('record-page')).toBeInTheDocument()
    expect(screen.getByTestId('phone-shell').getAttribute('data-depth')).toBe('1')
    expect(window.history.length).toBe(before + 1)
    act(() => window.history.back())
    await waitFor(() => expect(screen.getByTestId('phone-shell').getAttribute('data-depth')).toBe('0'))
    expect(phoneScreenOf(window.location.hash).tab).toBe('changed')
  })
})
