/**
 * UI-B6 — A RECORDED CASE REPLAYS END TO END ON THE NEW SHELL.
 *
 * The fixture is a REAL case (`test/liveCaseFixture.ts`: session
 * afa27c66…, turn 9037291e…, read with GET only from the live registry). The
 * REAL shell boots on `#case=<session>:<turn>`, the case player replays it
 * through the ONE executor (driver `case`), the reader steps it with the
 * CaseRail's own "Next step" button, and the test reads back:
 *
 *  - the CaseRail's outcome per action (every step `executed`, sealed);
 *  - the dock's end state — the stance, the tiles (the two bound reads of the
 *    subject in ONE tab group, the presenting Consult tile brought back), the
 *    scope, the focus, the window (no clock step: unmoved);
 *  - the ambient pin following the reader: the bound tile pins its subject
 *    the moment it opens, and the pin follows the focus after.
 *
 * And the STOP path: a replay stopped mid-way runs nothing after the latch; a
 * LIVE case stopped mid-way tells the stop route the step it stopped after,
 * and the record that lands stays stopped — as the real stopped session
 * (54f54de7…) reads back on reload.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { App } from '@/App'
import { PreferencesProvider } from '@/components/density/PreferencesProvider'
import { __resetReadTelemetry } from '@/lib/readTelemetry'
import { drive, getShell } from '@/lib/applyAction'
import { openPanel } from '@/lib/commands'
import { CONSULT_PANEL_ID } from '@/lib/caseReplay'
import { parseCaseRecord } from '@/lib/caseModel'
import { resetAmbientPins, SCOPE_PIN_ORIGIN } from '@/lib/consultContext'
import { casePlayer, resetCasePlayer } from '@/state/casePlayer'
import { useConsultSessions } from '@/state/consultSession'
import { resetScope, useScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { resetPanelParams, usePanelParamsStore } from '@/state/panelParams'
import { useWorldState } from '@/v4/world/worldState'
import { screenIds, stageView } from '@/test/frameProbe'
import { resetFrameState, stageInstanceId, useFrame } from '@/state/frame'
import {
  LIVE_CASE,
  LIVE_CASE_SESSION,
  LIVE_CASE_SESSION_ID,
  LIVE_CASE_SITUATION,
  LIVE_CASE_SUBJECT,
  LIVE_CASE_TURN_ID,
  LIVE_STOPPED_SESSION,
  LIVE_STOPPED_SESSION_ID,
} from '@/test/liveCaseFixture'

vi.mock('@/panels/v4/MapPanel', () => ({
  default: () => <div data-testid="stub-world-map" />,
}))

// The Consult tile and the bound reads are lazy chunks that re-mount with each
// arrangement; under the full suite's load they can take seconds to land.
configure({ asyncUtilTimeout: 30000 })

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <PreferencesProvider>{ui}</PreferencesProvider>
    </QueryClientProvider>
  )
}

interface Layout {
  grid?: { root?: unknown }
  panels?: Record<string, unknown>
}
const layout = () => (getShell()!.serializeLayout() as Layout) ?? {}
// U-6: what is on screen — the stage's view and the panel side's.
const dock = () => screenIds()
const ws = () => new URLSearchParams(window.location.hash.replace(/^#/, '')).get('ws')
/** Each grid leaf's views — one tab group per leaf. */
const groups = (node: unknown = layout().grid?.root): string[][] => {
  if (!node || typeof node !== 'object') return []
  const n = node as { type?: string; data?: unknown }
  if (n.type === 'leaf') return [((n.data as { views?: string[] })?.views ?? []).slice()]
  return Array.isArray(n.data) ? n.data.flatMap((c) => groups(c)) : []
}
const consultScopePin = () =>
  useConsultSessions
    .getState()
    .panel(CONSULT_PANEL_ID)
    .pins.find((p) => p.origin === SCOPE_PIN_ORIGIN)

let fetchBodies: Record<string, unknown> = {}
let posts: Array<{ url: string; body: unknown }> = []

beforeEach(() => {
  resetFrameState()
  __resetReadTelemetry()
  sessionStorage.clear()
  localStorage.clear()
  window.location.hash = ''
  resetScope()
  resetCasePlayer()
  resetAmbientPins()
  resetPanelParams()
  useSelection.getState().clear()
  fetchBodies = {}
  posts = []
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      const u = String(url)
      if (init?.method === 'POST') posts.push({ url: u, body: init.body ? JSON.parse(String(init.body)) : null })
      const hit = Object.entries(fetchBodies).find(([needle]) => u.includes(needle))
      const body = hit
        ? hit[1]
        : u.includes('/registry/')
          ? []
          : Object.assign([], { data: [], items: [], next_cursor: null })
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as unknown as Response
    }),
  )
})

