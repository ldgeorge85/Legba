/**
 * The Watch surface's three views, registered with the frame (U-9): their
 * CONTRACTS are declared with every other reader view in
 * `panel-registry/contracts.ts` (the panel-action catalogue is generated from
 * that file); this module adds each view's component, lazily, so the frame
 * renders it through `ViewHost` like any registered view, and installs the
 * views' action handlers and undo part (`state.ts`).
 *
 * Imported once, for its side effect, by the shell (`App.tsx`).
 */
import { lazy, Suspense, type ComponentType } from 'react'
import { declaredContract, registerPanel, type FrameViewComponent } from '@/panel-registry/contracts'
import { PanelLoading } from '@/components/PanelLoading'
import { installWatchContract, WATCH_ALERTS_VIEW, WATCH_EDITS_VIEW, WATCH_SILENCES_VIEW } from './state'

const AlertLog = lazy(() => import('./AlertLog'))
const Silences = lazy(() => import('./Silences'))
const SourceEdits = lazy(() => import('./SourceEdits'))

function hosted(C: ComponentType): FrameViewComponent {
  return function WatchView() {
    return (
      <Suspense fallback={<PanelLoading />}>
        <C />
      </Suspense>
    )
  }
}

let done = false

/** Attach the components to the declared contracts (idempotent). */
export function registerWatchViews(): void {
  if (done) return
  done = true
  installWatchContract()
  for (const [id, C] of [
    [WATCH_ALERTS_VIEW, AlertLog],
    [WATCH_SILENCES_VIEW, Silences],
    [WATCH_EDITS_VIEW, SourceEdits],
  ] as const) {
    const contract = declaredContract(id)
    if (!contract) throw new Error(`the Watch view ${id} has no contract in panel-registry/contracts.ts`)
    registerPanel(contract, hosted(C))
  }
}

registerWatchViews()
