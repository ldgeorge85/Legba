/**
 * layoutPresets — the RETIRED preset ids, as a translation table (U-2 shell).
 *
 * The seven named presets, the "Open a workspace…" dropdown and the single
 * Save/Restore slot (`legba_layout_custom`) are gone (02_UI_TARGET_BRIEF §3,
 * option A): a reader arranges a surface's regions directly and keeps a view
 * as a LINK. What remains is what still names a preset — recorded consult
 * cases (`arrange {layout_type: 'preset'}`) and the consult vocabulary
 * (`workstationVocabulary.json`, whose server-side drift test reads these ids
 * and seeds) — and the surface each one now opens
 * ({@link legacyPresetSurface}). `seeds` is data for that contract; the shell
 * never lays it out, and it never reads the old Save/Restore slot.
 */
import type { PanelKind } from '@/types'
import type { SurfaceId } from '@/lib/surfaces'

export interface LegacyPreset {
  id: string
  /** The surface the id now opens. */
  surface: SurfaceId
  /** The tiles it seeded when recorded cases were made against it (data only). */
  seeds: { kind: PanelKind }[]
}

export const LEGACY_PRESETS: readonly LegacyPreset[] = [
  {
    id: 'wall',
    surface: 'board',
    seeds: [{ kind: 'system.wall' }, { kind: 'system.inspector' }],
  },
  {
    id: 'monitoring',
    surface: 'investigate',
    seeds: [{ kind: 'system.findings' }, { kind: 'system.inspector' }, { kind: 'system.registry' }, { kind: 'system.alerts_watches' }],
  },
  {
    id: 'workspace',
    surface: 'investigate',
    seeds: [{ kind: 'system.findings' }, { kind: 'system.inspector' }, { kind: 'system.consult' }, { kind: 'system.provenance' }],
  },
  {
    id: 'investigation',
    surface: 'investigate',
    seeds: [{ kind: 'system.findings' }, { kind: 'system.inspector' }, { kind: 'system.entities' }, { kind: 'system.search' }],
  },
  {
    id: 'analysis',
    surface: 'evaluation',
    seeds: [{ kind: 'system.optimizer' }, { kind: 'system.eval_scorecard' }, { kind: 'system.consult' }, { kind: 'system.layer_divergence' }],
  },
  {
    id: 'operations',
    surface: 'engine',
    seeds: [{ kind: 'system.status' }, { kind: 'system.ledgers' }],
  },
  {
    id: 'focus',
    surface: 'board',
    seeds: [{ kind: 'system.inspector' }],
  },
]

/** Whether a string is one of the seven retired preset ids. */
export function isLegacyPresetId(id: string): boolean {
  return LEGACY_PRESETS.some((p) => p.id === id)
}

/** The surface a retired preset id now opens; null for any other word. */
export function legacyPresetSurface(id: string): SurfaceId | null {
  return LEGACY_PRESETS.find((p) => p.id === id)?.surface ?? null
}
