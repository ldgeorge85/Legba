/**
 * caseRunner — "consult presents a case", the case half of the executor
 * (C-1b/ui; UI-B1/shell).
 *
 * The executor itself is `lib/applyAction.ts` — the ONE executor every producer
 * drives the wall through (UI-D1 §3). C-1b's shell port became its
 * implementation: `CaseShell` is `ShellPort`, `registerCaseShell` is
 * `registerShell`, and a case runs with `driver: 'case'`, which keeps the
 * closed vocabulary's client-side refusals (`kind_not_drivable`, an
 * arrangement or a mission the vocabulary does not list) and re-opens the
 * presenting Consult tile after an arrangement.
 *
 * What stays here is what only a CASE needs: the snapshot taken before step 1
 * and the Restore that puts the wall back.
 */

import { useScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'
import type { WorkspaceSnapshot } from '@/lib/caseModel'
import { getShell, registerShell, scopeForAction, type ShellPort } from '@/lib/applyAction'

export {
  applyAction,
  type CaseSideEffects,
  type StepOutcome,
  type WorkstationAction,
} from '@/lib/applyAction'

/** C-1b's name for the shell port — the same object `applyAction` drives. */
export type CaseShell = ShellPort

/** Register the shell's port (one registration serves every driver). */
export const registerCaseShell = registerShell
export const getCaseShell = getShell

/** C-1b's name for the scope projector a case's `set_scope` uses. */
export function scopeForCase(...args: Parameters<typeof scopeForAction>) {
  return scopeForAction(...args)
}

// ---------------------------------------------------------------------------
// Snapshot / Restore (D2).
// ---------------------------------------------------------------------------

/** Capture what a case can move, before step 1. */
export function takeSnapshot(sh: CaseShell | null = getShell(), now = Date.now()): WorkspaceSnapshot {
  const w = useWorldState.getState()
  return {
    takenAt: now,
    scope: useScope.getState().scope,
    selection: useSelection.getState().selection,
    window: { startMs: w.windowStartMs, endMs: w.windowEndMs, spanMs: w.spanMs },
    layers: { ...w.layers },
    stance: sh?.currentStance() ?? null,
    layout: sh?.serializeLayout() ?? null,
  }
}

/**
 * Return the wall to the snapshot: scope, selection, window, layers, layout.
 * Written wholesale — a restore that merged would leave the case's extras lit.
 */
export function restoreSnapshot(snap: WorkspaceSnapshot, sh: CaseShell | null = getShell()): void {
  if (sh && snap.layout != null) sh.restoreLayout(snap.stance, snap.layout)
  useScope.getState().setScope(snap.scope)
  useSelection.getState().select(snap.selection)
  useWorldState.setState({
    windowStartMs: snap.window.startMs,
    windowEndMs: snap.window.endMs,
    spanMs: snap.window.spanMs,
    layers: { ...snap.layers },
  })
}
