/**
 * S8 / Users — session & RBAC panel (`system.users`).
 *
 * The registry's auth surface is still the single shared bearer token
 * (`LEGBA_REGISTRY_API_TOKEN`); the DID-bearer + OAuth2 user directory (and
 * with it real user CRUD) lands later (L-113 brief). Until then there is no
 * `/users` endpoint to roster — so this panel is the honest thing we CAN show:
 * the *current session*. It decodes the stored bearer as a best-effort JWT via
 * `@/auth/jwt` and surfaces the claims that gate the UI:
 *
 *  - `sub`    — the subject (operator identity) the token asserts.
 *  - `mode`   — the deployment mode the token is scoped to.
 *  - `roles`  — the RBAC role set.
 *  - `exp`    — expiry (with a live countdown / expired flag).
 *
 * It also surfaces the two derived verdicts the rest of the UI keys off:
 * `isOperator()` (admin/operator gate) and `currentMode()` (the resolved mode,
 * which can be overridden by `?mode=` ahead of the claim).
 *
 * Decode-only — the registry remains the source of truth; in dev mode any
 * token (or none) is accepted and `isOperator()` defaults open.
 */

import { useState } from 'react'
import type { ReactNode } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import {
  currentMode,
  getToken,
  isOperator,
  tryDecodeClaims,
} from '@/auth/jwt'
import type { AuthClaims, PanelProps } from '@/types'

const ROLE_PILL: Record<string, string> = {
  admin: 'bg-rose-900 text-rose-200',
  operator: 'bg-amber-900 text-amber-200',
  analyst: 'bg-sky-900 text-sky-200',
  viewer: 'bg-slate-700 text-slate-300',
}

interface Session {
  token: string | null
  claims: AuthClaims | null
  operator: boolean
  mode: string
}

function readSession(): Session {
  const token = getToken()
  return {
    token,
    claims: tryDecodeClaims(token),
    operator: isOperator(),
    mode: currentMode(),
  }
}

function expiryView(exp: number): { label: string; expired: boolean; unknown: boolean } {
  if (!exp) return { label: 'no expiry claim', expired: false, unknown: true }
  const ms = exp * 1000
  const expired = ms <= Date.now()
  const when = new Date(ms).toLocaleString()
  if (expired) return { label: `expired ${when}`, expired: true, unknown: false }
  const mins = Math.round((ms - Date.now()) / 60000)
  const rel = mins < 60 ? `${mins}m` : `${Math.round(mins / 60)}h`
  return { label: `${when} (in ${rel})`, expired: false, unknown: false }
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline gap-2 text-xs">
      <span className="w-20 shrink-0 text-slate-500 uppercase tracking-wide text-[10px]">
        {label}
      </span>
      <span className="text-slate-200 break-all">{children}</span>
    </div>
  )
}

export default function UsersPanel({ registration }: PanelProps) {
  const [session, setSession] = useState<Session>(() => readSession())
  const { token, claims, operator, mode } = session
  const exp = claims ? expiryView(claims.exp) : null

  return (
    <PanelChrome
      registration={registration}
      subtitle="current session · RBAC (decode-only)"
      onRefresh={() => setSession(readSession())}
    >
      <div className="flex-1 overflow-auto space-y-3 text-xs">
        {/* derived verdicts the rest of the UI gates on */}
        <div className="flex items-center gap-2 flex-wrap" data-testid="users-verdicts">
          <span
            className={`rounded px-2 py-0.5 ${
              operator ? 'bg-emerald-900 text-emerald-200' : 'bg-slate-800 text-slate-400'
            }`}
            data-testid="users-operator"
            title="isOperator() — gates operator/admin-only panels"
          >
            {operator ? '✓ operator' : '○ not operator'}
          </span>
          <span
            className="rounded px-2 py-0.5 bg-surface-200 text-slate-300 font-mono"
            data-testid="users-mode"
            title="currentMode() — ?mode= override > JWT claim > env default"
          >
            mode: {mode}
          </span>
          {!token && (
            <span
              className="rounded px-2 py-0.5 bg-slate-800 text-slate-400"
              data-testid="users-no-token"
            >
              no bearer stored
            </span>
          )}
          {token && !claims && (
            <span
              className="rounded px-2 py-0.5 bg-amber-950 text-amber-300"
              data-testid="users-opaque-token"
              title="A bearer is set but it is not a decodable JWT (shared-token / dev mode)"
            >
              opaque token (not a JWT)
            </span>
          )}
        </div>

        {/* decoded claims */}
        {claims ? (
          <div
            className="bg-surface-100 border border-slate-800 rounded p-2 space-y-1.5"
            data-testid="users-claims"
          >
            <Field label="subject">
              <code className="font-mono">{claims.sub}</code>
            </Field>
            <Field label="mode (claim)">
              <code className="font-mono">{claims.mode}</code>
              {claims.mode !== mode && (
                <span className="ml-2 text-amber-400" data-testid="users-mode-override">
                  (overridden → {mode})
                </span>
              )}
            </Field>
            <div className="flex items-baseline gap-2">
              <span className="w-20 shrink-0 text-slate-500 uppercase tracking-wide text-[10px]">
                roles
              </span>
              <span className="flex flex-wrap gap-1" data-testid="users-roles">
                {claims.roles.length === 0 && (
                  <span className="text-slate-500">none</span>
                )}
                {claims.roles.map((r) => (
                  <span
                    key={r}
                    className={`rounded px-1.5 py-0.5 ${ROLE_PILL[r] ?? 'bg-slate-700 text-slate-300'}`}
                    data-testid={`users-role-${r}`}
                  >
                    {r}
                  </span>
                ))}
              </span>
            </div>
            {exp && (
              <Field label="expiry">
                <span
                  className={
                    exp.expired
                      ? 'text-rose-300'
                      : exp.unknown
                        ? 'text-slate-500'
                        : 'text-slate-200'
                  }
                  data-testid="users-expiry"
                >
                  {exp.label}
                </span>
              </Field>
            )}
          </div>
        ) : (
          <div
            className="text-slate-500 bg-surface-100 border border-slate-800 rounded p-3"
            data-testid="users-no-claims"
          >
            No decodable JWT claims for the current session. The registry is
            running on the shared bearer token (dev mode), so RBAC is not yet
            enforced client-side and operator gates default open.
          </div>
        )}

        {/* honest note: no user directory yet */}
        <div className="text-[11px] text-slate-500 border-t border-slate-800 pt-2" data-testid="users-crud-note">
          User CRUD / role assignment is not exposed yet — the registry auth
          surface is the single shared bearer token today. A real user directory
          (DID-bearer + OAuth2) lands with L-113; this panel will roster it then.
        </div>
      </div>
    </PanelChrome>
  )
}
