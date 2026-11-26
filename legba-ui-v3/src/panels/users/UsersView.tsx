/**
 * Users — the admin's page for accounts (`view:users`, operator side).
 *
 *   - a table of accounts: name, email, role, state, created, last seen and
 *     live sessions, with per-row actions — change the role, disable or
 *     enable, end every session — each behind a confirmation that states the
 *     effect in one plain sentence;
 *   - Invite: an email, an optional name and a role; the answer is the link,
 *     shown ONCE with a copy button (the registry keeps only its hash). The
 *     platform sends no email: the admin sends the link;
 *   - pending invites, each with Withdraw.
 *
 * Readers and contributors never see the page offered (the catalog and ⌘K
 * leave it out); opened by a link anyway, it says it needs an admin and makes
 * no request. With sign-in off it explains how to turn sign-in on instead of
 * showing an empty table. The registry enforces every rule either way.
 */
import { useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { IDENTITY_QUERY_KEY } from '@/auth/session'
import { RANK_ADMIN, below, useRoleRank } from '@/auth/roles'
import { cn } from '@/lib/cn'
import {
  USERS_QUERY_KEY,
  changeRole,
  createInvite,
  disableUser,
  enableUser,
  endSessions,
  fetchAccounts,
  refusalCode,
  revokeInvite,
  usersErrorText,
  type AccountRow,
  type InviteMade,
  type PendingInvite,
  type UserRole,
} from './api'
import {
  ROLES,
  ROLE_LABEL,
  ROLE_LINE,
  agoText,
  blockedReason,
  confirmLabel,
  confirmSentence,
  countActiveAdmins,
  expiryText,
  inviteLink,
  personName,
  shortDate,
  type RowAction,
} from './model'

const INPUT =
  'w-full rounded-control border border-line bg-surf-2 px-2 py-1 text-body text-ink-1 placeholder:text-ink-3 focus:outline-none focus:ring-1 focus:ring-accent-emphasis'
const BTN =
  'rounded-control border border-line px-2 py-0.5 text-label text-ink-2 transition-colors hover:border-line-strong hover:text-ink-1 disabled:cursor-not-allowed disabled:text-ink-3'
const BTN_PRIMARY =
  'rounded-control border border-line-strong bg-surf-3 px-2.5 py-0.5 text-label font-medium text-ink-1 transition-colors hover:bg-surf-4 disabled:cursor-not-allowed disabled:text-ink-3'

function Head({ line, action }: { line: ReactNode; action?: ReactNode }) {
  return (
    <header className="flex items-end justify-between gap-3 border-b border-line-subtle pb-2" data-testid="users-head">
      <div>
        <h2 className="font-serif text-heading-lg text-ink-1">Users</h2>
        <div className="mt-0.5 text-label text-ink-2">{line}</div>
      </div>
      {action}
    </header>
  )
}

function Refusal({ text, testId = 'users-error' }: { text: string | null; testId?: string }) {
  if (!text) return null
  return (
    <p role="alert" className="text-label text-accent-critical" data-testid={testId}>
      {text}
    </p>
  )
}

/** Shown in place of the table while sign-in is off. */
export function SignInOff() {
  return (
    <div className="space-y-3 pt-3 text-body text-ink-2" data-testid="users-signin-off">
      <p className="text-ink-1">Sign-in is not enabled, so there are no individual accounts to manage.</p>
      <p>
        This instance asks every browser for one shared password, and everyone who has it is an admin. To give
        people their own accounts and roles, turn sign-in on, on the host:
      </p>
      <ol className="list-decimal space-y-1 pl-5">
        <li>Apply the migrations.</li>
        <li>
          Create the owner account once, on the host:{' '}
          <code className="font-mono text-label text-ink-1">python -m legba.data.users_bootstrap --email you@example.org</code>
        </li>
        <li>
          Set <code className="font-mono text-label text-ink-1">LEGBA_AUTH_SIGNIN=1</code> and recreate the registry,
          then the other services and the edge.
        </li>
      </ol>
      <p>
        Then sign in as the owner and come back here to invite people. The full steps are in the operating guide,
        under "Turning on sign-in".
      </p>
    </div>
  )
}

/** The link, once, with a copy button and the no-email note. */
export function InviteLinkPanel({ made, origin, onDone }: { made: InviteMade; origin: string; onDone: () => void }) {
  const link = inviteLink(origin, made.invite_path)
  const [copied, setCopied] = useState<'yes' | 'failed' | null>(null)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(link)
      setCopied('yes')
    } catch {
      setCopied('failed')
    }
  }
  const who = made.invite.email ?? 'this person'
  return (
    <section className="space-y-2 border-b border-line-subtle py-3" data-testid="users-invite-link">
      <p className="text-body text-ink-1">
        Invite ready for {who} as {ROLE_LABEL[made.invite.role].toLowerCase()}.
      </p>
      <div className="flex items-center gap-2">
        <input
          readOnly
          value={link}
          aria-label="Invite link"
          onFocus={(e) => e.currentTarget.select()}
          className={cn(INPUT, 'font-mono text-label')}
          data-testid="users-invite-link-value"
        />
        <button type="button" className={BTN_PRIMARY} onClick={copy} data-testid="users-invite-copy">
          {copied === 'yes' ? 'Copied' : 'Copy'}
        </button>
      </div>
      {copied === 'failed' && (
        <p className="text-label text-ink-2">The browser did not allow copying; select the link and copy it by hand.</p>
      )}
      <p className="text-label text-ink-2" data-testid="users-invite-no-email">
        No email is sent: send this link to {who} yourself. It works once, {expiryText(made.expires_at, false, Date.now())},
        and is shown only now — the registry keeps only a hash of it.
      </p>
      <button type="button" className={BTN} onClick={onDone} data-testid="users-invite-done">
        Done
      </button>
    </section>
  )
}

