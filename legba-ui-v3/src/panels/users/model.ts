/**
 * The Users page's words and decisions, pure: what each role may do, the one
 * sentence each confirmation states, the full invite link, and which actions a
 * row offers. The registry enforces every rule; this only keeps the page from
 * offering what it would refuse and says plainly why.
 */
import type { AccountRow, UserRole } from './api'

export const ROLES: readonly UserRole[] = ['reader', 'contributor', 'admin']

export const ROLE_LABEL: Record<UserRole, string> = {
  reader: 'Reader',
  contributor: 'Contributor',
  admin: 'Admin',
}

/** What each role can do, in one line (the invite form and the role picker). */
export const ROLE_LINE: Record<UserRole, string> = {
  reader: 'reads every reader page and asks Consult; changes nothing shared',
  contributor: 'a reader who can also grade, decide proposals and outcomes, and acknowledge alerts',
  admin: 'everything, including sources, settings and accounts',
}

export type RowAction =
  | { kind: 'role'; role: UserRole }
  | { kind: 'disable' }
  | { kind: 'enable' }
  | { kind: 'end_sessions' }
  | { kind: 'revoke' }

/** The name a person is shown by: their name, else their email. */
export function personName(row: { display_name: string; email: string | null; id?: string; user_id?: string }): string {
  return row.display_name.trim() || row.email || (row.id ?? row.user_id ?? '').slice(0, 8)
}

/** The one plain sentence a confirmation states before the change is sent. */
export function confirmSentence(action: RowAction, who: string, self = false): string {
  switch (action.kind) {
    case 'role':
      return self
        ? `You will become ${ROLE_LABEL[action.role].toLowerCase()}: ${ROLE_LINE[action.role]}.`
        : `${who} will become ${ROLE_LABEL[action.role].toLowerCase()}: ${ROLE_LINE[action.role]}.`
    case 'disable':
      return self
        ? 'You will be signed out everywhere at once and will not be able to sign in again until another admin enables you.'
        : `${who} will be signed out everywhere at once and will not be able to sign in until enabled again.`
    case 'enable':
      return `${who} will be able to sign in again with their existing password.`
    case 'end_sessions':
      return self
        ? 'You will be signed out on every device, including this one; you can sign in again.'
        : `${who} will be signed out on every device; they can sign in again with their password.`
    case 'revoke':
      return `The invite link sent to ${who} will stop working; you can invite them again later.`
  }
}

/** The label of the confirm button for an action. */
export function confirmLabel(action: RowAction): string {
  switch (action.kind) {
    case 'role':
      return `Make ${ROLE_LABEL[action.role].toLowerCase()}`
    case 'disable':
      return 'Disable'
    case 'enable':
      return 'Enable'
    case 'end_sessions':
      return 'End sessions'
    case 'revoke':
      return 'Withdraw invite'
  }
}

/**
 * Why a row cannot take an action, or null when it can. The owner always
 * stays an active admin; the only active admin cannot step down.
 */
export function blockedReason(row: AccountRow, action: RowAction, activeAdmins: number): string | null {
  const isAdmin = row.role === 'admin' && row.status === 'active'
  if (row.is_owner && (action.kind === 'disable' || (action.kind === 'role' && action.role !== 'admin'))) {
    return 'The owner always stays an active admin.'
  }
  const stepsDown = action.kind === 'disable' || (action.kind === 'role' && action.role !== 'admin')
  if (isAdmin && stepsDown && activeAdmins <= 1) {
    return 'This is the only active admin. Make someone else an admin first.'
  }
  return null
}

/** Active admins on the list (the last-admin rule's count). */
export function countActiveAdmins(rows: readonly AccountRow[]): number {
  return rows.filter((r) => r.role === 'admin' && r.status === 'active').length
}

/** The full invite link from the registry's `/#invite=<token>` path. */
export function inviteLink(origin: string, invitePath: string): string {
  const base = origin.replace(/\/+$/, '')
  return `${base}${invitePath.startsWith('/') ? invitePath : `/${invitePath}`}`
}

/** A date as a short day, or a dash when there is none. */
export function shortDate(iso: string | null | undefined): string {
  if (!iso) return '–'
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return '–'
  return new Date(t).toISOString().slice(0, 10)
}

/** "never", or how long ago, in plain words. */
export function agoText(iso: string | null | undefined, now: number): string {
  if (!iso) return 'never'
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return 'never'
  const mins = Math.max(0, Math.round((now - t) / 60_000))
  if (mins < 2) return 'just now'
  if (mins < 60) return `${mins} minutes ago`
  const hours = Math.round(mins / 60)
  if (hours < 48) return `${hours} hours ago`
  return `${Math.round(hours / 24)} days ago`
}

/** When an invite stops working, in plain words. */
export function expiryText(expiresAt: string | null, expired: boolean, now: number): string {
  if (expired || !expiresAt) return 'expired'
  const t = Date.parse(expiresAt)
  if (!Number.isFinite(t)) return 'expired'
  const hours = Math.round((t - now) / 3_600_000)
  if (hours < 1) return 'expires within the hour'
  if (hours < 48) return `expires in ${hours} hours`
  return `expires in ${Math.round(hours / 24)} days`
}
