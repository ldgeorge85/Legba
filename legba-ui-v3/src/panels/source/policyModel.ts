/**
 * UI-2 (Tier C) — subscription-policy model + refusal decision table.
 *
 * Mirrors the FROZEN backend enforcement at
 * `src/legba/runtime/subscription/policy.py::enforce_subscription` so the
 * Subscription-Policy panel can show — without a round-trip — whether a given
 * target would be ALLOWED or REFUSED a subscription, and *why*, using the same
 * decision table and reason strings the engine raises at registration.
 *
 * The authoritative gate is server-side (enforced at subscription
 * registration, NOT at delivery); this is a faithful client-side preview, the
 * same "preview what the server would do" pattern the Subscription Builder
 * uses for selectors/subscriptions. If `policy.py` changes its table or reason
 * wording, mirror it here.
 */

/** The three policies a source can declare (source.py::SourceDescriptor). */
export const SUBSCRIPTION_POLICIES = ['open', 'allowlist', 'grant'] as const
export type SubscriptionPolicy = (typeof SUBSCRIPTION_POLICIES)[number]

/** policy.py::SHARED_TENANT — a `shared` source crosses tenant boundaries. */
export const SHARED_TENANT = 'shared'

/** The policy slice of a source descriptor needed for the refusal decision.
 *  Mirrors policy.py::SourcePolicy. */
export interface SourcePolicySlice {
  source_id: string
  owner_tenant: string
  subscription_policy: SubscriptionPolicy
  allowed_targets: string[]
  allowed_tenants: string[]
}

/** A target candidate to test against a source's policy. */
export interface TargetCandidate {
  target_id: string
  /** The tenant the target would subscribe as (engine `target_tenant` arg). */
  target_tenant: string
}

export interface PolicyDecision {
  target_id: string
  allowed: boolean
  /** The refusal reason (mirrors SubscriptionPolicyError.reason), or '' when allowed. */
  reason: string
  /** True iff a `grant` source needs an explicit wiring_descriptor grant that
   *  we cannot see from the registry read surface — surfaced so the operator
   *  knows the decision is "grant required" rather than a hard deny. */
  grantRequired: boolean
}

export const SUBSCRIPTION_POLICY_HELP: Record<SubscriptionPolicy, string> = {
  open: 'any target in the same tenant (or a "shared" source) may attach',
  allowlist: 'only listed targets / tenants may attach',
  grant: 'each target needs an explicit grant (a subscription_grant wiring_descriptor)',
}

/**
 * Stable wiring_descriptor id for a (source, target) subscription grant.
 * Mirrors policy.py::grant_descriptor_id — dots in the source id are flattened
 * to underscores so the id reads cleanly and is collision-free per pair.
 */
export function grantDescriptorId(sourceId: string, targetId: string): string {
  const src = sourceId.replaceAll('.', '_')
  return `subgrant.${src}.${targetId}`
}

/**
 * Decide one target→source subscription, mirroring policy.py::enforce_subscription.
 *
 * Decision table (PIVOT §4.4.1):
 *   - cross-tenant to a non-`shared` source → refused unless an
 *     allowlist/grant explicitly widens it.
 *   - `open`      → allow same-tenant / shared.
 *   - `allowlist` → allow only if target ∈ allowed_targets OR
 *     target_tenant ∈ allowed_tenants.
 *   - `grant`     → allow only if a wiring_descriptor grant exists. The registry
 *     read surface does not expose grants, so we mark the decision
 *     `grantRequired` (and pass an explicit set of known-granted target ids when
 *     the caller has them).
 *   - unknown policy → fail closed.
 */
export function decideSubscription(
  source: SourcePolicySlice,
  target: TargetCandidate,
  knownGrantedTargets: ReadonlySet<string> = new Set(),
): PolicyDecision {
  const policy = source.subscription_policy
  const sameTenant =
    target.target_tenant === source.owner_tenant || source.owner_tenant === SHARED_TENANT

  const ok = (): PolicyDecision => ({
    target_id: target.target_id,
    allowed: true,
    reason: '',
    grantRequired: false,
  })
  const deny = (reason: string, grantRequired = false): PolicyDecision => ({
    target_id: target.target_id,
    allowed: false,
    reason,
    grantRequired,
  })

  if (policy === 'open') {
    if (!sameTenant) {
      return deny(
        `cross-tenant: target tenant "${target.target_tenant}" != source ` +
          `tenant "${source.owner_tenant}" and source is not 'shared'`,
      )
    }
    return ok()
  }

  if (policy === 'allowlist') {
    if (source.allowed_targets.includes(target.target_id)) return ok()
    if (source.allowed_tenants.includes(target.target_tenant)) return ok()
    return deny(
      `target "${target.target_id}" (tenant "${target.target_tenant}") not in ` +
        `allowed_targets/allowed_tenants`,
    )
  }

  if (policy === 'grant') {
    if (knownGrantedTargets.has(target.target_id)) return ok()
    return deny(
      `no active subscription grant (wiring_descriptor ` +
        `"${grantDescriptorId(source.source_id, target.target_id)}")`,
      true,
    )
  }

  // Unknown policy → fail closed (mirrors policy.py's trailing raise).
  return deny(`unknown subscription_policy "${policy}"`)
}

/** Parse a comma/space/newline-separated id field into a trimmed, deduped array. */
export function parseIdList(raw: string): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const t of raw.split(/[\s,]+/)) {
    const v = t.trim()
    if (v && !seen.has(v)) {
      seen.add(v)
      out.push(v)
    }
  }
  return out
}