function InviteForm({ onMade, onCancel }: { onMade: (m: InviteMade) => void; onCancel: () => void }) {
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [role, setRole] = useState<UserRole>('reader')
  const qc = useQueryClient()
  const make = useMutation({
    mutationFn: () => createInvite(email, role, name),
    onSuccess: (made) => {
      void qc.invalidateQueries({ queryKey: USERS_QUERY_KEY })
      onMade(made)
    },
  })
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!email.trim() || make.isPending) return
    make.mutate()
  }
  return (
    <form onSubmit={submit} className="space-y-2 border-b border-line-subtle py-3" data-testid="users-invite-form" noValidate>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <label className="block">
          <span className="mb-0.5 block text-label text-ink-2">Email</span>
          <input
            className={INPUT}
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            data-testid="users-invite-email"
            autoComplete="off"
            required
          />
        </label>
        <label className="block">
          <span className="mb-0.5 block text-label text-ink-2">Name (optional)</span>
          <input className={INPUT} value={name} onChange={(e) => setName(e.target.value)} data-testid="users-invite-name" autoComplete="off" />
        </label>
      </div>
      <fieldset>
        <legend className="mb-0.5 text-label text-ink-2">Role</legend>
        <div className="space-y-0.5">
          {ROLES.map((r) => (
            <label key={r} className="flex items-baseline gap-2 text-body text-ink-2">
              <input type="radio" name="invite-role" value={r} checked={role === r} onChange={() => setRole(r)} data-testid={`users-invite-role-${r}`} />
              <span className="text-ink-1">{ROLE_LABEL[r]}</span>
              <span className="text-label text-ink-3">{ROLE_LINE[r]}</span>
            </label>
          ))}
        </div>
      </fieldset>
      <Refusal text={make.isError ? usersErrorText(make.error) : null} testId="users-invite-error" />
      <div className="flex gap-2">
        <button type="submit" className={BTN_PRIMARY} disabled={!email.trim() || make.isPending} data-testid="users-invite-submit">
          {make.isPending ? 'Creating…' : 'Create invite link'}
        </button>
        <button type="button" className={BTN} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  )
}

