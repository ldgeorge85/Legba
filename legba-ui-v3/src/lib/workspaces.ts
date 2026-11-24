/**
 * workspaces — the RETIRED workspace ids, as a translation table (U-2 shell).
 *
 * The six workspaces (stances) and their per-mode saved slots are gone: the
 * console is four reader surfaces and four operator sections
 * (`lib/surfaces.ts`). This file keeps ONLY what still names them:
 *
 *   - share links (`#ws=morning_read`), recorded consult cases (`arrange
 *     desk`) and the consult vocabulary (`workstationVocabulary.json`, whose
 *     server-side drift test reads this file's ids and seeds) carry the ids;
 *   - each id resolves to the NEAREST SURFACE ({@link legacyWorkspaceSurface}).
 *
 * `seed` is the list of tiles the workspace seeded when cases were recorded
 * against it — DATA for that contract, never laid out by the shell. Nothing
 * here reads or writes storage; the old `legba_ws` store is never read.
 */
import type { PanelKind } from '@/types'
import type { SurfaceId } from '@/lib/surfaces'

/** The six retired workspace ids (they key old links, cases and the vocabulary). */
export type WorkspaceId = 'morning_read' | 'desk' | 'investigate' | 'trust' | 'gate' | 'engine'

/** A relative time window — the shape the retired missions declared. */
export interface MissionWindow {
  spanMs: number
  label: string
}

export interface LegacyWorkspace {
  id: WorkspaceId
  /** The surface the id now opens. */
  surface: SurfaceId
  /** The tiles it seeded when recorded cases were made against it (data only). */
  seed: { kind: PanelKind }[]
}

export const LEGACY_WORKSPACES: readonly LegacyWorkspace[] = [
  {
    id: 'morning_read',
    surface: 'board',
    seed: [
      { kind: 'v4.morning_read' },
      { kind: 'system.wall' },
      { kind: 'system.consult' },
      { kind: 'system.findings' },
      { kind: 'v4.map' },
    ],
  },
  {
    id: 'desk',
    surface: 'subject',
    seed: [
      { kind: 'system.findings' },
      { kind: 'system.inspector' },
      { kind: 'system.consult' },
      { kind: 'subject.page' },
      { kind: 'system.timeline' },
    ],
  },
  {
    id: 'investigate',
    surface: 'investigate',
    seed: [
      { kind: 'system.findings' },
      { kind: 'system.inspector' },
      { kind: 'system.entities' },
      { kind: 'system.provenance' },
      { kind: 'system.register' },
      { kind: 'system.graph_walk' },
      { kind: 'system.consult' },
    ],
  },
  {
    id: 'trust',
    surface: 'evaluation',
    seed: [
      { kind: 'system.eval_scorecard' },
      { kind: 'system.judge_stats' },
      { kind: 'system.source_health' },
      { kind: 'system.eval_boards' },
    ],
  },
  {
    id: 'gate',
    surface: 'gate',
    seed: [{ kind: 'system.journal_gate' }, { kind: 'system.goldset' }, { kind: 'system.journal' }],
  },
  {
    id: 'engine',
    surface: 'engine',
    seed: [{ kind: 'system.status' }, { kind: 'system.registry' }, { kind: 'system.ledgers' }],
  },
]

/** Whether a string is one of the six retired workspace ids. */
export function isWorkspaceId(id: string): id is WorkspaceId {
  return LEGACY_WORKSPACES.some((w) => w.id === id)
}

/** The surface a retired workspace id now opens; null for any other word. */
export function legacyWorkspaceSurface(id: string): SurfaceId | null {
  return LEGACY_WORKSPACES.find((w) => w.id === id)?.surface ?? null
}
