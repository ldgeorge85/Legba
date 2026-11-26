/**
 * Settings (M-2; plan §2.3 row "Settings"), from the top bar: who is signed
 * in and as what, sign out, how alerts reach you, where "since you last
 * looked" is kept, whether a Consult answer may move the screen (M-3g: the
 * desktop's drive mode, `state/driveMode`, the same default), "Desktop view",
 * and — for an admin — that the engine controls are on the desktop. Nothing
 * here writes to the record.
 */
import { useIdentity } from '@/auth/SignInGate'
import { signOut, userLabel } from '@/auth/session'
import { DESKTOP_OPT_KEY } from '@/lib/phoneSurface'
import { useDriveMode } from '@/state/driveMode'
import { seenWhereWords, useSeen, DEVICE_SEEN_KEY } from '@/mobile/shell/seenCursor'
import { RECENT_SUBJECTS_KEY } from '@/mobile/screens/subjects/SubjectScreens'
import { Quiet, SectionHead } from '@/mobile/shell/ui'

/** The product's roles in plain words (owner, admin, contributor, user). */
function roleWords(role: string, owner: boolean): string {
  if (owner) return 'owner'
  if (role === 'admin' || role === 'operator') return 'admin'
  if (role === 'contributor') return 'contributor'
  return 'user'
}

export default function SettingsPage() {
  const id = useIdentity()
  const seen = useSeen()
  const ident = id.data?.state === 'signed_in' ? id.data.identity : null
  const user = ident?.user ?? null
  const owner = !!(ident?.is_owner || user?.is_owner)
  const role = ident ? roleWords(ident.role, owner) : null
  const admin = role === 'admin' || role === 'owner'
  const signedInPerson = ident?.kind === 'user' && user
  const driveOn = useDriveMode((s) => s.on)
  const setDrive = useDriveMode((s) => s.setOn)

  return (
    <div className="ph-screen" data-testid="phone-settings">
      <SectionHead title="You" />
      {ident ? (
        <p className="ph-body" data-testid="phone-settings-who">
          {signedInPerson ? (
            <>
              Signed in as <span className="ph-ink-1">{userLabel(user)}</span> · {role}
            </>
          ) : (
            <>Not signed in as a person: this phone reads with the shared access ({role}).</>
          )}
        </p>
      ) : (
        <Quiet>{id.isPending ? 'Reading who you are…' : 'Who you are could not be read.'}</Quiet>
      )}
      {signedInPerson ? (
        <button
          type="button"
          className="ph-btn ph-btn-wide"
          data-testid="phone-sign-out"
          onClick={async () => {
            try {
              await signOut()
            } finally {
              // the phone keeps no record on the device; its own preferences go too
              for (const k of [DEVICE_SEEN_KEY, RECENT_SUBJECTS_KEY]) {
                try {
                  window.localStorage.removeItem(k)
                } catch {
                  // storage blocked
                }
              }
              window.location.replace('/m/')
            }
          }}
        >
          Sign out
        </button>
      ) : null}
      {admin ? <Quiet testId="phone-settings-engine">Engine controls are on the desktop.</Quiet> : null}

      <SectionHead title="Alerts" />
      <p className="ph-body">
        Pages reach you through the self-hosted notification server, as before. This app sends no push notifications, and nothing leaves the server for them.
      </p>

      <SectionHead title="Consult" />
      <button
        type="button"
        role="switch"
        aria-checked={driveOn}
        className="ph-switch ph-switch-wide"
        onClick={() => setDrive(!driveOn)}
        data-testid="phone-settings-drive"
      >
        <span className="ph-switch-dot" aria-hidden />
        Let answers open screens: {driveOn ? 'on' : 'off'}
      </button>
      <p className="ph-body" data-testid="phone-settings-drive-words">
        {driveOn
          ? 'When an answer points to a subject, the map, an alert or a record, the phone opens it at once. Undo puts you back where you were.'
          : 'Answers move nothing. An answer that pointed to screens before offers a button to show them.'}{' '}
        Kept on this phone only.
      </p>

      <SectionHead title="Since you last looked" />
      <p className="ph-body" data-testid="phone-settings-seen">
        {seen.data ? `${seenWhereWords(seen.data)}.` : 'Reading…'}
      </p>

      <SectionHead title="On this phone" />
      <p className="ph-body">No report is stored on this phone: what you read is fetched when you open it and kept only while the app is open.</p>

      <SectionHead title="Desktop view" />
      <p className="ph-body">The desktop is built for a mouse and a wide screen.</p>
      <a
        className="ph-btn ph-btn-wide"
        href="/?desktop=1"
        data-testid="phone-desktop-view"
        onClick={() => {
          try {
            window.sessionStorage.setItem(DESKTOP_OPT_KEY, '1')
          } catch {
            // the link's ?desktop=1 still carries the choice
          }
        }}
      >
        Open the desktop view
      </a>
    </div>
  )
}
