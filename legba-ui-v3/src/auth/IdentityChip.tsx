/**
 * X3-B3 — the identity chip: who the registry says is at this workstation.
 *
 * Reads the same `GET /api/v1/auth/me` the sign-in gate reads (one request):
 *
 *   signed-in person   `<name> · <role>`  + "sign out"
 *   perimeter operator `operator · perimeter` + "sign in as a user"
 *   dev registry       `dev · operator`
 *   not answered yet   `identity —`; unanswerable → `identity unavailable`
 *
 * The role printed is the registry's class (`read` / `reader-own-rows` /
 * `operator`) — the one its route gate enforces — never a client guess.
 */

import { useState } from 'react'
import { LogIn, LogOut, UserRound } from 'lucide-react'
import { cn } from '@/lib/cn'
import { signInRequired, signOut, userLabel } from '@/auth/session'
import { useIdentity } from '@/auth/SignInGate'

function goToSignIn(): void {
  window.location.hash = 'signin'
}

export interface IdentityChipProps {
  className?: string
  /** A bearer token sits in `localStorage.legba_token` (the app sends it on
   *  every request — the chip says so). */
  storedToken?: boolean
}

function StoredTokenMark() {
  return (
    <span
      className="text-ink-3"
      title="a bearer token is stored in this browser (localStorage.legba_token); the app sends it on every request"
      data-testid="identity-stored-token"
    >
      · stored token
    </span>
  )
}

export function IdentityChip({ className, storedToken = false }: IdentityChipProps) {
  const identity = useIdentity()
  const [leaving, setLeaving] = useState(false)
  const answer = identity.data
  const base = cn('inline-flex items-center gap-1.5 text-label', className)

  if (!answer) {
    return (
      <span className={base} data-testid="identity-chip">
        identity —
      </span>
    )
  }
  if (answer.state !== 'signed_in') {
    return (
      <span
        className={base}
        data-testid="identity-chip"
        title={answer.state === 'unavailable' ? answer.message : undefined}
      >
        identity unavailable
      </span>
    )
  }
  const { kind, role, user, actor } = answer.identity
  const signin = signInRequired(answer.identity)

  if (kind === 'user' && user) {
    const onSignOut = async () => {
      setLeaving(true)
      try {
        await signOut()
      } finally {
        // Back to the sign-in screen, not to the perimeter's operator.
        window.location.replace(`${window.location.pathname}#signin`)
      }
    }
    return (
      <span className={base} data-testid="identity-chip" title={`${user.email ?? ''} · ${actor}`}>
        <UserRound className="h-3 w-3 text-ink-3" aria-hidden />
        <span className="text-ink-1" data-testid="identity-name">
          {userLabel(user)}
        </span>
        <span className="text-ink-3">· {role}</span>
        {signin && user.is_owner && (
          <span className="text-ink-3" data-testid="identity-owner">
            · owner
          </span>
        )}
        {storedToken && <StoredTokenMark />}
        <button
          type="button"
          onClick={onSignOut}
          disabled={leaving}
          className="ml-1 inline-flex items-center gap-0.5 text-ink-3 hover:text-ink-1"
          data-testid="identity-sign-out"
        >
          <LogOut className="h-3 w-3" aria-hidden />
          {leaving ? 'signing out…' : 'sign out'}
        </button>
      </span>
    )
  }

  if (signin) {
    // A-1: sign-in is required; a non-person here is a credential stored in
    // this browser (the edge hands out none) — say which, and offer sign-in.
    const what =
      kind === 'admin-token'
        ? 'admin token · break-glass'
        : kind === 'service'
          ? `service${answer.identity.service ? `:${answer.identity.service}` : ''}`
          : `${kind} · ${role}`
    return (
      <span className={base} data-testid="identity-chip" title={actor}>
        <span className="text-ink-2" data-testid="identity-name">
          {what}
        </span>
        {storedToken && <StoredTokenMark />}
        <button
          type="button"
          onClick={goToSignIn}
          className="ml-1 inline-flex items-center gap-0.5 text-ink-3 hover:text-ink-1"
          data-testid="identity-sign-in"
        >
          <LogIn className="h-3 w-3" aria-hidden />
          sign in
        </button>
      </span>
    )
  }

  const via = storedToken ? 'stored token' : 'perimeter'
  const label = kind === 'anonymous' ? 'dev · operator' : `${kind} · ${via}`
  return (
    <span className={base} data-testid="identity-chip" title={actor}>
      <span className="text-ink-2" data-testid="identity-name">
        {label}
      </span>
      {kind === 'operator' && (
        <button
          type="button"
          onClick={goToSignIn}
          className="ml-1 inline-flex items-center gap-0.5 text-ink-3 hover:text-ink-1"
          data-testid="identity-sign-in"
        >
          <LogIn className="h-3 w-3" aria-hidden />
          sign in as a user
        </button>
      )}
    </span>
  )
}
