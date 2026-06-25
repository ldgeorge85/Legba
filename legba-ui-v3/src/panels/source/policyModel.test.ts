/**
 * Unit tests for the subscription-policy decision table.
 *
 * These mirror src/legba/runtime/subscription/policy.py::enforce_subscription
 * one decision at a time — the client-side refusal preview MUST agree with the
 * server gate. (The server re-checks authoritatively at registration.)
 */

import { describe, it, expect } from 'vitest'
import {
  decideSubscription,
  grantDescriptorId,
  parseIdList,
  type SourcePolicySlice,
} from './policyModel'

function src(over: Partial<SourcePolicySlice> = {}): SourcePolicySlice {
  return {
    source_id: 'source.rss.brazil',
    owner_tenant: 'default',
    subscription_policy: 'open',
    allowed_targets: [],
    allowed_tenants: [],
    ...over,
  }
}

describe('decideSubscription — open', () => {
  it('allows a same-tenant target', () => {
    const d = decideSubscription(src({ subscription_policy: 'open' }), {
      target_id: 't1',
      target_tenant: 'default',
    })
    expect(d.allowed).toBe(true)
    expect(d.reason).toBe('')
  })

  it('refuses a cross-tenant target on a non-shared source', () => {
    const d = decideSubscription(src({ subscription_policy: 'open', owner_tenant: 'acme' }), {
      target_id: 't1',
      target_tenant: 'default',
    })
    expect(d.allowed).toBe(false)
    expect(d.reason).toContain('cross-tenant')
  })

  it('allows any tenant against a shared source', () => {
    const d = decideSubscription(src({ subscription_policy: 'open', owner_tenant: 'shared' }), {
      target_id: 't1',
      target_tenant: 'whatever',
    })
    expect(d.allowed).toBe(true)
  })
})

describe('decideSubscription — allowlist', () => {
  it('allows a listed target', () => {
    const d = decideSubscription(
      src({ subscription_policy: 'allowlist', allowed_targets: ['t1'] }),
      { target_id: 't1', target_tenant: 'default' },
    )
    expect(d.allowed).toBe(true)
  })

  it('allows a target whose tenant is listed', () => {
    const d = decideSubscription(
      src({ subscription_policy: 'allowlist', allowed_tenants: ['acme'] }),
      { target_id: 't9', target_tenant: 'acme' },
    )
    expect(d.allowed).toBe(true)
  })

  it('refuses an unlisted target', () => {
    const d = decideSubscription(
      src({ subscription_policy: 'allowlist', allowed_targets: ['t1'] }),
      { target_id: 't2', target_tenant: 'default' },
    )
    expect(d.allowed).toBe(false)
    expect(d.reason).toContain('not in')
  })
})

describe('decideSubscription — grant', () => {
  it('refuses by default and reports the grant wiring id', () => {
    const d = decideSubscription(src({ subscription_policy: 'grant' }), {
      target_id: 'target.brazil',
      target_tenant: 'default',
    })
    expect(d.allowed).toBe(false)
    expect(d.grantRequired).toBe(true)
    expect(d.reason).toContain('subgrant.source_rss_brazil.target.brazil')
  })

  it('allows a target present in the known-granted set', () => {
    const d = decideSubscription(
      src({ subscription_policy: 'grant' }),
      { target_id: 'target.brazil', target_tenant: 'default' },
      new Set(['target.brazil']),
    )
    expect(d.allowed).toBe(true)
    expect(d.grantRequired).toBe(false)
  })
})

describe('decideSubscription — fail closed', () => {
  it('refuses an unknown policy', () => {
    const d = decideSubscription(
      // @ts-expect-error — exercising the fail-closed branch with a bad policy
      src({ subscription_policy: 'bananas' }),
      { target_id: 't1', target_tenant: 'default' },
    )
    expect(d.allowed).toBe(false)
    expect(d.reason).toContain('unknown subscription_policy')
  })
})

describe('grantDescriptorId', () => {
  it('flattens dots in the source id (matches policy.py)', () => {
    expect(grantDescriptorId('source.rss.brazil', 'target.x')).toBe(
      'subgrant.source_rss_brazil.target.x',
    )
  })
})

describe('parseIdList', () => {
  it('splits on whitespace/commas and dedupes', () => {
    expect(parseIdList('a, b\nc  a')).toEqual(['a', 'b', 'c'])
    expect(parseIdList('   ')).toEqual([])
  })
})
