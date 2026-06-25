/**
 * Target-scoped "Investigate" layout.
 *
 * The analysis panels (Overview / Map / Findings / Timeline / Graph / Situations)
 * are binding-scoped (`requiresBinding`, scopeKey `target_id`) — they only surface
 * in the sidebar when the runtime registry has per-target `ui_panel_registrations`
 * rows, and even then they sit one expand deep under the target group. As a
 * result the Map et al. were effectively unreachable from a cold workspace.
 *
 * This module gives the operator one action — "Investigate ⟨target⟩" — that
 * clears the workspace and opens a grid of the key analysis panels bound to a
 * chosen target. App.tsx supplies the opener (it owns how a tile mounts and
 * synthesizes the per-target binding); this module just sequences the grid.
 *
 *   +------------------+------------------+
 *   |  Overview        |  Map             |
 *   +------------------+------------------+
 *   |  Findings        |  Timeline        |
 *   +------------------+------------------+
 */

import type { DockviewApi } from 'dockview-react'
import type { PanelKind } from '@/types'
import { instanceId } from '@/panel-registry/loader'

/** The analysis panels the Investigate grid opens (all ship in personal + cis). */
export const INVESTIGATE_PANELS: readonly PanelKind[] = [
  'target.overview',
  'target.map',
  'target.findings',
  'target.timeline',
]

/**
 * Bound-panel opener supplied by App.tsx. `position` references the *bound*
 * panel id (`<kind>:<target_id>`), which App mints via the same `instanceId`
 * used here, so splits resolve against panels opened earlier in the sequence.
 */
export type InvestigateOpener = (
  kind: PanelKind,
  position?: {
    referencePanel: string
    direction: 'right' | 'left' | 'above' | 'below' | 'within'
  },
) => void

/**
 * Clear the workspace and lay out the target-scoped analysis grid.
 *
 * Rebalanced for Move 6 ("the active task gets the room"): Findings — the
 * surface you actually scan while investigating — anchors the canvas, with the
 * Map/Overview/Timeline demoted to a left context rail. App docks the singleton
 * Inspector on the right after this and pins the proportions, so the map no
 * longer eats ~70% of the canvas during an investigation.
 *
 *   +----------------+----------------------------------+
 *   | Overview       |  Findings (active — anchor)       |
 *   +----------------+                                  |
 *   | Map / Timeline |                                  |
 *   +----------------+----------------------------------+
 */
export function applyInvestigateLayout(
  api: DockviewApi,
  targetId: string,
  open: InvestigateOpener,
): void {
  api.clear()
  const boundId = (kind: PanelKind) => instanceId(kind, { target_id: targetId })

  open('target.findings') // anchor — the active scan surface gets the room
  open('target.overview', { referencePanel: boundId('target.findings'), direction: 'left' })
  open('target.map', { referencePanel: boundId('target.overview'), direction: 'below' })
  open('target.timeline', { referencePanel: boundId('target.map'), direction: 'within' })
}

/**
 * Analyst-scoped "Investigate" grid (redesign Move 3b — mirror of the target
 * picker, closing P-B2). The per-analyst panels (Runs / Outputs / Cross-target /
 * Critiques) are binding-scoped on `analyst_id` and otherwise sit one expand deep
 * under the sidebar's Analysts group — unreachable from a cold workspace. This
 * gives one action — "Investigate ⟨analyst⟩" — that opens the bound analyst grid.
 *
 *   +------------------+------------------+
 *   |  Outputs         |  Runs            |
 *   +------------------+------------------+
 *   |  Critic Scores   |  Cross-target    |
 *   +------------------+------------------+
 */
export const INVESTIGATE_ANALYST_PANELS: readonly PanelKind[] = [
  'analyst.outputs',
  'analyst.runs',
  'analyst.critiques',
  'analyst.cross_target',
]

/** Clear the workspace and lay out the analyst-scoped grid. */
export function applyInvestigateAnalystLayout(
  api: DockviewApi,
  analystId: string,
  open: InvestigateOpener,
): void {
  api.clear()
  const boundId = (kind: PanelKind) => instanceId(kind, { analyst_id: analystId })

  open('analyst.outputs') // anchor
  open('analyst.runs', { referencePanel: boundId('analyst.outputs'), direction: 'right' })
  open('analyst.critiques', { referencePanel: boundId('analyst.outputs'), direction: 'below' })
  open('analyst.cross_target', { referencePanel: boundId('analyst.runs'), direction: 'below' })
}
