/**
 * Live sign-in as a named account: the pure parts.
 *
 * With sign-in on and the console's edge perimeter off, no bearer reaches a
 * browser, so a live walk signs in the way a person does: the real sign-in
 * screen, an email and a password. The password comes from a file named by
 * LEGBA_WALK_PASSWORD_FILE (mounted read-only into the walk's container) and is
 * never printed, logged, written down or drawn in a screenshot; the session
 * cookie the registry answers with is held in memory only.
 *
 * A walk account may hold a lower role than an admin. A check that needs a
 * higher role than the account holds is SKIPPED on live and named in the
 * report — never failed, and never weakened on fixtures (fixture mode has no
 * account and runs every check).
 */

/** The ranks of the sign-in roles (src/legba/shared/auth_signin.py ROLES). */
export const ROLE_RANK = { reader: 1, contributor: 2, admin: 3 }

/**
 * The password in a password file: the value of a `password:` line when there
 * is one, else the whole file trimmed. Empty when neither yields anything.
 */
export function passwordFromFile(text) {
  const s = String(text ?? '')
  for (const line of s.split(/\r?\n/)) {
    const m = /^\s*password\s*:\s?(.*)$/i.exec(line)
    if (m) return m[1].replace(/\s+$/, '')
  }
  return s.trim()
}

/**
 * The `name=value` pair of the first cookie in a response's Set-Cookie
 * headers (attributes dropped), or '' when there is none.
 */
export function sessionCookieFrom(setCookies) {
  for (const raw of setCookies ?? []) {
    const pair = String(raw).split(';', 1)[0].trim()
    const eq = pair.indexOf('=')
    if (eq > 0 && pair.slice(eq + 1)) return pair
  }
  return ''
}

/** The role named by a sign-in or /auth/me answer (`user.role` or `role`), or null. */
export function roleFrom(body) {
  const role = body?.user?.role ?? body?.role ?? null
  return typeof role === 'string' && role in ROLE_RANK ? role : null
}

/**
 * True when a check that needs `needed` must be skipped for an account
 * holding `held`. `held` null = no account (fixture mode, or the bearer):
 * nothing is skipped.
 */
export function skipsForRole(held, needed) {
  if (held == null) return false
  return (ROLE_RANK[held] ?? 0) < (ROLE_RANK[needed] ?? Infinity)
}

/**
 * The checks that rest on a read (or an act) above the reader's role, with
 * the role they need and the routes that decide it (route_policy.py
 * ROUTE_MIN_ROLES). Measured on a live walk as a reader: each of
 * these routes answered 403 and each check below failed for that reason
 * alone. A check matching an entry is SKIPPED (named, with its role) when the
 * signed-in account is below `needs`; it is never failed and never passed.
 *
 * The reader surfaces' reads are open and every change stays with a
 * contributor or an admin: the reader's map, Watch and phone checks
 * now read the reader's own routes (`/v3/roster`, `/v3/watch/alerts`, the
 * lowered figures) and run for a reader. What stays: the change actions, the
 * operator side (a reader has no way across — the button says why), and the
 * drift flags' passages (an admin's read; a reader sees the count).
 */
export const ROLE_GATED = [
  // the operator side: engine, evaluation internals, the gate, investigation
  { match: 'the operator side has its four sections', needs: 'contributor', reads: ['the operator side (no way across below contributor)'] },
  { match: /^Flow( panel$|: )/, needs: 'admin', reads: ['/v3/flow/graph'] },
  { match: 'the Gate shows the forecast resolution queue', needs: 'contributor', reads: ['/v3/ledger/resolution-queue'] },
  { match: 'a proposed resolution opens with its evidence', needs: 'contributor', reads: ['/v3/ledger/resolution-queue'] },
  // a flag's changed passages quote the publisher's page — an admin's read
  { match: /^Watch: the open-flag count is the one the API returns/, needs: 'admin', reads: ['/v3/evidence/drift/flags'] },
  // the change actions
  { match: /^phone \d+: an alert row opens its page; Acknowledge and Snooze/, needs: 'contributor', reads: ['POST /v3/system/alerts/{id}/ack', 'POST /v3/system/alerts/{id}/snooze'] },
]

/** The ROLE_GATED entry a check's name matches, or null. */
export function roleGateFor(name) {
  for (const g of ROLE_GATED) {
    if (typeof g.match === 'string' ? g.match === name : g.match.test(name)) return g
  }
  return null
}
