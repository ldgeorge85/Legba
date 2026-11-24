/**
 * UI-B6 — THE PIN HOOK ON EVERY OBJECT SURFACE, one test per surface.
 *
 * Each REAL surface is mounted (fetch stubbed) and the ambient pin read back:
 * the pin is set when the surface reads an object, and released when it reads
 * none — never a stale pin. The bound per-subject / per-unit tiles are proven
 * on the mounted shell (`App.caseReplay.test.tsx`), because their call site is
 * the shell's panel component.
 *
 *   rail      — a subject / unit / source picked; any other selection releases
 *   map       — the subject the camera fits; one it cannot place pins nothing
 *   timeline  — the record it holds selected; an entity releases
 *   register  — a situation on the Situations tab; Narratives releases
 *   accuracy  — the whole roster while mounted; unmount releases
 *   consult   — the census line reports the pin a question was asked under
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

vi.mock('@/v4/world/MapLibreWorldMap', () => ({ default: () => <div data-testid="stub-maplibre" /> }))
vi.mock('@/v4/globe/GlobeMap', () => ({ default: () => <div data-testid="stub-globe" /> }))
vi.mock('@/v4/globe/SvgWorld', () => ({ default: () => <div data-testid="stub-drawn" /> }))
vi.mock('@/lib/mapEngine', () => ({ mapEngine: () => 'maplibre', hasWebGL2: () => true }))
vi.mock('@/components/TileWebGLOverlay', () => ({
  TileWebGLOverlay: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}))

import { ObjectRail } from '@/components/ObjectRail'
import MapPanel from '@/panels/v4/MapPanel'
import TimelineMerged from '@/panels/merged/Timeline'
import RegisterMerged from '@/panels/merged/Register'
import EvalScorecardPanel from '@/panels/system/EvalScorecard'
import ConsultPanel from '@/panels/system/Consult'
import {
  resetAmbientPins,
  resolveAmbientPin,
  SCOPE_PIN_ORIGIN,
  useAmbientPins,
} from '@/lib/consultContext'
import { useConsultSessions } from '@/state/consultSession'
import { resetScope, useScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import type { PanelRegistration } from '@/types'

function reg(kind: string): PanelRegistration {
  return {
    id: `singleton:${kind}`,
    panel_id: kind.replace('.', '_'),
    descriptor_id: '(singleton)',
    descriptor_version: '00000000',
    descriptor_family: 'target',
    analyst_id: null,
    title: kind,
    mode: 'personal',
    layout_slot: kind,
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-10-04T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

/** The registry's target listing — one subject the gazetteer can place, one it cannot. */
const TARGETS = [
  { descriptor_id: 'country_g20_br', state: 'active', body: { scope: { geo: ['BR'] } } },
  { descriptor_id: 'lane_nowhere', state: 'active', body: { scope: { tags: ['lane'] } } },
]

let consultAnswer: Record<string, unknown> | null = null
let lastConsultBody: Record<string, unknown> | null = null

beforeEach(() => {
  resetScope()
  resetAmbientPins()
  useSelection.getState().clear()
  localStorage.clear()
  sessionStorage.clear()
  consultAnswer = null
  lastConsultBody = null
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      const u = String(url)
      let body: unknown = { data: [], items: [], next_cursor: null, server_now: new Date().toISOString() }
      if ((u.includes('/registry/descriptors') || u.includes('/v3/roster')) && u.includes('family=target')) body = TARGETS
      else if (u.includes('/registry/')) body = []
      if (u.includes('/consult') && init?.method === 'POST' && consultAnswer) {
        lastConsultBody = JSON.parse(String(init.body))
        body = consultAnswer
      }
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as unknown as Response
    }),
  )
  vi.stubGlobal(
    'EventSource',
    class {
      onmessage = null
      onerror = null
      addEventListener() {}
      close() {}
    },
  )
})

