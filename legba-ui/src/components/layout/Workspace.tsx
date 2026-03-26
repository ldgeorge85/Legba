import { useEffect, useRef, useCallback } from 'react'
import { useSelectionStore } from '@/stores/selection'
import {
  DockviewReact,
  type DockviewReadyEvent,
  type IDockviewPanelProps,
  type DockviewApi,
} from 'dockview-react'
import { useWorkspaceStore, type PanelType } from '@/stores/workspace'
import { ErrorBoundary } from '@/components/common/ErrorBoundary'
import { DashboardPanel } from '@/panels/DashboardPanel'
import { SignalsPanel } from '@/panels/SignalsPanel'
import { EventsPanel } from '@/panels/EventsPanel'
import { EventDetailPanel } from '@/panels/EventDetailPanel'
import { EntitiesPanel } from '@/panels/EntitiesPanel'
import { EntityDetailPanel } from '@/panels/EntityDetailPanel'
import { SourcesPanel } from '@/panels/SourcesPanel'
import { GoalsPanel } from '@/panels/GoalsPanel'
import { GraphPanel } from '@/panels/GraphPanel'
import { MapPanel } from '@/panels/MapPanel'
import { ConsultPanel } from '@/panels/ConsultPanel'
import { SituationsPanel } from '@/panels/SituationsPanel'
import { WatchlistPanel } from '@/panels/WatchlistPanel'
import { AnalyticsPanel } from '@/panels/AnalyticsPanel'
import { CycleMonitorPanel } from '@/panels/CycleMonitorPanel'
import { JournalPanel } from '@/panels/JournalPanel'
import { EventStreamPanel } from '@/panels/EventStreamPanel'
import { TimelinePanel } from '@/panels/TimelinePanel'
import { FactsPanel } from '@/panels/FactsPanel'
import { ReportsPanel } from '@/panels/ReportsPanel'
import { ScorecardPanel } from '@/panels/ScorecardPanel'
import { ProposedEdgesPanel } from '@/panels/ProposedEdgesPanel'
import { HypothesesPanel } from '@/panels/HypothesesPanel'
import { BriefsPanel } from '@/panels/BriefsPanel'
import { ConfigPanel } from '@/panels/ConfigPanel'
import { InboxPanel } from '@/panels/InboxPanel'
import { UsersPanel } from '@/panels/UsersPanel'

const PANEL_TITLES: Record<PanelType, string> = {
  dashboard: 'Dashboard',
  signals: 'Signals',
  events: 'Events',
  'event-detail': 'Event Detail',
  entities: 'Entities',
  'entity-detail': 'Entity Detail',
  sources: 'Sources',
  goals: 'Goals',
  graph: 'Knowledge Graph',
  map: 'Geospatial Map',
  timeline: 'Timeline',
  'event-stream': 'Live Feed',
  consult: 'Consult',
  situations: 'Situations',
  watchlist: 'Watchlist',
  analytics: 'Analytics',
  'cycle-monitor': 'Cycle Monitor',
  journal: 'Journal',
  facts: 'Facts',
  reports: 'Reports',
  scorecard: 'Scorecard',
  'proposed-edges': 'Proposed Edges',
  hypotheses: 'Hypotheses',
  briefs: 'Situation Briefs',
  inbox: 'Inbox',
  config: 'Config Editor',
  users: 'Users',
}

function PanelContent({ type, params }: { type: PanelType; params?: Record<string, string> }) {
  switch (type) {
    case 'dashboard': return <DashboardPanel />
    case 'signals': return <SignalsPanel />
    case 'events': return <EventsPanel />
    case 'event-detail': return <EventDetailPanel eventId={params?.id ?? null} />
    case 'entities': return <EntitiesPanel />
    case 'entity-detail': return <EntityDetailPanel entityId={params?.id ?? null} />
    case 'sources': return <SourcesPanel />
    case 'goals': return <GoalsPanel />
    case 'graph': return <GraphPanel />
    case 'map': return <MapPanel />
    case 'timeline': return <TimelinePanel />
    case 'event-stream': return <EventStreamPanel />
    case 'consult': return <ConsultPanel />
    case 'situations': return <SituationsPanel />
    case 'watchlist': return <WatchlistPanel />
    case 'analytics': return <AnalyticsPanel />
    case 'cycle-monitor': return <CycleMonitorPanel />
    case 'journal': return <JournalPanel />
    case 'facts': return <FactsPanel />
    case 'reports': return <ReportsPanel />
    case 'scorecard': return <ScorecardPanel />
    case 'proposed-edges': return <ProposedEdgesPanel />
    case 'hypotheses': return <HypothesesPanel />
    case 'briefs': return <BriefsPanel />
    case 'inbox': return <InboxPanel />
    case 'config': return <ConfigPanel />
    case 'users': return <UsersPanel />
    default: return <div className="p-4 text-muted-foreground">Unknown panel type</div>
  }
}

