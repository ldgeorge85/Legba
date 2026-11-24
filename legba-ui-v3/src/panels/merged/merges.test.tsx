/**
 * UI-B1/merges — every survivor's tabs render their MOUNTED component.
 *
 * Each child module is replaced by a stub that renders `mounted-<name>`
 * (`@/test/panelStub`), so these tests prove the wrapper's contract and
 * nothing else: tab X mounts component Y, the alias tab a retired id carries
 * (`initialTab`) opens on Y, and a tab requested while the tile is already
 * open (`requestPanelTab`, posted by the shell's singleton opener) switches to
 * Y. The children's own behaviour is their own tests' business — they render
 * unmodified.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ComponentType } from 'react'
import type { PanelProps, PanelRegistration } from '@/types'
import { requestPanelTab, resetPanelParams, setPanelParams } from '@/state/panelParams'

const { stub } = vi.hoisted(() => ({
  stub: async (name: string) => (await import('@/test/panelStub')).panelStub(name),
}))

vi.mock('@/panels/system/SystemStatus', () => stub('SystemStatus'))
vi.mock('@/panels/system/ProductionGauge', () => stub('ProductionGauge'))
vi.mock('@/panels/system/DeadLetter', () => stub('DeadLetter'))
vi.mock('@/panels/system/ActorHealth', () => stub('ActorHealth'))
vi.mock('@/panels/system/StreamLag', () => stub('StreamLag'))
vi.mock('@/panels/system/GovernorEvents', () => stub('GovernorEvents'))
vi.mock('@/panels/system/AuditChain', () => stub('AuditChain'))
vi.mock('@/panels/system/Budget', () => stub('Budget'))
vi.mock('@/panels/system/Ledger', () => stub('Ledger'))
vi.mock('@/panels/system/Scorecard', () => stub('Scorecard'))
vi.mock('@/panels/system/SituationTrajectory', () => stub('SituationTrajectory'))
vi.mock('@/panels/system/Narratives', () => stub('Narratives'))
vi.mock('@/panels/system/Optimizer', () => stub('Optimizer'))
vi.mock('@/panels/system/Wall', () => stub('Wall'))
vi.mock('@/panels/system/WallMovers', () => stub('WallMovers'))
vi.mock('@/panels/v4/KpiPanel', () => stub('KpiPanel'))
vi.mock('@/panels/registry/Targets', () => stub('RegistryTargets'))
vi.mock('@/panels/registry/Analysts', () => stub('RegistryAnalysts'))
vi.mock('@/panels/registry/Stack', () => stub('RegistryStack'))
vi.mock('@/panels/registry/ActionPacks', () => stub('RegistryActionPacks'))
vi.mock('@/panels/source/SourceRegistry', () => stub('SourceRegistry'))
vi.mock('@/panels/source/SourceDetail', () => stub('SourceDetail'))
vi.mock('@/panels/source/SubscriptionBuilder', () => stub('SubscriptionBuilder'))
vi.mock('@/panels/source/SubscriptionPolicy', () => stub('SubscriptionPolicy'))
vi.mock('@/panels/source/FanoutExplorer', () => stub('FanoutExplorer'))
vi.mock('@/panels/v4/FlowPanel', () => stub('FlowPanel'))
vi.mock('@/panels/v4/WhyPanel', () => stub('WhyPanel'))
vi.mock('@/panels/system/Lineage', () => stub('Lineage'))
// The diff stub reads its candidate exactly as the real child does — from
// `state/panelParams` under its OWN retired kind — so the wrapper's relay is
// what is under test.
vi.mock('@/panels/system/OptimizerDiff', async () => {
  const React = await import('react')
  const { usePanelOpenParams } = await import('@/state/panelParams')
  return {
    default: function OptimizerDiffStub() {
      const [candidate, setCandidate] = React.useState('')
      usePanelOpenParams('system.optimizer.diff', (p) => setCandidate(String(p.candidate_id ?? '')))
      return React.createElement('div', { 'data-testid': 'mounted-OptimizerDiff', 'data-candidate': candidate })
    },
  }
})

import StatusMerged from './Status'
import LedgersMerged from './Ledgers'
import RegisterMerged from './Register'
import OptimizerMerged from './Optimizer'
import WallMerged from './Wall'
import RegistryMerged from './Registry'
import ProvenanceMerged from './Provenance'

function reg(kind: string): PanelRegistration {
  return {
    id: `singleton:${kind}`, panel_id: kind.replace(/[.]/g, '_'), descriptor_id: '(singleton)',
    descriptor_version: '00000000', descriptor_family: 'target', analyst_id: null, title: kind,
    mode: 'personal', layout_slot: kind, data_query: {}, binding: {}, retired: false,
    created_at: '2026-10-04T00:00:00Z', retired_at: null,
  }
}

interface WrapperSpec {
  kind: string
  Component: ComponentType<PanelProps>
  prefix: string
  /** tab id → the stub(s) it mounts. */
  tabs: Record<string, string[]>
  /** Mounted on every tab (a header band). */
  always?: string[]
}