interface Pending {
  key: string
  action: RowAction
  sentence: string
  run: () => Promise<unknown>
  self: boolean
}

/** One plain sentence, then Confirm / Cancel. */
function ConfirmLine({ pending, onClose }: { pending: Pending; onClose: () => void }) {
  const qc = useQueryClient()
  const go = useMutation({
    mutationFn: pending.run,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: USERS_QUERY_KEY })
      if (pending.self) void qc.invalidateQueries({ queryKey: IDENTITY_QUERY_KEY })
      onClose()
    },
    onError: (err) => {
      const code = refusalCode(err)
      if (code === 'user_unknown' || code === 'invite_unknown') void qc.invalidateQueries({ queryKey: USERS_QUERY_KEY })
    },
  })
  return (
    <div className="space-y-1 border-l-2 border-accent-warning py-1.5 pl-3" role="group" aria-label="Confirm the change" data-testid="users-confirm">
      <p className="text-body text-ink-1" data-testid="users-confirm-sentence">
        {pending.sentence}
      </p>
      <Refusal text={go.isError ? usersErrorText(go.error) : null} testId="users-confirm-error" />
      <div className="flex gap-2">
        <button type="button" className={BTN_PRIMARY} disabled={go.isPending} onClick={() => go.mutate()} data-testid="users-confirm-go">
          {go.isPending ? 'Working…' : confirmLabel(pending.action)}
        </button>
        <button type="button" className={BTN} onClick={onClose} data-testid="users-confirm-cancel">
          Cancel
        </button>
      </div>
    </div>
  )
}

