/**
 * Views only an admin is offered. The `＋` catalog and ⌘K leave them out for
 * a caller KNOWN to be below admin (a reader or a contributor); the view
 * itself also refuses to read anything for them, and the registry refuses
 * their requests either way.
 *
 * A leaf: no React, no registry imports, so the catalog, the palette and the
 * view can all read it.
 */
import { RANK_ADMIN, below, type RoleRank } from '@/auth/roles'

export const USERS_VIEW = 'view:users'

export const ADMIN_ONLY_VIEWS: ReadonlySet<string> = new Set([USERS_VIEW])

/** Whether a view is offered to a caller of this rank (an unknown rank is offered, as elsewhere). */
export function offeredToRank(viewId: string, rank: RoleRank | null): boolean {
  return !ADMIN_ONLY_VIEWS.has(viewId) || !below(rank, RANK_ADMIN)
}
