/**
 * READ TELEMETRY — the BINDING PATH (D2e).
 *
 * The emitter's own unit tests prove it batches and fails silent. They prove
 * nothing about whether the app ever calls it, which is the only property the
 * oracle wager actually depends on. These tests mount the REAL surfaces —
 * `App.tsx`'s own `COMPONENTS` map inside a real `DockviewReact` (the
 * `aliasesRuntime.test.tsx` idiom), the real selection store, the real
 * `CitedProse` chips, the real merged Provenance panel — and assert the events
 * that reach the queue.
 *
 * If a future refactor moves the panel-open chokepoint, swaps the citation
 * chip's click handler, or routes finding opens around the selection store,
 * one of these goes red. That is the entire point: an instrument nobody wired
 * up is worse than no instrument, because it reports zero and looks like
 * evidence.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import {
  DockviewReact,
  type DockviewApi,
  type DockviewReadyEvent,
} from 'dockview-react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { COMPONENTS } from '@/App'
import { __pendingReadEvents, __resetReadTelemetry } from '@/lib/readTelemetry'
import { selectRow, useSelection } from '@/state/selection'
import CitedProse from '@/components/CitedProse'
import ProvenanceMerged from '@/panels/merged/Provenance'
import type { PanelRegistration } from '@/types'

function kindsEmitted(): string[] {
  return __pendingReadEvents().map((e) => e.event_kind)
}

function subjectsFor(kind: string): (string | null | undefined)[] {
  return __pendingReadEvents()
    .filter((e) => e.event_kind === kind)
    .map((e) => e.subject_id)
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

beforeEach(() => {
  __resetReadTelemetry()
  sessionStorage.clear()
  useSelection.getState().clear()
  // Panels self-fetch; nothing here asserts their data, and a rejecting fetch
  // exercises the error paths rather than hanging on a real request.
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => '{}',
    } as unknown as Response),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------
// panel_open — through a real Dockview mounting App.tsx's real component map
// ---------------------------------------------------------------------------

async function mountDock(): Promise<DockviewApi> {
  let api: DockviewApi | null = null
  render(
    wrap(
      <div style={{ width: 1280, height: 800 }}>
        <DockviewReact
          components={COMPONENTS}
          onReady={(ev: DockviewReadyEvent) => {
            api = ev.api
          }}
          className="dockview-theme-abyss h-full"
        />
      </div>,
    ),
  )
  await waitFor(() => expect(api).not.toBeNull())
  return api!
}

/** Mirrors `App.tsx`'s `addSingleton` params shape exactly. */
function addSingleton(api: DockviewApi, kind: string) {
  return api.addPanel({
    id: kind,
    component: 'default',
    title: kind,
    params: { registration: null, singletonKind: kind, mode: 'personal' },
  })
}

describe('panel_open (real Dockview × App.tsx COMPONENTS)', () => {
  it('emits exactly one panel_open per real addPanel, naming the kind', async () => {
    const api = await mountDock()
    addSingleton(api, 'system.inspector')
    await waitFor(() => expect(kindsEmitted()).toContain('panel_open'))
    expect(subjectsFor('panel_open')).toEqual(['system.inspector'])
    expect(kindsEmitted().filter((k) => k === 'panel_open')).toHaveLength(1)
  })

  it('counts two different panels as two opens', async () => {
    const api = await mountDock()
    addSingleton(api, 'system.inspector')
    addSingleton(api, 'system.settings')
    await waitFor(() =>
      expect(kindsEmitted().filter((k) => k === 'panel_open')).toHaveLength(2),
    )
    expect(subjectsFor('panel_open').sort()).toEqual([
      'system.inspector',
      'system.settings',
    ])
  })

  it('resolves a RETIRED kind onto its survivor before counting it', async () => {
    // Train A's alias table folded `system.lineage` into `system.provenance`.
    // A saved layout still naming the retired id must not create a phantom
    // kind in the rollup that no panel corresponds to.
    const api = await mountDock()
    addSingleton(api, 'system.lineage')
    await waitFor(() => expect(kindsEmitted()).toContain('panel_open'))
    expect(subjectsFor('panel_open')).toEqual(['system.provenance'])
  })

  it('emits consult_open alongside panel_open for the consult surface', async () => {
    const api = await mountDock()
    addSingleton(api, 'system.consult')
    await waitFor(() => expect(kindsEmitted()).toContain('consult_open'))
    expect(kindsEmitted()).toContain('panel_open')
  })
})

// ---------------------------------------------------------------------------
// finding_open — through the real selection store
// ---------------------------------------------------------------------------