function AccountLine({
  row,
  you,
  activeAdmins,
  now,
  pending,
  ask,
  onClose,
}: {
  row: AccountRow
  you: string | null
  activeAdmins: number
  now: number
  pending: Pending | null
  ask: (p: Pending) => void
  onClose: () => void
}) {
  const self = row.id === you
  const name = personName(row)
  const open = pending?.key.startsWith(`${row.id}:`) ? pending : null
  const offer = (action: RowAction, run: () => Promise<unknown>) => {
    ask({ key: `${row.id}:${action.kind}`, action, sentence: confirmSentence(action, name, self), run, self })
  }
  const disableBlock = blockedReason(row, { kind: 'disable' }, activeAdmins)
  return (
    <li className="border-b border-line-subtle py-2" data-testid="users-row" data-user={row.id} data-state={row.status}>
      <div className="grid grid-cols-[minmax(10rem,2fr)_minmax(7rem,1fr)_minmax(6rem,1fr)_minmax(8rem,1fr)] items-baseline gap-x-3 gap-y-1 max-lg:grid-cols-2">
        <div className="min-w-0">
          <div className="truncate text-body text-ink-1">
            {name}
            {self && <span className="ml-1.5 text-label text-ink-3">(you)</span>}
            {row.is_owner && (
              <span className="ml-1.5 text-label text-ink-2" data-testid="users-owner">
                owner
              </span>
            )}
          </div>
          {row.email && row.email !== name && <div className="truncate text-label text-ink-3">{row.email}</div>}
        </div>
        <div>
          <label className="sr-only" htmlFor={`role-${row.id}`}>
            Role of {name}
          </label>
          <select
            id={`role-${row.id}`}
            value={row.role}
            disabled={row.status !== 'active' || !!row.is_owner}
            title={row.is_owner ? 'The owner always stays an active admin.' : row.status !== 'active' ? 'Enable the account to change its role.' : undefined}
            onChange={(e) => {
              const role = e.target.value as UserRole
              const block = blockedReason(row, { kind: 'role', role }, activeAdmins)
              if (block) {
                ask({ key: `${row.id}:blocked`, action: { kind: 'role', role }, sentence: block, run: async () => undefined, self })
                return
              }
              offer({ kind: 'role', role }, () => changeRole(row.id, role))
            }}
            className="rounded-control border border-line bg-surf-2 px-1.5 py-0.5 text-body text-ink-1 disabled:text-ink-3"
            data-testid="users-role-select"
          >
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {ROLE_LABEL[r]}
              </option>
            ))}
          </select>
        </div>
        <div className="text-label">
          <span className={row.status === 'active' ? 'text-ink-1' : 'text-accent-warning'} data-testid="users-state">
            {row.status === 'active' ? 'active' : 'disabled'}
          </span>
          <div className="text-ink-3">
            {row.live_sessions === 1 ? '1 session' : `${row.live_sessions} sessions`}
          </div>
        </div>
        <div className="text-label text-ink-3">
          <div>last seen {agoText(row.last_seen_at ?? row.last_sign_in_at, now)}</div>
          <div>added {shortDate(row.created_at)}</div>
        </div>
      </div>
      <div className="mt-1 flex flex-wrap gap-1.5">
        {row.status === 'active' ? (
          <button
            type="button"
            className={BTN}
            disabled={!!disableBlock}
            title={disableBlock ?? undefined}
            onClick={() => offer({ kind: 'disable' }, () => disableUser(row.id))}
            data-testid="users-disable"
          >
            Disable
          </button>
        ) : (
          <button type="button" className={BTN} onClick={() => offer({ kind: 'enable' }, () => enableUser(row.id))} data-testid="users-enable">
            Enable
          </button>
        )}
        <button
          type="button"
          className={BTN}
          disabled={row.live_sessions === 0}
          title={row.live_sessions === 0 ? 'No live sessions.' : undefined}
          onClick={() => offer({ kind: 'end_sessions' }, () => endSessions(row.id))}
          data-testid="users-end-sessions"
        >
          End sessions
        </button>
      </div>
      {open &&
        (open.key.endsWith(':blocked') ? (
          <div className="mt-1 flex items-baseline gap-2">
            <Refusal text={open.sentence} testId="users-blocked" />
            <button type="button" className={BTN} onClick={onClose}>
              OK
            </button>
          </div>
        ) : (
          <div className="mt-1">
            <ConfirmLine pending={open} onClose={onClose} />
          </div>
        ))}
    </li>
  )
}

function InviteLine({
  inv,
  now,
  inviter,
  pending,
  ask,
  onClose,
}: {
  inv: PendingInvite
  now: number
  inviter: string | null
  pending: Pending | null
  ask: (p: Pending) => void
  onClose: () => void
}) {
  const name = personName(inv)
  const open = pending?.key === `${inv.user_id}:revoke` ? pending : null
  return (
    <li className="border-b border-line-subtle py-2" data-testid="users-invite-row" data-user={inv.user_id}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="min-w-0">
          <span className="text-body text-ink-1">{name}</span>
          {inv.email && inv.email !== name && <span className="ml-1.5 text-label text-ink-3">{inv.email}</span>}
          <div className="text-label text-ink-3">
            {ROLE_LABEL[inv.role]} · invited {shortDate(inv.created_at)}
            {inviter ? ` by ${inviter}` : ''} ·{' '}
            <span className={inv.expired ? 'text-accent-warning' : undefined}>{expiryText(inv.expires_at, inv.expired, now)}</span>
          </div>
        </div>
        <button
          type="button"
          className={BTN}
          onClick={() =>
            ask({
              key: `${inv.user_id}:revoke`,
              action: { kind: 'revoke' },
              sentence: confirmSentence({ kind: 'revoke' }, name),
              run: () => revokeInvite(inv.user_id),
              self: false,
            })
          }
          data-testid="users-revoke"
        >
          Withdraw
        </button>
      </div>
      {open && (
        <div className="mt-1">
          <ConfirmLine pending={open} onClose={onClose} />
        </div>
      )}
    </li>
  )
}

