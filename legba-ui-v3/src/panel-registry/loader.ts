/**
 * Panel loader — dispatch from a `PanelRegistration` row to a concrete React
 * component.
 *
 * The runtime registry stores the descriptor-facing `panel_id` (e.g.
 * `"target_overview"`). The frontend bundle stores `PanelKind` (e.g.
 * `"target.overview"`). `resolvePanel` walks one to the other and returns
 * an unbound-placeholder shape when the lookup fails.
 *
 * Unknown `panel_id` is non-fatal: the panel-loader returns a marker that
 * the App renders as an `UnboundPanelPlaceholder` per L-108 §8.
 */

import type { ComponentType } from 'react'
import type { PanelKind, PanelKindDefinition, PanelProps, PanelRegistration } from '@/types'
import { PANEL_ID_TO_KIND, PANEL_REGISTRY } from './registry'

export type ResolvedPanel =
  | {
      ok: true
      kind: PanelKind
      definition: PanelKindDefinition
      Component: ComponentType<PanelProps>
      registration: PanelRegistration
    }
  | { ok: false; reason: 'unknown_panel_id'; panel_id: string; registration: PanelRegistration }

/**
 * Resolve a registration row to a renderable bundle.
 *
 * Strips a leading `panels.` prefix defensively even though the backend
 * normalizes it; harmless in either path.
 */
export function resolvePanel(reg: PanelRegistration): ResolvedPanel {
  const raw = reg.panel_id.trim()
  const normalized = raw.startsWith('panels.') ? raw.slice('panels.'.length) : raw
  const kind = PANEL_ID_TO_KIND[normalized]
  if (!kind) {
    return { ok: false, reason: 'unknown_panel_id', panel_id: normalized, registration: reg }
  }
  const entry = PANEL_REGISTRY[kind]
  return {
    ok: true,
    kind,
    definition: entry.definition,
    Component: entry.Component,
    registration: reg,
  }
}

/** Extract the scope value(s) from a registration's `binding` JSON. */
export function extractScope(reg: PanelRegistration): {
  target_id?: string
  analyst_id?: string
  dashboard_id?: string
} {
  const b = reg.binding ?? {}
  return {
    target_id: typeof b.target_id === 'string' ? b.target_id : undefined,
    analyst_id: typeof b.analyst_id === 'string' ? b.analyst_id : undefined,
    dashboard_id: typeof b.dashboard_id === 'string' ? b.dashboard_id : undefined,
  }
}

/**
 * Build a stable panel-instance id: `<kind>:<scope_value>` per L-108 §3.
 * Returns the kind alone for singletons.
 */
export function instanceId(kind: PanelKind, scope: ReturnType<typeof extractScope>): string {
  const value = scope.target_id ?? scope.analyst_id ?? scope.dashboard_id
  return value ? `${kind}:${value}` : kind
}