describe('finding_open (real selection store)', () => {
  it('emits when a finding is selected, carrying its id', () => {
    selectRow('finding', 'f-123', 'Iran escalation unit', { origin: 'live-feed' })
    expect(kindsEmitted()).toEqual(['finding_open'])
    expect(subjectsFor('finding_open')).toEqual(['f-123'])
  })

  it('emits for the bare store action too, not only for selectRow', () => {
    // `RecordLink` and the map/Flow handlers call `select` directly.
    useSelection.getState().select({ kind: 'finding', id: 'f-456' })
    expect(subjectsFor('finding_open')).toEqual(['f-456'])
  })

  it('counts a coerced substrate kind — a hypothesis open IS a finding open', () => {
    // `selectionKindOf` maps hypothesis/prediction/alert/critique onto
    // `finding`; the rollup must see the drill, not lose it to a vocabulary
    // mismatch.
    selectRow('hypothesis', 'h-1')
    expect(subjectsFor('finding_open')).toEqual(['h-1'])
  })

  it('does NOT count a target/source/entity click as a finding open', () => {
    selectRow('target', 't-1')
    selectRow('source', 's-1')
    selectRow('entity', 'e-1')
    expect(kindsEmitted()).toEqual([])
  })

  it('emits nothing when the selection is cleared', () => {
    useSelection.getState().select(null)
    expect(kindsEmitted()).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// citation_drill — through the real CitedProse chip
// ---------------------------------------------------------------------------

const CITED_TEXT = 'Iran closed the Strait of Hormuz [1].'
const CITATIONS = [
  // `marker` is the inline token EXACTLY as it appears in the prose;
  // `signalId` is the deprecated back-compat alias for `refId`.
  {
    marker: '[1]',
    refKind: 'signal' as const,
    refId: 'sig-77',
    signalId: 'sig-77',
    title: 'Reuters wire',
  },
]

describe('citation_drill (real CitedProse chip)', () => {
  it('emits when a chip is clicked, naming the evidence it points at', () => {
    render(<CitedProse text={CITED_TEXT} citations={CITATIONS} />)
    fireEvent.click(screen.getByTestId('citation-chip'))
    expect(kindsEmitted()).toContain('citation_drill')
    expect(subjectsFor('citation_drill')).toEqual(['sig-77'])
  })

  it('still emits when the host passes its own onCiteClick', () => {
    // The Inspector and the report BOTH override the click with a
    // scroll-to-evidence handler. Counting inside `defaultCiteClick` would
    // have made the product's two busiest drill surfaces invisible.
    const own = vi.fn()
    render(<CitedProse text={CITED_TEXT} citations={CITATIONS} onCiteClick={own} />)
    fireEvent.click(screen.getByTestId('citation-chip'))
    expect(own).toHaveBeenCalledTimes(1)
    expect(subjectsFor('citation_drill')).toEqual(['sig-77'])
  })

  it('emits nothing for an UNRESOLVED marker — there is nothing to drill into', () => {
    render(<CitedProse text="A claim with a dangling marker [9]." citations={CITATIONS} />)
    expect(screen.queryByTestId('citation-chip')).toBeNull()
    expect(kindsEmitted()).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// lineage_walk — through the real merged Provenance panel
// ---------------------------------------------------------------------------

function reg(): PanelRegistration {
  return {
    id: 'prov1',
    panel_id: 'system_provenance',
    descriptor_id: '(singleton)',
    descriptor_version: '0'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Provenance',
    mode: 'personal',
    layout_slot: 'main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-08-01T00:00:00Z',
    retired_at: null,
  }
}

describe('lineage_walk (real Provenance panel)', () => {
  it('emits for the tab the panel opens on', () => {
    render(wrap(<ProvenanceMerged registration={reg()} scope={{}} mode="personal" />))
    expect(kindsEmitted()).toContain('lineage_walk')
    expect(subjectsFor('lineage_walk')).toEqual(['why'])
  })

  it('emits again when the operator walks to another surface', async () => {
    render(wrap(<ProvenanceMerged registration={reg()} scope={{}} mode="personal" />))
    fireEvent.click(screen.getByTestId('provenance-tab-lineage'))
    await waitFor(() => expect(subjectsFor('lineage_walk')).toContain('lineage'))
    expect(subjectsFor('lineage_walk')).toEqual(['why', 'lineage'])
  })

  it('does NOT count Trajectory or Narratives — they answer a different question', async () => {
    render(wrap(<ProvenanceMerged registration={reg()} scope={{}} mode="personal" />))
    fireEvent.click(screen.getByTestId('provenance-tab-trajectory'))
    fireEvent.click(screen.getByTestId('provenance-tab-narratives'))
    await waitFor(() => expect(screen.getByTestId('provenance-tab-narratives')).toBeTruthy())
    // Only the opening `why` walk was ever counted.
    expect(subjectsFor('lineage_walk')).toEqual(['why'])
  })

  it('honours a deep-link straight onto a walk surface', () => {
    render(
      wrap(
        <ProvenanceMerged
          registration={reg()}
          scope={{}}
          mode="personal"
          initialTab="flow"
        />,
      ),
    )
    expect(subjectsFor('lineage_walk')).toEqual(['flow'])
  })
})
