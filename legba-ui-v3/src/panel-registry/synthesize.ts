/**
 * Bound-panel synthesis — sidebar reachability for binding-scoped panels
 * without `ui_panel_registrations` rows.
 *
 * The live system carries ZERO `ui_panel_registrations` rows (no descriptor
 * declares `outputs.ui_panel`), so the sidebar's Targets/Analysts groups —
 * built solely from `GET /registry/ui_panels` — never rendered, leaving every
 * bound panel kind (`target.map`, `target.timeline`, `analyst.runs`, …)
 * reachable only through the palette's Investigate grids.
 *
 * This module extends the synthetic-registration pattern the shell already
 * uses for singletons (App.tsx `addSingleton`) and Investigate grids
 * (`addBound`) to the registry list itself: given the live descriptor heads
 * (`GET /registry/descriptors?family=target|analyst&head_only=true` — the same
 * FROZEN routes `usePaletteRecords` reads), it mints one `PanelRegistration`
 * per (record × bound panel kind), so `useRegistry` can hand the Sidebar a
 * complete per-target / per-analyst panel set.
 *
 * Real registry rows stay authoritative: `mergeRegistrations` drops any
 * synthetic row whose panel instance (`instanceId(kind, scope)`) is already
 * covered by a live, non-retired registration.
 *
 * Everything here is pure + DOM-free (unit-tested in synthesize.test.ts).
 */

import type { Mode, PanelKind, PanelRegistration } from '@/types'
import { PANEL_ID_TO_KIND, PANEL_REGISTRY } from './registry'
import { extractScope, instanceId } from './loader'
import type { RegistryEntry } from './registry'

/** One descriptor head from `GET /registry/descriptors?family=…&head_only=true`. */
export interface RecordDescriptor {
  descriptor_id: string
  name?: string | null
  state?: string | null
}

/** Binding-scoped panel kinds for one category, in registry declaration order
 *  (the registry declares them in semantic order — Overview first). */
function boundKindsFor(category: 'target' | 'analyst'): PanelKind[] {
  const out: PanelKind[] = []
  for (const [kind, entry] of Object.entries(PANEL_REGISTRY) as Array<[PanelKind, RegistryEntry]>) {
    if (!entry.definition.requiresBinding) continue
    if (entry.definition.hidden) continue
    if (entry.definition.category !== category) continue
    out.push(kind)
  }
  return out
}

/** The per-target panel set (T1–T10 minus any hidden kinds). */
export const TARGET_BOUND_KINDS: readonly PanelKind[] = boundKindsFor('target')

/** The per-analyst panel set (A1–A5 minus any hidden kinds). */
export const ANALYST_BOUND_KINDS: readonly PanelKind[] = boundKindsFor('analyst')

/**
 * Mint the synthetic registration for one bound kind × record — the same
 * shape `App.addBound` builds for an Investigate grid, so a sidebar open and
 * a palette Investigate dedupe onto the same Dockview tile
 * (`instanceId(kind, scope)` = `<kind>:<record_id>` for both).
 */
function syntheticRegistration(
  kind: PanelKind,
  recordId: string,
  axis: 'target' | 'analyst',
  mode: Mode,
): PanelRegistration {
  const def = PANEL_REGISTRY[kind].definition
  return {
    id: `synthetic:${kind}:${recordId}`,
    panel_id: def.panelId,
    descriptor_id: recordId,
    descriptor_version: '00000000',
    descriptor_family: axis,
    analyst_id: axis === 'analyst' ? recordId : null,
    title: `${def.defaultTitle} · ${recordId}`,
    mode,
    layout_slot: kind,
    data_query: {},
    binding: axis === 'analyst' ? { analyst_id: recordId } : { target_id: recordId },
    retired: false,
    created_at: new Date(0).toISOString(),
    retired_at: null,
  }
}

/**
 * Synthesize the full bound-panel registration set from live descriptor heads.
 *
 * Retired records are skipped; panel kinds not shipped in `mode` are skipped
 * (mirrors the `addSingleton`/`addBound` mode gate so the sidebar never lists
 * a panel the opener would refuse).
 */
export function synthesizeBoundRegistrations(
  targets: readonly RecordDescriptor[],
  analysts: readonly RecordDescriptor[],
  mode: Mode,
): PanelRegistration[] {
  const out: PanelRegistration[] = []
  const emit = (records: readonly RecordDescriptor[], kinds: readonly PanelKind[], axis: 'target' | 'analyst') => {
    for (const rec of records) {
      if (!rec.descriptor_id || rec.state === 'retired') continue
      for (const kind of kinds) {
        const def = PANEL_REGISTRY[kind].definition
        if (def.modes.length > 0 && !def.modes.includes(mode)) continue
        out.push(syntheticRegistration(kind, rec.descriptor_id, axis, mode))
      }
    }
  }
  emit(targets, TARGET_BOUND_KINDS, 'target')
  emit(analysts, ANALYST_BOUND_KINDS, 'analyst')
  return out
}

/** Resolve a registration to its panel-instance key, or null for unknown kinds. */
function instanceKeyOf(reg: PanelRegistration): string | null {
  const raw = reg.panel_id.trim()
  const normalized = raw.startsWith('panels.') ? raw.slice('panels.'.length) : raw
  const kind = PANEL_ID_TO_KIND[normalized]
  if (!kind) return null
  return instanceId(kind, extractScope(reg))
}

/**
 * Merge real registry rows with synthesized ones.
 *
 * Real rows come first and win: a synthetic row is dropped when a live,
 * non-retired real registration already covers the same panel instance.
 * Real rows with unknown panel kinds pass through untouched (the loader's
 * unbound-placeholder path owns those).
 */
export function mergeRegistrations(
  real: readonly PanelRegistration[],
  synthetic: readonly PanelRegistration[],
): PanelRegistration[] {
  const covered = new Set<string>()
  for (const reg of real) {
    if (reg.retired) continue
    const key = instanceKeyOf(reg)
    if (key) covered.add(key)
  }
  const out: PanelRegistration[] = [...real]
  for (const reg of synthetic) {
    const key = instanceKeyOf(reg)
    if (key && covered.has(key)) continue
    out.push(reg)
  }
  return out
}