const SPECS: WrapperSpec[] = [
  {
    kind: 'system.status',
    Component: StatusMerged,
    prefix: 'status-tab',
    tabs: {
      status: ['SystemStatus'],
      production: ['ProductionGauge'],
      failures: ['DeadLetter'],
      actors: ['ActorHealth'],
      queues: ['StreamLag'],
    },
  },
  {
    kind: 'system.ledgers',
    Component: LedgersMerged,
    prefix: 'ledgers-tab',
    // Forecasts + Scorecard left 2026-11-13: one home, `panels/ledger/ForecastLedger`.
    tabs: { governor: ['GovernorEvents'], audit: ['AuditChain'], budget: ['Budget'] },
  },
  {
    kind: 'system.register',
    Component: RegisterMerged,
    prefix: 'register-tab',
    tabs: { situations: ['SituationTrajectory'], narratives: ['Narratives'] },
  },
  {
    kind: 'system.optimizer',
    Component: OptimizerMerged,
    prefix: 'optimizer-tab',
    tabs: { candidates: ['Optimizer'], diff: ['OptimizerDiff'] },
  },
  {
    kind: 'system.wall',
    Component: WallMerged,
    prefix: 'wall-tab',
    tabs: { overview: ['Wall'], movers: ['WallMovers'] },
    always: ['KpiPanel'],
  },
  {
    kind: 'system.registry',
    Component: RegistryMerged,
    prefix: 'registry-tab',
    tabs: {
      subjects: ['RegistryTargets'],
      units: ['RegistryAnalysts'],
      sources: ['SourceRegistry', 'SourceDetail'],
      stack: ['RegistryStack'],
      packs: ['RegistryActionPacks'],
      builder: ['SubscriptionBuilder'],
      policy: ['SubscriptionPolicy'],
      canvas: ['FlowPanel'],
    },
  },
  {
    kind: 'system.provenance',
    Component: ProvenanceMerged,
    prefix: 'provenance-tab',
    tabs: { why: ['WhyPanel'], lineage: ['Lineage'], fanout: ['FanoutExplorer'] },
  },
]

const ALL_STUBS = [...new Set(SPECS.flatMap((s) => [...Object.values(s.tabs).flat(), ...(s.always ?? [])]))]

function expectMounted(names: string[]) {
  for (const n of ALL_STUBS) {
    if (names.includes(n)) expect(screen.getByTestId(`mounted-${n}`), n).toBeInTheDocument()
    else expect(screen.queryByTestId(`mounted-${n}`), `${n} should not be mounted`).not.toBeInTheDocument()
  }
}

beforeEach(() => resetPanelParams())
afterEach(() => cleanup())