afterEach(() => {
  cleanup()
  resetCasePlayer()
  useConsultSessions.getState().reset(CONSULT_PANEL_ID)
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

async function bootOnCase(session: string, turn: string, detail: unknown) {
  fetchBodies[`/consult/sessions/${session}`] = detail
  window.location.hash = `#case=${session}:${turn}`
  render(wrap(<App />))
  await waitFor(() => expect(getShell()).not.toBeNull())
}

describe('a recorded case replays END TO END on the new shell', () => {
  it('#case= → the CaseRail steps it → the dock, scope, focus, window and pin end where the case did', async () => {
    const before = { start: useWorldState.getState().windowStartMs, end: useWorldState.getState().windowEndMs }
    await bootOnCase(LIVE_CASE_SESSION_ID, LIVE_CASE_TURN_ID, LIVE_CASE_SESSION)
    await waitFor(() => expect(casePlayer().player?.mode).toBe('replay'))
    // The rail is in the presenting Consult tile; its "Next step" drives.
    const rail = await screen.findByTestId('case-rail', {}, { timeout: 30000 })
    expect(within(rail).getByTestId('case-replay-caption')).toHaveTextContent('Replaying a case recorded 2026-10-04T04:31:12Z')
    expect(within(rail).getAllByTestId('case-step')).toHaveLength(5)

    // An arrangement replaces the dock and brings the presenting tile back, so
    // the rail re-mounts: each step waits for the rail that is on screen now.
    const next = async (n: number) => {
      fireEvent.click(await screen.findByTestId('case-replay-next', {}, { timeout: 30000 }))
      await waitFor(
        () => expect(screen.getAllByTestId('case-step')[n - 1]).toHaveAttribute('data-state', 'executed'),
        { timeout: 30000 },
      )
    }
    await next(1) // arrange — the retired Subject stance id, now the Subject surface
    await waitFor(() => expect(ws()).toBe('subject'))
    await next(2) // open_panel target.overview on the subject
    // THE PIN HOOK on a bound tile: the subject the tile reads is the pin.
    await waitFor(() => expect(consultScopePin()).toMatchObject({ kind: 'target', id: LIVE_CASE_SUBJECT }))
    await next(3) // set_scope
    await next(4) // open_panel target.situations — same subject, same tab group
    await next(5) // focus the situation

    // The CaseRail's outcomes per action, and the seal.
    expect(screen.getAllByTestId('case-step').map((li) => li.getAttribute('data-state'))).toEqual([
      'executed',
      'executed',
      'executed',
      'executed',
      'executed',
    ])
    await waitFor(() => expect(screen.getByTestId('case-status')).toHaveTextContent('sealed'))
    expect(screen.getByTestId('case-summary')).toHaveTextContent(LIVE_CASE.summary)

    // The dock's end state.
    expect(ws()).toBe('subject')
    const ids = dock()
    // UI-B3 + U-6: the recorded `target.overview` and `target.situations`
    // opens (ids as the case recorded them) land on ONE bound subject page on
    // the Subject page's STAGE, which follows the later open onto its
    // Situations tab.
    // …and the focused situation was pulled up onto the stage after it, as the
    // Record, with the subject's page one crumb behind.
    const trail = (useFrame.getState().stages.subject?.stack ?? []).map(stageInstanceId)
    expect(trail).toContain(`subject.page:${LIVE_CASE_SUBJECT}`)
    expect(trail.filter((i) => i.endsWith(`:${LIVE_CASE_SUBJECT}`))).toEqual([`subject.page:${LIVE_CASE_SUBJECT}`])
    expect(ids.filter((i) => i.endsWith(`:${LIVE_CASE_SUBJECT}`))).toEqual([])
    expect(usePanelParamsStore.getState().byKind[`subject.page:${LIVE_CASE_SUBJECT}#tab`]?.params.tab).toBe('situations')
    // Consult is in the frame, never a tile; the page is on the stage.
    expect(screen.getByTestId('consult-dock')).toBeInTheDocument()
    expect(stageView()).toBe(`record:situation:${LIVE_CASE_SITUATION}`)
    expect(groups().some((g) => g.includes(`subject.page:${LIVE_CASE_SUBJECT}`))).toBe(false)
    expect(useScope.getState().scope).toMatchObject({ kind: 'target', id: LIVE_CASE_SUBJECT, origin: 'case' })
    expect(useSelection.getState().selection).toMatchObject({ kind: 'situation', id: LIVE_CASE_SITUATION })
    // No clock step in this case: the case never drove the shared window —
    // its aperture is the reader's own (the live window keeps its span).
    expect(casePlayer().clockDrive).toBeNull()
    expect(useWorldState.getState().windowEndMs - useWorldState.getState().windowStartMs).toBe(before.end - before.start)
    // The pin followed the reader to the focused situation (U-6: the record on
    // the stage claims it while in front), and Consult carries it.
    await waitFor(() => expect(consultScopePin()).toMatchObject({ kind: 'situation', id: LIVE_CASE_SITUATION }))
    // A replay sends no consult POST and makes no tool call.
    expect(posts.filter((p) => p.url.includes('/consult'))).toEqual([])
  }, 90000)
})

describe('the STOP path', () => {
  it('a replay stopped after step 2 runs nothing after the latch', async () => {
    await bootOnCase(LIVE_CASE_SESSION_ID, LIVE_CASE_TURN_ID, LIVE_CASE_SESSION)
    await waitFor(() => expect(casePlayer().player?.mode).toBe('replay'))
    act(() => {
      casePlayer().stepReplay()
      casePlayer().stepReplay()
    })
    fireEvent.click(await screen.findByTestId('case-stop', {}, { timeout: 30000 }))
    expect(casePlayer().player!.status).toBe('stopped')
    expect(casePlayer().player!.steps.map((s) => s.state)).toEqual([
      'executed',
      'executed',
      'stopped',
      'stopped',
      'stopped',
    ])
    expect(casePlayer().stepReplay()).toBe(false)
    await waitFor(() => expect(dock()).toContain(`subject.page:${LIVE_CASE_SUBJECT}`))
    // Nothing after the latch: the page never moved onto the Situations tab.
    expect(usePanelParamsStore.getState().byKind[`subject.page:${LIVE_CASE_SUBJECT}#tab`]).toBeUndefined()
    expect(useScope.getState().scope).toBeNull()
    expect(useSelection.getState().selection).toBeNull()
    // A replay has no run to stop: no stop route is called.
    expect(posts.filter((p) => p.url.includes('/stop'))).toEqual([])
  }, 90000)

  it('a LIVE case stopped after step 2 tells the stop route, and the landed record stays stopped', async () => {
    render(wrap(<App />))
    await waitFor(() => expect(getShell()).not.toBeNull())
    act(() => {
      drive(openPanel('system.consult'))
    })
    const requestId = LIVE_CASE.request_id
    const frame = (i: number) => ({ type: 'step', phase: 'case', kind: 'case_action', round: 2, ...LIVE_CASE.actions[i] })
    act(() => {
      casePlayer().beginLive(requestId, CONSULT_PANEL_ID)
      casePlayer().ingest(frame(0))
      casePlayer().ingest(frame(1))
    })
    await waitFor(() => expect(dock()).toContain(`subject.page:${LIVE_CASE_SUBJECT}`))
    await act(async () => {
      await casePlayer().stop()
    })
    expect(posts.find((p) => p.url.includes(`/consult/runs/${requestId}/stop`))?.body).toEqual({ executed_through_seq: 2 })
    // A frame that arrives after the latch is shown, never run.
    act(() => {
      casePlayer().ingest(frame(2))
    })
    expect(casePlayer().player!.steps[2]).toMatchObject({ seq: 3, state: 'stopped' })
    // The persisted record the turn lands with (status stopped after 2).
    act(() => {
      casePlayer().landRecord({ ...LIVE_CASE, status: 'stopped', stopped_after_seq: 2, actions: LIVE_CASE.actions.slice(0, 2) })
    })
    expect(casePlayer().player!.status).toBe('stopped')
    expect(useScope.getState().scope).toBeNull()
    expect(dock()).not.toContain(`target.situations:${LIVE_CASE_SUBJECT}`)
  }, 90000)

  it('a stopped case persists as stopped — the real stopped session reads back so on reload', async () => {
    const rec = parseCaseRecord(LIVE_STOPPED_SESSION.turns[1].case)!
    expect(rec).toMatchObject({ status: 'stopped', stopped_after_seq: 3 })
    render(wrap(<App />))
    await waitFor(() => expect(getShell()).not.toBeNull())
    act(() => {
      drive(openPanel('system.consult'))
      useConsultSessions.getState().adoptSession(CONSULT_PANEL_ID, LIVE_STOPPED_SESSION as never)
    })
    const line = await screen.findByTestId('consult-case-line', {}, { timeout: 30000 })
    expect(line).toHaveTextContent('case · stopped · 0 steps · recorded 2026-10-04T04:35:54Z')
    // Nothing to replay past a stop: the control is off.
    expect(within(line).getByTestId('consult-case-replay')).toBeDisabled()
    expect(useConsultSessions.getState().panel(CONSULT_PANEL_ID).sessionId).toBe(LIVE_STOPPED_SESSION_ID)
  }, 90000)
})
