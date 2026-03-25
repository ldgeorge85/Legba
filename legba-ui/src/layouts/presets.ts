import type { PanelType } from '@/stores/workspace'

export interface LayoutPreset {
  name: string
  label: string
  panels: PanelType[]
}

export const LAYOUT_PRESETS: LayoutPreset[] = [
  {
    name: 'monitoring',
    label: 'Monitoring',
    panels: ['dashboard', 'event-stream', 'map', 'situations'],
  },
  {
    name: 'investigation',
    label: 'Investigation',
    panels: ['events', 'entity-detail', 'graph', 'timeline'],
  },
  {
    name: 'analysis',
    label: 'Analysis',
    panels: ['hypotheses', 'consult', 'facts', 'analytics'],
  },
  {
    name: 'production',
    label: 'Production',
    panels: ['briefs', 'reports', 'situations'],
  },
  {
    name: 'operations',
    label: 'Operations',
    panels: ['goals', 'sources', 'cycle-monitor', 'scorecard'],
  },
]

const CUSTOM_LAYOUTS_KEY = 'legba-workspace-custom-layouts'

export interface CustomLayout {
  name: string
  layoutJSON: string
}

export function loadCustomLayouts(): Record<string, string> {
  try {
    const raw = localStorage.getItem(CUSTOM_LAYOUTS_KEY)
    if (raw) return JSON.parse(raw)
  } catch { /* ignore corrupt data */ }
  return {}
}

export function saveCustomLayout(name: string, layoutJSON: string): void {
  const layouts = loadCustomLayouts()
  layouts[name] = layoutJSON
  localStorage.setItem(CUSTOM_LAYOUTS_KEY, JSON.stringify(layouts))
}

export function deleteCustomLayout(name: string): void {
  const layouts = loadCustomLayouts()
  delete layouts[name]
  localStorage.setItem(CUSTOM_LAYOUTS_KEY, JSON.stringify(layouts))
}
