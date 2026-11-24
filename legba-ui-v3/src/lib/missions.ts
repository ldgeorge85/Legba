/**
 * missions — the RETIRED mission ids, as a translation table (U-2 shell).
 *
 * The four missions (a workspace plus a scope, a window and map layers), the
 * operator's saved missions and the workspace chooser that listed them are
 * gone: a saved view is a LINK now (`lib/address.ts` — surface, scope, point
 * and window in the address bar), not a stored posture.
 *
 * What remains is what still names a mission — `#mission=` links, recorded
 * consult cases (`run_mission`) and the consult vocabulary
 * (`workstationVocabulary.json`, whose server-side drift test reads this
 * file's ids, workspaces and added tiles) — and what running one now does
 * ({@link legacyMission}): open the nearest surface and set the window the
 * mission declared. Scope is left as the reader has it; nothing is invented.
 */
import type { PanelKind } from '@/types'
import type { SurfaceId } from '@/lib/surfaces'
import type { MissionWindow, WorkspaceId } from '@/lib/workspaces'
import { legacyWorkspaceSurface } from '@/lib/workspaces'

/** The four retired mission ids. */
export type MissionId = 'morning_read' | 'desk_watch' | 'crisis' | 'release'

export interface LegacyMission {
  id: MissionId
  /** The retired workspace it mounted (the vocabulary contract). */
  workspace: WorkspaceId
  /** The tiles it added (data for the vocabulary contract). */
  adds: { kind: PanelKind }[]
  /** The window it set — still applied when an old link or case runs it. */
  timeWindow: MissionWindow
  /** Where it lands now, when that differs from its workspace's surface. */
  surface?: SurfaceId
}

const HOUR = 3_600_000
const DAY = 24 * HOUR

export const LEGACY_MISSIONS: readonly LegacyMission[] = [
  {
    id: 'morning_read',
    workspace: 'morning_read',
    adds: [{ kind: 'system.consult' }],
    timeWindow: { spanMs: DAY, label: '24h' },
  },
  {
    id: 'desk_watch',
    workspace: 'desk',
    adds: [{ kind: 'v4.map' }, { kind: 'system.consult' }],
    timeWindow: { spanMs: 7 * DAY, label: '7d' },
  },
  {
    id: 'crisis',
    workspace: 'investigate',
    adds: [{ kind: 'v4.map' }, { kind: 'system.timeline' }, { kind: 'system.consult' }],
    timeWindow: { spanMs: 6 * HOUR, label: '6h' },
  },
  {
    id: 'release',
    workspace: 'trust',
    adds: [{ kind: 'system.eval_scorecard' }, { kind: 'system.consult' }],
    timeWindow: { spanMs: 30 * DAY, label: '30d' },
    surface: 'ledger',
  },
]

/** Whether a string is one of the four retired mission ids. */
export function isMissionId(id: string): id is MissionId {
  return LEGACY_MISSIONS.some((m) => m.id === id)
}

/** A retired mission by id. */
export function legacyMission(id: string): LegacyMission | undefined {
  return LEGACY_MISSIONS.find((m) => m.id === id)
}

/** The surface a retired mission id now opens; null for any other word. */
export function legacyMissionSurface(id: string): SurfaceId | null {
  const m = legacyMission(id)
  if (!m) return null
  return m.surface ?? legacyWorkspaceSurface(m.workspace)
}
