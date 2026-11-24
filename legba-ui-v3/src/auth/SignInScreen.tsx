/**
 * X3-B3 — the sign-in screen and the invite-accept screen.
 *
 * Full-page, before the shell: the workstation renders nothing a caller the
 * registry refused could read. Two forms over one frame:
 *
 *   - SignInScreen          email + password → `POST /auth/sign-in`;
 *   - InviteAcceptScreen    the invite link's token + a chosen password (twice)
 *                           + an optional display name → `POST /auth/accept`.
 *
 * Every refusal prints the registry's own message (`detail.message`), never a
 * generic "something went wrong". On success the registry has set the
 * HttpOnly session cookie; `onDone` reloads the shell onto it.
 */

import { useState, type FormEvent, type ReactNode } from 'react'
import { cn } from '@/lib/cn'
import {
  acceptInvite,
  PASSWORD_MIN_CHARS,
  refusalText,
  signIn,
} from '@/auth/session'

// M-0a — on a narrow screen (under Tailwind's `sm`, 640 px) every input and
// button is a 48 px touch target and the inputs are 16 px, so iOS does not
// zoom on focus; the card sits near the top third instead of the middle of an
// empty screen. The `max-sm:` variants leave the desktop look byte-for-byte as
// it was (the audit measured 33 px inputs and a 30 px button on a phone).
const INPUT =
  'w-full rounded border border-line bg-surf-2 px-2 py-1.5 text-body text-ink-1 placeholder:text-ink-3 focus:outline-none focus:ring-1 focus:ring-accent-emphasis max-sm:min-h-12 max-sm:text-base'
const BUTTON =
  'w-full rounded-sm bg-surf-3 px-3 py-1.5 text-label font-medium tracking-wide text-ink-1 transition-colors hover:bg-surf-4 disabled:cursor-not-allowed disabled:text-ink-3 max-sm:min-h-12 max-sm:text-sm'

function AuthFrame({
  title,
  note,
  children,
  testId,
}: {
  title: string
  note?: ReactNode
  children: ReactNode
  testId: string
}) {
  return (
    <div
      className="flex h-full w-full items-center justify-center bg-surf-base p-4 max-sm:items-start max-sm:pt-16"
      data-testid={testId}
    >
      <div className="w-full max-w-sm rounded border border-line bg-surf-1 p-5">
        <div className="mb-1 text-label uppercase tracking-wider text-ink-3">Legba</div>
        <h1 className="mb-3 text-heading-lg font-semibold text-ink-1">{title}</h1>
        {note && <p className="mb-3 text-label text-ink-2">{note}</p>}
        {children}
      </div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="mb-3 block">
      <span className="mb-1 block text-label text-ink-2">{label}</span>
      {children}
    </label>
  )
}

function Refusal({ text }: { text: string | null }) {
  if (!text) return null
  return (
    <p role="alert" className="mb-3 text-label text-accent-critical" data-testid="auth-refusal">
      {text}
    </p>
  )
}

export interface SignInScreenProps {
  /** Why the screen is up (a session that ended, a perimeter operator asking). */
  reason?: string | null
  /** After the cookie is set. Default: reload the shell onto it. */
  onDone?: () => void
  /** The perimeter operator may go back to the workstation instead. */
  onCancel?: () => void
}

export function reloadIntoShell(): void {
  window.location.replace(window.location.pathname + window.location.search)
}

export function SignInScreen({ reason, onDone = reloadIntoShell, onCancel }: SignInScreenProps) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setRefusal(null)
    try {
      await signIn(email.trim(), password)
      onDone()
    } catch (err) {
      setRefusal(refusalText(err))
      setBusy(false)
    }
  }

  return (
    <AuthFrame
      title="Sign in"
      testId="sign-in-screen"
      note={reason ?? 'Sign-in is by invitation. Use the email your invite was sent to.'}
    >
      <form onSubmit={submit} noValidate>
        <Field label="Email">
          <input
            className={INPUT}
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            data-testid="sign-in-email"
            required
          />
        </Field>
        <Field label="Password">
          <input
            className={INPUT}
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            data-testid="sign-in-password"
            required
          />
        </Field>
        <Refusal text={refusal} />
        <button
          type="submit"
          className={BUTTON}
          disabled={busy || !email.trim() || !password}
          data-testid="sign-in-submit"
        >
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="mt-2 w-full text-label text-ink-3 hover:text-ink-2 max-sm:min-h-12"
            data-testid="sign-in-cancel"
          >
            Back to the workstation
          </button>
        )}
      </form>
    </AuthFrame>
  )
}

export interface InviteAcceptScreenProps {
  token: string
  onDone?: () => void
}

export function InviteAcceptScreen({ token, onDone = reloadIntoShell }: InviteAcceptScreenProps) {
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)

  const tooShort = password.length > 0 && password.length < PASSWORD_MIN_CHARS
  const mismatch = confirm.length > 0 && confirm !== password
  const ready = password.length >= PASSWORD_MIN_CHARS && confirm === password

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!ready) return
    setBusy(true)
    setRefusal(null)
    try {
      await acceptInvite(token, password, name)
      onDone()
    } catch (err) {
      setRefusal(refusalText(err))
      setBusy(false)
    }
  }

  return (
    <AuthFrame
      title="Accept your invitation"
      testId="invite-accept-screen"
      note="Choose the password you will sign in with. The invitation link works once."
    >
      <form onSubmit={submit} noValidate>
        <Field label="Your name (optional)">
          <input
            className={INPUT}
            autoComplete="name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            data-testid="invite-name"
          />
        </Field>
        <Field label={`Password (at least ${PASSWORD_MIN_CHARS} characters)`}>
          <input
            className={cn(INPUT, tooShort && 'border-accent-critical')}
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            data-testid="invite-password"
          />
        </Field>
        <Field label="The same password again">
          <input
            className={cn(INPUT, mismatch && 'border-accent-critical')}
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            data-testid="invite-confirm"
          />
        </Field>
        {tooShort && (
          <p className="mb-3 text-label text-ink-3">
            {password.length} of {PASSWORD_MIN_CHARS} characters
          </p>
        )}
        {mismatch && <p className="mb-3 text-label text-ink-3">the two passwords differ</p>}
        <Refusal text={refusal} />
        <button type="submit" className={BUTTON} disabled={busy || !ready} data-testid="invite-submit">
          {busy ? 'Accepting…' : 'Accept and sign in'}
        </button>
      </form>
    </AuthFrame>
  )
}