export default function UsersView() {
  const rank = useRoleRank()
  const notAdmin = below(rank, RANK_ADMIN)
  const q = useQuery({
    queryKey: USERS_QUERY_KEY,
    queryFn: fetchAccounts,
    enabled: !notAdmin,
    retry: false,
    staleTime: 15_000,
  })
  const [inviting, setInviting] = useState(false)
  const [made, setMade] = useState<InviteMade | null>(null)
  const [pending, setPending] = useState<Pending | null>(null)
  const now = Date.now()

  const users = q.data?.users
  const activeAdmins = useMemo(() => (users ? countActiveAdmins(users) : 0), [users])
  const names = useMemo(() => {
    const m = new Map<string, string>()
    for (const u of users ?? []) m.set(u.actor, personName(u))
    return m
  }, [users])

  if (notAdmin) {
    return (
      <div className="p-3" data-testid="users-view">
        <Head line="Accounts on this instance." />
        <p className="pt-3 text-body text-ink-2" data-testid="users-needs-admin">
          Managing accounts needs an admin. Ask an admin to invite people or change a role.
        </p>
      </div>
    )
  }

  const code = q.isError ? refusalCode(q.error) : null
  if (code === 'signin_not_enabled') {
    return (
      <div className="p-3" data-testid="users-view">
        <Head line="Accounts on this instance." />
        <SignInOff />
      </div>
    )
  }

  const count = q.data ? `${q.data.users.length} ${q.data.users.length === 1 ? 'account' : 'accounts'} · ${q.data.invites.length} pending ${q.data.invites.length === 1 ? 'invite' : 'invites'}` : 'Accounts on this instance.'

  return (
    <div className="h-full overflow-y-auto p-3" data-testid="users-view">
      <Head
        line={count}
        action={
          q.data && !inviting && !made ? (
            <button type="button" className={BTN_PRIMARY} onClick={() => setInviting(true)} data-testid="users-invite-open">
              Invite
            </button>
          ) : undefined
        }
      />
      {q.isPending && <p className="pt-3 text-label text-ink-3">reading accounts…</p>}
      {q.isError && (
        <div className="pt-3">
          <Refusal text={usersErrorText(q.error)} />
        </div>
      )}
      {inviting && !made && (
        <InviteForm
          onMade={(m) => {
            setMade(m)
            setInviting(false)
          }}
          onCancel={() => setInviting(false)}
        />
      )}
      {made && <InviteLinkPanel made={made} origin={window.location.origin} onDone={() => setMade(null)} />}
      {q.data && (
        <>
          <ul className="mt-1" data-testid="users-table">
            {q.data.users.length === 0 && <li className="py-2 text-label text-ink-3">No accounts yet beyond pending invites.</li>}
            {q.data.users.map((row) => (
              <AccountLine
                key={row.id}
                row={row}
                you={q.data.you}
                activeAdmins={activeAdmins}
                now={now}
                pending={pending}
                ask={setPending}
                onClose={() => setPending(null)}
              />
            ))}
          </ul>
          <section className="mt-4" data-testid="users-invites">
            <h3 className="border-b border-line-subtle pb-1 text-label uppercase tracking-[0.06em] text-ink-3">Pending invites</h3>
            {q.data.invites.length === 0 ? (
              <p className="py-2 text-label text-ink-3">No invite is waiting to be accepted.</p>
            ) : (
              <ul>
                {q.data.invites.map((inv) => (
                  <InviteLine
                    key={inv.user_id}
                    inv={inv}
                    now={now}
                    inviter={inv.invited_by ? names.get(inv.invited_by) ?? null : null}
                    pending={pending}
                    ask={setPending}
                    onClose={() => setPending(null)}
                  />
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </div>
  )
}