function DockviewPanel(props: IDockviewPanelProps<{ type: PanelType; params?: Record<string, string> }>) {
  const { type, params } = props.params
  return (
    <ErrorBoundary>
      <div className="h-full overflow-auto bg-background">
        <PanelContent type={type} params={params} />
      </div>
    </ErrorBoundary>
  )
}

const components = {
  panel: DockviewPanel,
}

const STORAGE_KEY = 'legba-workspace-layout'

function saveLayout(api: DockviewApi) {
  try {
    const layout = api.toJSON()
    localStorage.setItem(STORAGE_KEY, JSON.stringify(layout))
  } catch { /* ignore */ }
}

function clearAllPanels(api: DockviewApi) {
  // Remove all panels — iterate a copy since we're mutating
  const panels = [...api.panels]
  for (const panel of panels) {
    panel.api.close()
  }
}

function addPresetPanels(api: DockviewApi, panelTypes: PanelType[]) {
  panelTypes.forEach((type) => {
    api.addPanel({
      id: `${type}-preset`,
      component: 'panel',
      params: { type },
      title: PANEL_TITLES[type] ?? type,
    })
  })
}

export function Workspace() {
  const apiRef = useRef<DockviewApi | null>(null)
  const { pendingPanel, clearPending, pendingPreset, clearPreset, pendingCustomLayout, clearCustomLayout, setDockviewApiRef } =
    useWorkspaceStore()

  const onReady = useCallback((event: DockviewReadyEvent) => {
    apiRef.current = event.api

    // Register the API ref so the store can read layout JSON
    setDockviewApiRef({
      getLayoutJSON: () => {
        try {
          return JSON.stringify(event.api.toJSON())
        } catch {
          return null
        }
      },
    })

    // Try to restore saved layout
    try {
      const saved = localStorage.getItem(STORAGE_KEY)
      if (saved) {
        const layout = JSON.parse(saved)
        event.api.fromJSON(layout)
        // Save on every layout change (tab moves, closes, etc.)
        event.api.onDidLayoutChange(() => saveLayout(event.api))
        // Clear selection when detail panels close
        event.api.onDidRemovePanel((e) => {
          const panelType = e.params?.type as string
          if (panelType === 'entity-detail' || panelType === 'event-detail') {
            const { deselect } = useSelectionStore.getState()
            deselect()
          }
        })
        return
      }
    } catch { /* fall through to default */ }

    // Open default dashboard panel
    event.api.addPanel({
      id: 'dashboard-default',
      component: 'panel',
      params: { type: 'dashboard' as PanelType },
      title: 'Dashboard',
    })

    // Save on every layout change
    event.api.onDidLayoutChange(() => saveLayout(event.api))
    // Clear selection when detail panels close
    event.api.onDidRemovePanel((e) => {
      const panelType = e.params?.type as string
      if (panelType === 'entity-detail' || panelType === 'event-detail') {
        const { deselect } = useSelectionStore.getState()
        deselect()
      }
    })
  }, [setDockviewApiRef])

  // Handle panel open requests from sidebar
  useEffect(() => {
    if (!pendingPanel || !apiRef.current) return

    const { type, params } = pendingPanel
    const id = params?.id ? `${type}-${params.id}` : type

    // Check if panel already exists
    const existing = apiRef.current.panels.find((p) => p.id === id)
    if (existing) {
      existing.api.setActive()
      clearPending()
      return
    }

    apiRef.current.addPanel({
      id,
      component: 'panel',
      params: { type, params },
      title: PANEL_TITLES[type] ?? type,
    })

    clearPending()
  }, [pendingPanel, clearPending])

  // Handle preset application
  useEffect(() => {
    if (!pendingPreset || !apiRef.current) return

    clearAllPanels(apiRef.current)
    addPresetPanels(apiRef.current, pendingPreset.panels)
    saveLayout(apiRef.current)
    clearPreset()
  }, [pendingPreset, clearPreset])

  // Handle custom layout restoration
  useEffect(() => {
    if (!pendingCustomLayout || !apiRef.current) return

    try {
      const layout = JSON.parse(pendingCustomLayout.layoutJSON)
      apiRef.current.fromJSON(layout)
      saveLayout(apiRef.current)
    } catch { /* ignore corrupt layout */ }
    clearCustomLayout()
  }, [pendingCustomLayout, clearCustomLayout])

  return (
    <DockviewReact
      className="dockview-theme-dark flex-1"
      onReady={onReady}
      components={components}
    />
  )
}
