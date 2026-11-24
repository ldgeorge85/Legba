/**
 * UI-B6 — C-1's nine actions, executed by the shell, AUDITED against the W8
 * shell (the merges, the map's geometry contract, the one clock).
 *
 * Every action runs through the REAL executor (`applyAction`, driver `case`)
 * inside the case player, over a mounted App:
 *
 *  - every vocabulary tab of a drivable kind is a tab its survivor really has;
 *  - `open_panel` on every drivable kind lands a tile (bound kinds on their
 *    subject); a RETIRED id a recorded case still carries lands on its
 *    survivor's tab — and SWITCHES an already-open survivor to it;
 *  - `map` moves layers and the camera (geometry declared here; the refusal
 *    under `geometry: none` is `App.geometryNone.test.tsx`);
 *  - `timeline` moves the ONE shared window and hands the validity Timeline
 *    the case's clock;
 *  - `run_mission` (a retired mission id) lands on its surface with its window,
 *    and REFUSES an id the vocabulary does not list by
 *    name (D7: not drivable by consult until a `case/2`);
 *  - `set_scope`, `focus`, `narrate`, `done` execute.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { App } from '@/App'
import { PreferencesProvider } from '@/components/density/PreferencesProvider'
import { __resetReadTelemetry } from '@/lib/readTelemetry'
import { getShell } from '@/lib/applyAction'
import { CASE_VOCABULARY, parseCaseRecord, type CaseRecord } from '@/lib/caseModel'
import { CONSULT_PANEL_ID } from '@/lib/caseReplay'
import { resolveKind } from '@/panel-registry/aliases'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { ALERTS_WATCHES_TABS } from '@/panels/merged/AlertsWatches'
import { PROVENANCE_TABS } from '@/panels/merged/Provenance'
import { REGISTER_TABS } from '@/panels/merged/Register'
import { TIMELINE_TABS } from '@/panels/merged/Timeline'
import { WALL_TABS } from '@/panels/merged/Wall'
import { SUBJECT_TABS } from '@/lib/subjectPage'
import { casePlayer, resetCasePlayer } from '@/state/casePlayer'
import { useConsultSessions } from '@/state/consultSession'
import { resetScope, useScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'
import { screenIds } from '@/test/frameProbe'
import { resetFrameState } from '@/state/frame'

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

// U-6: what is on screen — the stage's view and the panel side's (a page-sized
// open lands on the stage, a panel-sized one in its home region).
const dock = () => screenIds()
const ws = () => new URLSearchParams(window.location.hash.replace(/^#/, '')).get('ws')

const AT = '2026-10-04T04:31:00Z'
let seq = 0
function step(action: string, args: Record<string, unknown>) {
  seq += 1
  return {
    seq,
    action,
    args,
    at: AT,
    cites: action === 'done' ? [] : [{ kind: 'target', id: 'country_g20_br' }],
    rests_on: action === 'done' ? [] : [{ round: 1, tool: 'list_targets' }],
  }
}
function record(actions: ReturnType<typeof step>[]): CaseRecord {
  return parseCaseRecord({
    version: 'case/1',
    request_id: `req-audit-${seq}`,
    status: 'sealed',
    as_of: '2026-10-04T04:31:12Z',
    plane: 'core',
    vocabulary: 'a'.repeat(64),
    refused: 0,
    stopped_after_seq: null,
    complete: true,
    gap_at_seq: null,
    summary: null,
    // A record's seqs run 1..n (a hole stops the replay there).
    actions: actions.map((a, i) => ({ ...a, seq: i + 1 })),
  })!
}

/** Replay a record to its end through the player; returns each step's outcome. */
function play(rec: CaseRecord): string[] {
  act(() => casePlayer().replay(rec, CONSULT_PANEL_ID))
  let guard = 0
  act(() => {
    while (casePlayer().stepReplay() && guard++ < 80) {
      /* step */
    }
  })
  return casePlayer().player!.steps.map((s) => `${s.action?.action}:${s.state}${s.reason ? ` (${s.reason})` : ''}`)
}

