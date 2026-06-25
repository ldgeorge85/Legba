/**
 * UI-6 (Tier G) — alert / notification-center data model.
 *
 * v2's "Watchlist" reframed for the source-first model: instead of a static
 * list of entities to stare at, the operator manages **alert subscriptions**
 * — each one binds to a target's / analyst's `alert` output (the `alert`
 * output kind from TRAVIS_ASM_BRIEF §1.6, severity-aware fan-out) at a
 * chosen severity floor — and sees **recently fired alerts** live-tailed
 * off the registry-events WS.
 *
 * Subscriptions persist to localStorage (no backend write surface for an
 * operator-local watchlist); fired alerts arrive via the NATS multiplexer on
 * the `alert.*` / `analyst.*.alert` subjects. All persistence + envelope
 * mapping + matching logic is here so it's unit-testable without a DOM
 * (same split as `@/lib/findingsViews`).
 */

export type AlertScopeKind = 'target' | 'analyst'

export type Severity = 'low' | 'medium' | 'high' | 'critical'

export const SEVERITY_ORDER: readonly Severity[] = [
  'low',
  'medium',
  'high',
  'critical',
] as const

const SEV_RANK: Record<string, number> = {
  low: 1,
  medium: 2,
  high: 3,
  critical: 4,
}

/** One alert subscription — operator binds to a scope at a severity floor. */
export interface AlertSubscription {
  /** Stable id (kind:scope_id). */
  id: string
  scope_kind: AlertScopeKind
  /** target_id or analyst_id. */
  scope_id: string
  /** Only fire when severity >= this floor. */
  severity_floor: Severity
  /** Operator can mute without deleting. */
  muted: boolean
  created_at: string
}

/** A fired alert as surfaced in the center (mapped from a WS envelope). */
export interface FiredAlert {
  id: string
  title: string
  severity: Severity | null
  target_id: string | null
  analyst_id: string | null
  fired_at: string
  /** Which subscription matched (if any). */
  matched_sub_id: string | null
}

const STORAGE_KEY = 'legba.alerts.subscriptions'

/** The WS subject filter the alert center subscribes to. */
export const ALERT_TAIL_FILTER = 'alert.>'

export function subId(kind: AlertScopeKind, scopeId: string): string {
  return `${kind}:${scopeId}`
}

export function loadSubscriptions(): AlertSubscription[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? (parsed as AlertSubscription[]) : []
  } catch {
    return []
  }
}

export function persistSubscriptions(subs: AlertSubscription[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(subs))
  } catch {
    /* ignore quota / private-mode errors */
  }
}

/** Add (or replace by id) a subscription, returning a new array. */
export function upsertSubscription(
  subs: AlertSubscription[],
  sub: AlertSubscription,
): AlertSubscription[] {
  const next = subs.filter((s) => s.id !== sub.id)
  next.push(sub)
  return next
}

export function removeSubscription(
  subs: AlertSubscription[],
  id: string,
): AlertSubscription[] {
  return subs.filter((s) => s.id !== id)
}

export function toggleMute(
  subs: AlertSubscription[],
  id: string,
): AlertSubscription[] {
  return subs.map((s) => (s.id === id ? { ...s, muted: !s.muted } : s))
}

/** severity a >= severity b ? */
export function severityAtLeast(a: string | null, floor: Severity): boolean {
  return (SEV_RANK[a ?? ''] ?? 0) >= (SEV_RANK[floor] ?? 0)
}

/**
 * Map a WS event payload into a FiredAlert. Returns null when the envelope
 * lacks an id (malformed / non-alert frame).
 */
export function mapAlertEnvelope(
  payload: Record<string, unknown> | undefined,
): FiredAlert | null {
  if (!payload) return null
  const id = typeof payload.id === 'string' ? payload.id : null
  if (!id) return null
  const sevRaw = typeof payload.severity === 'string' ? payload.severity : null
  const severity =
    sevRaw && sevRaw in SEV_RANK ? (sevRaw as Severity) : null
  return {
    id,
    title:
      (typeof payload.title === 'string' && payload.title) ||
      (typeof payload.summary === 'string' && payload.summary) ||
      '(alert)',
    severity,
    target_id: typeof payload.target_id === 'string' ? payload.target_id : null,
    analyst_id: typeof payload.analyst_id === 'string' ? payload.analyst_id : null,
    fired_at:
      typeof payload.produced_at === 'string'
        ? payload.produced_at
        : typeof payload.fired_at === 'string'
          ? payload.fired_at
          : new Date().toISOString(),
    matched_sub_id: null,
  }
}

/**
 * Find the subscription (if any) that a fired alert matches: the alert's
 * scope id must equal a non-muted subscription's scope id AND the alert's
 * severity must meet the subscription's floor. Returns the matched sub id.
 */
export function matchSubscription(
  alert: FiredAlert,
  subs: AlertSubscription[],
): string | null {
  for (const s of subs) {
    if (s.muted) continue
    const scopeVal = s.scope_kind === 'target' ? alert.target_id : alert.analyst_id
    if (scopeVal !== s.scope_id) continue
    if (!severityAtLeast(alert.severity, s.severity_floor)) continue
    return s.id
  }
  return null
}

/** Annotate a fired alert with its matched subscription id (immutable). */
export function annotateMatch(
  alert: FiredAlert,
  subs: AlertSubscription[],
): FiredAlert {
  return { ...alert, matched_sub_id: matchSubscription(alert, subs) }
}
