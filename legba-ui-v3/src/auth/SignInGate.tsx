/**
 * X3-B3 — the App shell's sign-in gate: ONE function, `SignInGate`.
 *
 * Mounted around `<App/>` in `main.tsx`. It decides, from the registry's own
 * answer to `GET /api/v1/auth/me` and the address, which ONE thing renders:
 *
 *   `#invite=<token>`          → the invite-accept screen (whoever is asking);
 *   `#signin`                  → the sign-in screen (a perimeter operator who
 *                                wants to be a person; "Back" returns);
 *   me → 401                   → the sign-in screen (no credential the
 *                                registry accepts: a session that ended, or no
 *                                perimeter in front);
 *   me → any identity          → the shell, unchanged — the perimeter operator
 *                                passes exactly as before;
 *   me → any OTHER failure     → the shell, unchanged. The gate is the door,
 *                                not the lock: every route is enforced by the
 *                                registry, and a registry outage must not lock
 *                                the operator out of the surfaces that say so.
 *   while asking               → a one-line "checking sign-in" frame.
 */

import { useEffect, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  fetchIdentity,
  IDENTITY_QUERY_KEY,
  inviteTokenFromHash,
  wantsSignIn,
} from '@/auth/session'
import { InviteAcceptScreen, SignInScreen } from '@/auth/SignInScreen'

/** The identity read every auth surface shares (one request per page). */
export function useIdentity() {
  return useQuery({
    queryKey: IDENTITY_QUERY_KEY,
    queryFn: fetchIdentity,
    staleTime: 60_000,
    retry: false,
  })
}

function useHash(): string {
  const [hash, setHash] = useState(() => window.location.hash)
  useEffect(() => {
    const on = () => setHash(window.location.hash)
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return hash
}

function leaveSignIn(): void {
  // Drop `#signin` from the address and return to the workstation as it was.
  history.replaceState(null, '', window.location.pathname + window.location.search)
  window.dispatchEvent(new HashChangeEvent('hashchange'))
}

export function SignInGate({ children }: { children: ReactNode }) {
  const hash = useHash()
  const identity = useIdentity()
  const invite = inviteTokenFromHash(hash)

  if (invite) return <InviteAcceptScreen token={invite} />

  const answer = identity.data
  if (wantsSignIn(hash)) {
    const signedIn = answer?.state === 'signed_in'
    return <SignInScreen onCancel={signedIn ? leaveSignIn : undefined} />
  }
  if (identity.isPending) {
    return (
      <div
        className="flex h-full w-full items-center justify-center bg-surf-base text-label text-ink-3"
        data-testid="sign-in-checking"
      >
        checking sign-in…
      </div>
    )
  }
  if (answer?.state === 'signed_out') {
    return <SignInScreen reason="Your session has ended, or this browser is not signed in." />
  }
  return <>{children}</>
}