beforeEach(() => {
  seq = 0
  resetFrameState()
  __resetReadTelemetry()
  sessionStorage.clear()
  localStorage.clear()
  window.location.hash = ''
  resetScope()
  resetCasePlayer()
  useSelection.getState().clear()
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string) => {
      // An empty list that also answers the paged shape — every mounted panel
      // reads "nothing yet" whichever shape its route returns.
      const body = String(url).includes('/registry/') ? [] : Object.assign([], { data: [], items: [], next_cursor: null })
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

async function boot() {
  render(wrap(<App />))
  await waitFor(() => expect(getShell()).not.toBeNull())
  await waitFor(() => expect(dock().length).toBeGreaterThan(0))
}

describe('the vocabulary against the W8 shell (pure)', () => {
  const SURVIVOR_TABS: Record<string, readonly { id: string }[]> = {
    'system.alerts_watches': ALERTS_WATCHES_TABS,
    'system.provenance': PROVENANCE_TABS,
    'system.register': REGISTER_TABS,
    'system.timeline': TIMELINE_TABS,
    'system.wall': WALL_TABS,
    'subject.page': SUBJECT_TABS,
  }

  it('every tab the vocabulary lets a case ask for is a tab its survivor really has', () => {
    for (const [kind, tabs] of Object.entries(CASE_VOCABULARY.tabs)) {
      const have = SURVIVOR_TABS[kind]
      expect(have, `${kind} declares tabs the audit does not know`).toBeDefined()
      for (const t of tabs) expect(have.map((x) => x.id), `${kind}#${t}`).toContain(t)
    }
  })

  it('every drivable kind is registered, or resolves onto a registered survivor', () => {
    for (const k of CASE_VOCABULARY.drivableKinds) {
      const r = resolveKind(k)
      expect(r && PANEL_REGISTRY[r.kind], k).toBeTruthy()
    }
  })
})

describe('C-1 actions on the mounted W8 shell, driver case', () => {
  it('open_panel lands every drivable kind in its home (bound kinds on their subject)', async () => {
    await boot()
    const subjectOf: Record<string, { kind: string; id: string }> = {
      target: { kind: 'target', id: 'country_g20_br' },
      analyst: { kind: 'analyst', id: 'energy_analyst' },
    }
    // U-2: an open REPLACES content in its home region and may change the
    // surface, so each kind is opened, then looked for, in turn.
    for (const k of CASE_VOCABULARY.drivableKinds) {
      const sk = CASE_VOCABULARY.subjectKind[k]
      const outcomes = play(record([step('open_panel', { kind: k, params: sk ? { subject: subjectOf[sk] } : {} })]))
      expect(outcomes, k).toEqual(['open_panel:executed'])
      const survivor = resolveKind(k)!.kind
      if (survivor === 'system.consult') {
        expect(screen.getByTestId('consult-dock')).toBeInTheDocument()
        continue
      }
      if (survivor === 'system.inspector') {
        await waitFor(() => expect(screen.getByTestId('record-sheet')).toBeInTheDocument())
        continue
      }
      const expected = sk && k !== 'analysis.cross_framing' ? `${survivor}:${subjectOf[sk].id}` : survivor
      await waitFor(() => expect(dock(), k).toContain(expected))
    }
  }, 90000)

  it('a retired id a recorded case carries lands on its survivor\'s tab, and switches an open survivor', async () => {
    await boot()
    expect(play(record([step('open_panel', { kind: 'system.situations', params: {} })]))).toEqual([
      'open_panel:executed',
    ])
    await waitFor(() => expect(dock()).toContain('system.register'))
    expect(dock()).not.toContain('system.situations')
    await waitFor(() => expect(screen.getByTestId('register-tab-situations')).toHaveAttribute('aria-selected', 'true'), {
      timeout: 10000,
    })
    play(record([step('open_panel', { kind: 'system.narratives', params: {} })]))
    await waitFor(() => expect(screen.getByTestId('register-tab-narratives')).toHaveAttribute('aria-selected', 'true'), {
      timeout: 10000,
    })
    expect(dock().filter((id) => id === 'system.register')).toHaveLength(1)
  }, 90000)

  it('the Timeline and Alerts & Watches follow a tab asked of an ALREADY-OPEN tile (the merges fix)', async () => {
    await boot()
    // X3/D2: the five-tile landing no longer seeds the Timeline, so a case
    // opens it first (on its default tab); then it asks the OPEN tile for its
    // validity tab.
    play(record([step('open_panel', { kind: 'system.timeline' })]))
    await waitFor(() => expect(dock()).toContain('system.timeline'))
    await waitFor(() => expect(screen.getByTestId('timeline-mode-events')).toHaveAttribute('aria-selected', 'true'), {
      timeout: 10000,
    })
    play(record([step('open_panel', { kind: 'system.timeline', params: { tab: 'validity' } })]))
    await waitFor(() => expect(screen.getByTestId('timeline-mode-validity')).toHaveAttribute('aria-selected', 'true'), {
      timeout: 10000,
    })
    play(record([step('open_panel', { kind: 'system.alerts_watches', params: { tab: 'watches' } })]))
    await waitFor(() => expect(screen.getByTestId('alerts-watches-tab-watches')).toHaveAttribute('aria-selected', 'true'), {
      timeout: 10000,
    })
    play(record([step('open_panel', { kind: 'system.alerts_watches', params: { tab: 'deliveries' } })]))
    await waitFor(
      () => expect(screen.getByTestId('alerts-watches-tab-deliveries')).toHaveAttribute('aria-selected', 'true'),
      { timeout: 30000 },
    )
  }, 90000)

  it('map, timeline, set_scope, focus, narrate and done execute; the window is the ONE shared clock', async () => {
    await boot()
    const outcomes = play(
      record([
        step('set_scope', { kind: 'target', id: 'country_g20_br' }),
        step('focus', { ref: { kind: 'finding', id: 'f-77' } }),
        step('map', { view: 'fit_ref', ref: { kind: 'target', id: 'country_g20_br' }, layers: ['events', 'situations'] }),
        step('map', { view: 'reset', ref: null, layers: [] }),
        step('timeline', { start: '2026-09-27T00:00:00Z', end: '2026-10-04T00:00:00Z', span: '7d', at: '2026-10-01T00:00:00Z' }),
        step('narrate', { text: 'What moved.' }),
        step('done', { summary: 'audit' }),
      ]),
    )
    expect(outcomes).toEqual([
      'set_scope:executed',
      'focus:executed',
      'map:executed',
      'map:executed',
      'timeline:executed',
      'narrate:executed',
      'done:executed',
    ])
    expect(useScope.getState().scope).toMatchObject({ kind: 'target', id: 'country_g20_br' })
    // fit_ref moves the camera through the selection — the last focus wins.
    expect(useSelection.getState().selection).toMatchObject({ kind: 'target', id: 'country_g20_br' })
    const w = useWorldState.getState()
    expect(w.layers.events).toBe(true)
    expect(w.layers.situations).toBe(true)
    expect(w.layers.signals).toBe(false)
    expect(w.windowStartMs).toBe(Date.parse('2026-09-27T00:00:00Z'))
    expect(w.windowEndMs).toBe(Date.parse('2026-10-04T00:00:00Z'))
    expect(casePlayer().clockDrive).toEqual({ atMs: Date.parse('2026-10-01T00:00:00Z') })
    expect(casePlayer().cameraResetNonce).toBe(1)
    expect(casePlayer().player!.sealed).toBe(true)
  })

  it('run_mission lands a retired mission on its surface with its window; an unlisted id is refused (D7)', async () => {
    await boot()
    const outcomes = play(
      record([step('run_mission', { name: 'desk_watch' }), step('run_mission', { name: 'user:saved-1' })]),
    )
    expect(outcomes).toEqual(['run_mission:executed', 'run_mission:refused (invalid_arg: mission user:saved-1)'])
    await waitFor(() => expect(ws()).toBe('subject'))
    expect(useWorldState.getState().spanMs).toBe(7 * 24 * 3_600_000)
    // Consult is in the frame: no arrangement takes it away.
    expect(screen.getByTestId('consult-dock')).toBeInTheDocument()
  })
})
