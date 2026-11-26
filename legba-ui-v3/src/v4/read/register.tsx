/**
 * The Board's stage view, registered with the frame: its contract is declared
 * with the other reader views in `panel-registry/contracts.ts`; this module
 * attaches the component, lazily, so the frame renders it through `ViewHost`
 * like any registered view.
 *
 * Imported once, for its side effect, by the shell (`App.tsx`).
 */
import { lazy, Suspense } from 'react'
import { declaredContract, registerPanel } from '@/panel-registry/contracts'
import { PanelLoading } from '@/components/PanelLoading'
import { BOARD_LIST_VIEW } from '@/lib/surfaces'

const BoardList = lazy(() => import('./BoardList'))

function BoardListView() {
  return (
    <Suspense fallback={<PanelLoading />}>
      <BoardList />
    </Suspense>
  )
}

let done = false

/** Attach the component to the declared contract (idempotent). */
export function registerBoardViews(): void {
  if (done) return
  done = true
  const contract = declaredContract(BOARD_LIST_VIEW)
  if (!contract) throw new Error(`the Board view ${BOARD_LIST_VIEW} has no contract in panel-registry/contracts.ts`)
  registerPanel(contract, BoardListView)
}

registerBoardViews()
