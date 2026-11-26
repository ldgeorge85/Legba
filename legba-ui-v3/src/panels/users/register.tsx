/**
 * Registers the Users page with the frame: its contract (operator side, on the
 * stage) and its component, lazily. The contract is registered here rather
 * than in `panel-registry/contracts.ts` on purpose: the server's panel-action
 * catalogue is generated from that file, and Consult has no business opening
 * or driving the accounts page.
 *
 * Imported once, for its side effect, by the shell (`App.tsx`).
 */
import { lazy, Suspense } from 'react'
import { registerPanel, type PanelContract } from '@/panel-registry/contracts'
import { PanelLoading } from '@/components/PanelLoading'
import { USERS_VIEW } from '@/auth/adminViews'

const UsersView = lazy(() => import('./UsersView'))

export const USERS_CONTRACT: PanelContract = {
  id: USERS_VIEW,
  name: 'Users',
  description: 'Accounts on this instance: invite people, change roles, disable accounts and end sessions. Admins only.',
  group: 'Operator',
  side: 'operator',
  binds: [],
  size: 'stage',
  home: 'stage',
  // Accounts are the engine's configuration: the page opens on Engine.
  page: 'engine',
  actions: [],
}

function HostedUsers() {
  return (
    <Suspense fallback={<PanelLoading />}>
      <UsersView />
    </Suspense>
  )
}

let done = false

/** Register the view (idempotent). */
export function registerUsersView(): void {
  if (done) return
  done = true
  registerPanel(USERS_CONTRACT, HostedUsers)
}

registerUsersView()
