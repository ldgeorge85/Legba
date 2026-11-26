/**
 * M-1 — the desktop Consult's request body, held BYTE-FOR-BYTE.
 *
 * Before the request builder moved out of `Consult.tsx` into `lib/consultCore`,
 * this file was run against the inline `send()` with
 * `LEGBA_RECORD_CONSULT_BODIES=1`, which wrote the exact `JSON.stringify`
 * string each state of the panel POSTed into
 * `__fixtures__/consultRequestBodies.json`. From then on it runs in compare
 * mode: the same UI states must POST the same bytes — key order included.
 *
 * The matrix covers what the panel can put on a turn: the three models, the
 * time band's window (Consult sends the band's absolute ends, whatever its
 * span — the `window` cases set the band), both grounding modes, pins
 * (manual and the ambient scope pin), the on-screen report (inside a frame),
 * "Drive the view" (present_case, which also moves the round cap), "Allow
 * changes this turn", a scope predicate and a round cap, and a follow-up turn
 * carrying history. Time is frozen (Date only) and the request id is fixed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import type { ReactElement } from 'react'
import ConsultPanel from './Consult'
import { consultActions, useConsultSessions } from '@/state/consultSession'
import { resetCasePlayer } from '@/state/casePlayer'
import { useDriveMode } from '@/state/driveMode'
import { resetScope, useScope } from '@/state/scope'
import { scopeFromTarget } from '@/lib/scopeFromReport'
import { useSelection } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'
import { registerFramePort, type FramePort } from '@/lib/panelActions'
import { ConsultFrameProvider } from '@/components/ConsultFrameContext'
import type { PanelRegistration } from '@/types'
import { openConsultMenu } from '@/test/consultMenu'

const PANEL = 'singleton:system.consult'
const FIXTURE = resolve(dirname(new URL(import.meta.url).pathname), '__fixtures__/consultRequestBodies.json')
const RECORD = process.env.LEGBA_RECORD_CONSULT_BODIES === '1'
const NOW = Date.parse('2026-11-14T08:00:00.000Z')
const HOUR = 3_600_000
const BAND_HOURS = { '24h': 24, '168h': 168, '720h': 720 } as const

function reg(): PanelRegistration {
  return {
    id: PANEL,
    panel_id: 'system_consult',
    descriptor_id: '(singleton)',
    descriptor_version: '00000000',
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Consult',
    mode: 'personal',
    layout_slot: 'system.consult',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-10-03T00:00:00Z',
    retired_at: null,
  }
}

class FakeEventSource {
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  constructor(public url: string) {}
  addEventListener() {}
  close() {}
}

const FAKE_PORT: FramePort = {
  surface: () => 'board',
  openView: () => true,
  closeView: () => true,
  focusView: () => true,
  findOpen: () => null,
  views: () => [
    { id: 'v4.morning_read', at: 'stage', front: true },
    { id: 'subject.page:brazil', at: 'p1', front: true },
  ],
  chat: () => ({ placement: 'bottom', collapsed: false }),
  snapshot: () => null,
  restore: () => {},
}

interface Case {
  name: string
  model?: 'core' | 'opus' | 'fable'
  window?: '24h' | '168h' | '720h' | 'band'
  recordOnly?: boolean
  pin?: boolean
  scopePin?: boolean
  frame?: boolean
  drive?: boolean
  writes?: boolean
  scope?: string
  rounds?: number
  followUp?: boolean
  question?: string
}

const CASES: Case[] = [
  { name: 'plain (defaults)' },
  { name: 'model opus', model: 'opus' },
  { name: 'model fable', model: 'fable' },
  { name: 'model core, explicit', model: 'core' },
  { name: 'window 24h', window: '24h' },
  { name: 'window 720h', window: '720h' },
  { name: 'window band', window: 'band' },
  { name: 'record only', recordOnly: true },
  { name: 'record only + band + opus', recordOnly: true, window: 'band', model: 'opus' },
  { name: 'pinned selection', pin: true },
  { name: 'ambient scope pin', scopePin: true },
  { name: 'scope pin + manual pin', scopePin: true, pin: true },
  { name: 'on screen (in a frame)', frame: true },
  { name: 'on screen + scope pin + band', frame: true, scopePin: true, window: 'band' },
  { name: 'drive the view (present_case)', drive: true },
  { name: 'drive + custom rounds', drive: true, rounds: 7 },
  { name: 'allow writes', writes: true },
  { name: 'allow writes + drive + fable', writes: true, drive: true, model: 'fable' },
  { name: 'scope predicate + rounds', scope: '  target.id == "brazil"  ', rounds: 22 },
  { name: 'follow-up with history', followUp: true },
  { name: 'follow-up + pin + frame + record only + 24h', followUp: true, pin: true, frame: true, recordOnly: true, window: '24h' },
  { name: 'unicode + multiline question', question: 'Qué cambió?\n— «Brasil» 🇧🇷 "quoted"' },
  { name: 'everything', model: 'opus', window: 'band', recordOnly: true, pin: true, scopePin: true, frame: true, drive: true, writes: true, scope: 'x == 1', rounds: 12, followUp: true },
]

let fetchMock: ReturnType<typeof vi.fn>
let unregister: (() => void) | null = null

beforeEach(() => {
  vi.restoreAllMocks()
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  localStorage.clear()
  useConsultSessions.setState({ panels: {} })
  resetScope()
  resetCasePlayer()
  useSelection.getState().clear()
  useDriveMode.getState().setOn(false)
  useWorldState.getState().setWindow(NOW - 3 * 24 * HOUR, NOW - 2 * HOUR)
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  vi.stubGlobal('crypto', { ...globalThis.crypto, randomUUID: () => '00000000-0000-0000-0000-000000000abc' })
  let n = 0
  fetchMock = vi.fn().mockImplementation(async () => {
    n += 1
    return {
      ok: true,
      status: 200,
      json: async () => ({
        answer: `answer ${n}`,
        finding_id: null,
        derived_from: [],
        tool_calls: [],
        cited_refs: [],
        session_id: 'sess-1',
      }),
    }
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  unregister?.()
  unregister = null
  vi.useRealTimers()
})

function consultBodies(): string[] {
  return fetchMock.mock.calls
    .filter(([url, init]) => String(url).endsWith('/consult') && (init as RequestInit)?.method === 'POST')
    .map(([, init]) => (init as RequestInit).body as string)
}

async function ask(question: string, expected: number) {
  fireEvent.change(screen.getByTestId('consult-question'), { target: { value: question } })
  fireEvent.click(screen.getByTestId('consult-submit'))
  await waitFor(() => expect(consultBodies().length).toBe(expected))
  await waitFor(() => expect(useConsultSessions.getState().panel(PANEL).pendingTurn).toBeNull())
}

async function bodyFor(c: Case): Promise<string> {
  if (c.model) {
    // an EXPLICIT pick (a stored paid plane counts only with the pick marker)
    localStorage.setItem('legba_consult_model', c.model)
    localStorage.setItem('legba_consult_model_chosen', '1')
  }
  // The window is the time band's: a case's window is the band's span, ending now.
  if (c.window && c.window !== 'band') useWorldState.getState().setWindow(NOW - BAND_HOURS[c.window] * HOUR, NOW)
  if (c.recordOnly) localStorage.setItem('legba_consult_grounding', 'record_only')
  if (c.drive) useDriveMode.getState().setOn(true)
  if (c.frame) unregister = registerFramePort(FAKE_PORT)
  const panel: ReactElement = <ConsultPanel registration={reg()} scope={{}} mode="personal" />
  render(c.frame ? <ConsultFrameProvider value={{ compact: false }}>{panel}</ConsultFrameProvider> : panel)
  if (c.scopePin) act(() => useScope.getState().setScope(scopeFromTarget('country_g20_br', 'Brazil')))
  if (c.pin) {
    act(() =>
      consultActions().addPin(PANEL, {
        kind: 'finding',
        id: 'f-0001',
        label: 'Tanker attacks rise',
        preview: { title: 'Tanker attacks rise', body: 'Three attacks in a week.' },
      } as never),
    )
  }
  if (c.scope !== undefined || c.rounds !== undefined) {
    openConsultMenu()
    if (c.scope !== undefined) fireEvent.change(screen.getByTestId('consult-scope'), { target: { value: c.scope } })
    if (c.rounds !== undefined)
      fireEvent.change(screen.getByTestId('consult-max-rounds'), { target: { value: String(c.rounds) } })
  }
  let expected = 1
  if (c.followUp) {
    await ask('first question', expected)
    expected += 1
    // a case turn moves the round cap only while drive is on; keep it on
  }
  openConsultMenu()
  if (c.writes) fireEvent.click(screen.getByTestId('consult-allow-writes'))
  await ask(c.question ?? 'what moved this week?', expected)
  const all = consultBodies()
  return all[all.length - 1]
}

describe('The desktop Consult POSTs byte-identical request bodies', () => {
  const recorded: Record<string, string> = {}
  const fixture: Record<string, string> = !RECORD && existsSync(FIXTURE) ? JSON.parse(readFileSync(FIXTURE, 'utf8')) : {}

  it('the fixture holds every case in the matrix', () => {
    if (RECORD) return
    expect(Object.keys(fixture).sort()).toEqual(CASES.map((c) => c.name).sort())
  })

  for (const c of CASES) {
    it(c.name, async () => {
      const body = await bodyFor(c)
      if (RECORD) {
        recorded[c.name] = body
        mkdirSync(dirname(FIXTURE), { recursive: true })
        writeFileSync(FIXTURE, JSON.stringify(recorded, null, 2) + '\n')
        return
      }
      expect(body).toBe(fixture[c.name])
    })
  }
})