afterEach(() => {
  cleanup()
  resetAmbientPins()
  useConsultSessions.getState().reset('singleton:system.consult')
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const pinNow = () => resolveAmbientPin(useAmbientPins.getState().claims, useScope.getState().scope)
const claimsOf = (surface: string) => useAmbientPins.getState().claims.filter((c) => c.surface === surface)

describe('the rail', () => {
  it('pins a subject, a unit and a source picked from it; any other selection releases the claim', async () => {
    render(wrap(<ObjectRail mode="personal" />))
    for (const [kind, id] of [
      ['target', 'country_g20_br'],
      ['analyst', 'energy_analyst'],
      ['source', 'src_reuters'],
    ] as const) {
      act(() => useSelection.getState().select({ kind, id, origin: 'rail' }))
      await waitFor(() => expect(claimsOf('rail').map((c) => c.object)).toEqual([{ kind, id, label: null }]))
    }
    act(() => useSelection.getState().select({ kind: 'finding', id: 'f1', origin: 'feed' }))
    await waitFor(() => expect(claimsOf('rail')).toEqual([]))
  })
})

describe('the map', () => {
  it('pins the subject the camera fits; a subject it cannot place pins nothing', async () => {
    render(wrap(<MapPanel />))
    act(() => useSelection.getState().select({ kind: 'target', id: 'country_g20_br' }))
    await waitFor(() => expect(claimsOf('map').map((c) => c.object.id)).toEqual(['country_g20_br']))
    expect(pinNow()).toMatchObject({ pin: { kind: 'target', id: 'country_g20_br' } })
    act(() => useSelection.getState().select({ kind: 'target', id: 'lane_nowhere' }))
    await waitFor(() => expect(claimsOf('map')).toEqual([]))
    act(() => useSelection.getState().clear())
    expect(claimsOf('map')).toEqual([])
  })
})

describe('the Timeline', () => {
  it('pins the record it holds selected; a selection it does not place releases the claim', async () => {
    render(wrap(<TimelineMerged registration={reg('system.timeline')} scope={{}} mode="personal" />))
    act(() => useSelection.getState().select({ kind: 'event', id: 'ev-1', label: 'Port strike', origin: 'timeline' }))
    await waitFor(() => expect(pinNow()).toMatchObject({ surface: 'timeline', pin: { kind: 'event', id: 'ev-1' } }))
    act(() => useSelection.getState().select({ kind: 'entity', id: 'Acme', origin: 'entities' }))
    await waitFor(() => expect(claimsOf('timeline')).toEqual([]))
  })
})

describe('the Register', () => {
  it('pins the situation picked on its Situations tab; the Narratives tab pins nothing', async () => {
    render(wrap(<RegisterMerged registration={reg('system.register')} scope={{}} mode="personal" />))
    act(() => useSelection.getState().select({ kind: 'situation', id: 'sit-1', label: 'Strait closure' }))
    await waitFor(() => expect(pinNow()).toMatchObject({ surface: 'register', pin: { kind: 'situation', id: 'sit-1' } }))
    fireEvent.click(screen.getByTestId('register-tab-narratives'))
    await waitFor(() => expect(claimsOf('register')).toEqual([]))
  })
})

describe('the Accuracy page', () => {
  it('pins the whole roster while it is on the wall, and lets go when it leaves', async () => {
    const page = render(wrap(<EvalScorecardPanel registration={reg('system.eval_scorecard')} scope={{}} mode="personal" />))
    await waitFor(() => expect(pinNow()).toMatchObject({ surface: 'accuracy', pin: { kind: 'roster', id: 'roster' } }))
    page.unmount()
    expect(claimsOf('accuracy')).toEqual([])
    expect(pinNow()).toBeNull()
  })
})

describe('Consult follows the reader, and the census reports the pin', () => {
  it("the tile's ambient pin is the newest surface's object; the answer's line names it", async () => {
    consultAnswer = { answer: 'ok', steps: [], tool_calls: [], cited_refs: [], session_id: 's1' }
    render(
      wrap(
        <>
          <ObjectRail mode="personal" />
          <ConsultPanel registration={reg('system.consult')} scope={{}} mode="personal" />
        </>,
      ),
    )
    act(() => useSelection.getState().select({ kind: 'analyst', id: 'energy_analyst', label: 'Energy', origin: 'rail' }))
    await waitFor(() => {
      const pins = useConsultSessions.getState().panel('singleton:system.consult').pins
      expect(pins.filter((p) => p.origin === SCOPE_PIN_ORIGIN).map((p) => `${p.kind}:${p.id}`)).toEqual([
        'analyst:energy_analyst',
      ])
    })
    fireEvent.change(screen.getByTestId('consult-question'), { target: { value: 'what does this unit read?' } })
    fireEvent.click(screen.getByTestId('consult-submit'))
    await waitFor(() => expect(screen.getByTestId('consult-turn-pin')).toHaveTextContent('pinned: analyst · Energy (rail)'))
    expect(lastConsultBody?.pinned_context).toEqual([
      expect.objectContaining({ kind: 'analyst', id: 'energy_analyst' }),
    ])
    expect(String(lastConsultBody?.question)).toContain('[Pinned analyst: "Energy" (id=energy_analyst)]')
  })
})