describe.each(SPECS)('$kind — every tab renders its mounted component', (spec) => {
  const tabIds = Object.keys(spec.tabs)

  it('the strip carries exactly the declared tabs, first one active by default', () => {
    render(<spec.Component registration={reg(spec.kind)} scope={{}} mode="personal" />)
    for (const id of tabIds) expect(screen.getByTestId(`${spec.prefix}-${id}`)).toBeInTheDocument()
    expect(screen.getByTestId(`${spec.prefix}-${tabIds[0]}`)).toHaveAttribute('aria-selected', 'true')
    expectMounted([...spec.tabs[tabIds[0]], ...(spec.always ?? [])])
  })

  it.each(Object.entries(spec.tabs))('clicking %s mounts %s', (tab, names) => {
    render(<spec.Component registration={reg(spec.kind)} scope={{}} mode="personal" />)
    // Radix Tabs activates on mouseDown (automatic activation), not click.
    fireEvent.mouseDown(screen.getByTestId(`${spec.prefix}-${tab}`))
    fireEvent.click(screen.getByTestId(`${spec.prefix}-${tab}`))
    expect(screen.getByTestId(`${spec.prefix}-${tab}`)).toHaveAttribute('aria-selected', 'true')
    expectMounted([...names, ...(spec.always ?? [])])
  })

  it.each(Object.entries(spec.tabs))('opened on %s (a retired id\'s alias tab) mounts %s', (tab, names) => {
    render(<spec.Component registration={reg(spec.kind)} scope={{}} mode="personal" initialTab={tab} />)
    expectMounted([...names, ...(spec.always ?? [])])
  })

  it('an already-open tile follows a tab the shell requests, and ignores an unknown one', () => {
    render(<spec.Component registration={reg(spec.kind)} scope={{}} mode="personal" />)
    const last = tabIds[tabIds.length - 1]
    act(() => requestPanelTab(spec.kind, last))
    expectMounted([...spec.tabs[last], ...(spec.always ?? [])])
    act(() => requestPanelTab(spec.kind, 'no-such-tab'))
    expectMounted([...spec.tabs[last], ...(spec.always ?? [])])
  })

  it('a stale saved tab falls back to the default, never an empty surface', () => {
    render(<spec.Component registration={reg(spec.kind)} scope={{}} mode="personal" initialTab="flow-legacy" />)
    expectMounted([...spec.tabs[tabIds[0]], ...(spec.always ?? [])])
  })
})

describe('system.ledgers — forecasts have one home elsewhere (UI 2026-11-13)', () => {
  it('carries no Forecasts or Scorecard tab and mounts neither copy; a one-line pointer names the home', () => {
    render(<LedgersMerged registration={reg('system.ledgers')} scope={{}} mode="personal" />)
    expect(screen.queryByTestId('ledgers-tab-forecasts')).toBeNull()
    expect(screen.queryByTestId('ledgers-tab-scorecard')).toBeNull()
    expect(screen.getByTestId('ledgers-forecasts-pointer')).toHaveTextContent('Ledger → Forecasts')
  })

  it.each(['forecasts', 'scorecard'])('an old link to the %s tab falls back to Governor, never an empty surface', (tab) => {
    render(<LedgersMerged registration={reg('system.ledgers')} scope={{}} mode="personal" initialTab={tab} />)
    expectMounted(['GovernorEvents'])
  })
})

describe('system.optimizer — the diff drill rides the survivor', () => {
  it('first open: the candidate written before mount reaches the diff, on the Diff tab', () => {
    setPanelParams('system.optimizer', { candidate_id: 'c-1' })
    render(<OptimizerMerged registration={reg('system.optimizer')} scope={{}} mode="personal" initialTab="diff" />)
    expect(screen.getByTestId('mounted-OptimizerDiff')).toHaveAttribute('data-candidate', 'c-1')
  })

  it('while mounted on Candidates: a new candidate switches to Diff and lands there', () => {
    render(<OptimizerMerged registration={reg('system.optimizer')} scope={{}} mode="personal" />)
    expect(screen.getByTestId('mounted-Optimizer')).toBeInTheDocument()
    act(() => setPanelParams('system.optimizer', { candidate_id: 'c-9' }))
    expect(screen.getByTestId('mounted-OptimizerDiff')).toHaveAttribute('data-candidate', 'c-9')
    expect(screen.getByTestId('optimizer-tab-diff')).toHaveAttribute('aria-selected', 'true')
  })

  it('params without a candidate do not move the tab', () => {
    render(<OptimizerMerged registration={reg('system.optimizer')} scope={{}} mode="personal" />)
    act(() => setPanelParams('system.optimizer', { origin: 'x' }))
    expect(screen.getByTestId('mounted-Optimizer')).toBeInTheDocument()
  })
})
