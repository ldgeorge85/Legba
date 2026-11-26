/**
 * RA-1 — what the signed-in role may do, read from the ONE identity answer
 * (`GET /api/v1/auth/me`, `useIdentity`). The registry enforces every route;
 * this only keeps the console from firing a request the role will be refused
 * (a 403 in normal use) and lets a control say plainly why it is not there.
 *
 * Ranks mirror `legba.shared.auth_signin.ROLE_RANK`: reader 1 < contributor 2
 * < admin 3. The October classes map onto them: `read` / `reader-own-rows`
 * are a reader, `operator` (the bearer, the perimeter, dev) is an admin.
 *
 * NOT KNOWN (`null`): no identity answer yet, or the registry could not say
 * (`unavailable`). The console then behaves as it always did — the gate is
 * the door, not the lock — and the registry still refuses what it refuses.
 */
import { useIdentity } from '@/auth/SignInGate'
import type { Identity, IdentityState } from '@/auth/session'
import { IDENTITY_QUERY_KEY } from '@/auth/session'
import { useContext, useEffect, useReducer } from 'react'
import { QueryClientContext } from '@tanstack/react-query'

export type RoleRank = 1 | 2 | 3
export const RANK_READER: RoleRank = 1
export const RANK_CONTRIBUTOR: RoleRank = 2
export const RANK_ADMIN: RoleRank = 3

const RANK_BY_ROLE: Record<string, RoleRank> = {
  reader: RANK_READER,
  read: RANK_READER,
  'reader-own-rows': RANK_READER,
  contributor: RANK_CONTRIBUTOR,
  admin: RANK_ADMIN,
  operator: RANK_ADMIN,
}

/** The rank of an identity, or null when it names no role this console knows. */
export function rankOf(identity: Pick<Identity, 'role'> | null | undefined): RoleRank | null {
  if (!identity || typeof identity.role !== 'string') return null
  return RANK_BY_ROLE[identity.role] ?? null
}

/** The rank of an identity answer; null unless the registry named a signed-in caller. */
export function rankOfState(state: IdentityState | null | undefined): RoleRank | null {
  return state?.state === 'signed_in' ? rankOf(state.identity) : null
}

/** True when the rank is KNOWN and below `needed` (an unknown rank is never "below"). */
export function below(rank: RoleRank | null, needed: RoleRank): boolean {
  return rank != null && rank < needed
}

/** The signed-in caller's rank (null: not known — behave as before). */
export function useRoleRank(): RoleRank | null {
  const q = useIdentity()
  return rankOfState(q.data)
}

/** True when the caller is KNOWN to be below admin (a reader or a contributor). */
export function useBelowAdmin(): boolean {
  return below(useRoleRank(), RANK_ADMIN)
}

/** True when the caller is KNOWN to be a reader (below contributor). */
export function useBelowContributor(): boolean {
  return below(useRoleRank(), RANK_CONTRIBUTOR)
}

/** The plain reason shown where a control needs a higher role. */
export const NEEDS_ADMIN = 'This needs an admin. Your account can read; an admin changes how the engine runs.'
export const NEEDS_CONTRIBUTOR = 'This needs a contributor or an admin. Your account can read but not change shared work.'
/** Why a reader has no way across to the operator side. */
export const OPERATOR_SIDE_REASON =
  'The operator side (engine, evaluation internals, the gate, investigation) is for contributors and admins. Your account reads the Board, Subject, Ledger and Watch.'

/**
 * True when the caller is KNOWN to be below admin, read PASSIVELY: from the
 * identity answer already in the query cache (the sign-in gate asks for it
 * once per page), never asking for it, and false with no query client at all.
 * For the words a role sees inside components that also render on their own
 * (a badge in a test, a card outside the shell): an unknown role sees what it
 * always saw.
 */
export function useBelowAdminPassive(): boolean {
  const client = useContext(QueryClientContext)
  const [, bump] = useReducer((n: number) => n + 1, 0)
  useEffect(() => {
    if (!client) return
    return client.getQueryCache().subscribe((e) => {
      if (e.query.queryKey[0] === IDENTITY_QUERY_KEY[0] && e.query.queryKey[1] === IDENTITY_QUERY_KEY[1]) bump()
    })
  }, [client])
  const state = client?.getQueryData<IdentityState>(IDENTITY_QUERY_KEY)
  return below(rankOfState(state), RANK_ADMIN)
}
