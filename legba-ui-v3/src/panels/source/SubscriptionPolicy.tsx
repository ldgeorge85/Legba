/**
 * UI-2 / Tier C — source subscription-policy LOCKING panel.
 *
 * A source declares WHO may subscribe via `subscription_policy` (source.py /
 * runtime/subscription/policy.py):
 *   - open       — any target in the same tenant (or a `shared` source)
 *   - allowlist  — only listed targets / tenants
 *   - grant      — each target needs an explicit grant (a subscription_grant
 *                  wiring_descriptor keyed (source_id, target_id))
 *
 * Enforcement happens at subscription REGISTRATION (the control plane), not at
 * delivery. This panel is the operator surface for that gate:
 *
 *   1. View — the selected source's current policy + allowlist / tenant list.
 *   2. Set  — flip the policy and edit allowed_targets / allowed_tenants, then
 *             save by PUTing the patched source body back through the same
 *             registry path the inline DescriptorEditor uses
 *             (PUT /registry/descriptors/source/{id}; identity.version is
 *             re-stamped server-side).
 *   3. Refusal preview — enumerate the registered targets and show, per target,
 *             whether it would be ALLOWED or REFUSED a subscription and WHY,
 *             mirroring policy.py::enforce_subscription's decision table + reason
 *             strings (no round-trip; the gate is re-checked authoritatively on
 *             registration).
 *   4. Grants — for a `grant` source, the registry read surface does not expose
 *             wiring_descriptor grants, so per refused target we surface the
 *             stable grant id and a copy-ready subscription_grant wiring body
 *             (matching policy.py::write_grant), plus a local "treat as granted"
 *             toggle so the preview reflects a grant you've recorded out-of-band.
 *
 * Source selection: panel scope (`data_query.source_id`) OR the
 * `legba:open-source-detail` cross-panel event (shared with source.detail /
 * source.fanout), OR the ScopePicker.
 */

import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { ScopePicker } from '@/components/ScopePicker'
import { apiGet, ApiError, readErrorBody } from '@/lib/api'
import type { PanelProps } from '@/types'
import { useSelection } from '@/state/selection'
import type { SourceDescriptorOut } from './sourceTypes'
import {
  SUBSCRIPTION_POLICIES,
  SUBSCRIPTION_POLICY_HELP,
  decideSubscription,
  grantDescriptorId,
  parseIdList,
  type SourcePolicySlice,
  type SubscriptionPolicy,
  type TargetCandidate,
} from './policyModel'

const VERSION_SENTINEL = '0'.repeat(16)
const DEFAULT_TENANT = 'default'

/** Registry descriptor row (GET /registry/descriptors?family=target). */
interface TargetRow {
  descriptor_id: string
  name?: string | null
  state?: string | null
  body?: Record<string, unknown>
}

/** Read a target's tenant from its body (scope.owner_tenant) or fall back to
 *  the panel-wide default tenant — the engine receives `target_tenant` at
 *  registration; target descriptors don't pin it, so this is the operator's
 *  best estimate of how the target would subscribe. */
function targetTenant(row: TargetRow, fallback: string): string {
  const scope = (row.body?.scope as Record<string, unknown> | undefined) ?? {}
  const t = scope.owner_tenant
  return typeof t === 'string' && t.trim() ? t : fallback
}

/** Re-stamp identity.version to the sentinel so the registry mints a fresh
 *  content-hash version (mirrors DescriptorEditor::ensureSentinelVersion). */
function ensureSentinelVersion(body: Record<string, unknown>): Record<string, unknown> {
  const out = { ...body }
  const identity = (out.identity as Record<string, unknown> | undefined) ?? {}
  if (!identity.version || /^[0a-fA-F]{0,15}$/.test(String(identity.version))) {
    out.identity = { ...identity, version: VERSION_SENTINEL }
  }
  return out
}

/** PUT the patched source body back through the registry (same path as the
 *  inline DescriptorEditor — apiPost only does POST, so we issue PUT here). */
async function putSource(id: string, body: Record<string, unknown>): Promise<{ version: string }> {
  const token = localStorage.getItem('legba_token')
  const headers: HeadersInit = {
    'Content-Type': 'application/json',
    Accept: 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
  const res = await fetch(`/api/v1/registry/descriptors/source/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers,
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    throw new ApiError(res.status, await readErrorBody(res))
  }
  return res.json() as Promise<{ version: string }>
}

export default function SubscriptionPolicyPanel({ registration, scope }: PanelProps) {
  const initial =
    (registration.data_query?.source_id as string | undefined) ??
    (scope as { source_id?: string }).source_id ??
    ''
  const [sourceId, setSourceId] = useState(initial)

  // editable policy form (seeded from the loaded source, then operator-driven)
  const [policy, setPolicy] = useState<SubscriptionPolicy>('open')
  const [allowTargets, setAllowTargets] = useState('')
  const [allowTenants, setAllowTenants] = useState('')
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [savedVersion, setSavedVersion] = useState<string | null>(null)

  // refusal-preview controls
  const [defaultTenant, setDefaultTenant] = useState(DEFAULT_TENANT)
  // grant policy: targets the operator has recorded a grant for out-of-band,
  // so the preview can reflect "granted" without a grant read endpoint.
  const [grantedTargets, setGrantedTargets] = useState<Set<string>>(() => new Set())

  // Redesign Move 2: follow the shared selection when it's a source (replaces
  // the legacy `legba:open-source-detail` window listener).
  const selection = useSelection((s) => s.selection)
  useEffect(() => {
    if (selection?.kind === 'source') setSourceId(selection.id)
  }, [selection])

  const enabled = sourceId.trim().length > 0

  const desc = useQuery<SourceDescriptorOut>({
    enabled,
    queryKey: ['policy-source', sourceId],
    queryFn: () => apiGet<SourceDescriptorOut>(`/registry/sources/${encodeURIComponent(sourceId)}`),
  })

  // Seed the editable form whenever a fresh source loads (and reset dirty/grants).
  useEffect(() => {
    if (!desc.data) return
    const body = desc.data.body as {
      subscription_policy?: string
      allowed_targets?: string[]
      allowed_tenants?: string[]
    }
    const p = (desc.data.subscription_policy ?? body.subscription_policy ?? 'open') as SubscriptionPolicy
    setPolicy(SUBSCRIPTION_POLICIES.includes(p) ? p : 'open')
    setAllowTargets((body.allowed_targets ?? []).join('\n'))
    setAllowTenants((body.allowed_tenants ?? []).join('\n'))
    setDirty(false)
    setSaveError(null)
    setSavedVersion(null)
    setGrantedTargets(new Set())
  }, [desc.data])

  const ownerTenant = (() => {
    const scopeBody = (desc.data?.body?.scope as Record<string, unknown> | undefined) ?? {}
    const t = scopeBody.owner_tenant
    return (typeof t === 'string' && t.trim()) || desc.data?.owner_tenant || DEFAULT_TENANT
  })()

  const editedTargets = useMemo(() => parseIdList(allowTargets), [allowTargets])
  const editedTenants = useMemo(() => parseIdList(allowTenants), [allowTenants])

  // The policy slice the refusal preview decides against — the live EDITED
  // form (so flipping the policy / editing the allowlist updates the preview
  // before you save).
  const policySlice: SourcePolicySlice = useMemo(
    () => ({
      source_id: sourceId,
      owner_tenant: ownerTenant,
      subscription_policy: policy,
      allowed_targets: editedTargets,
      allowed_tenants: editedTenants,
    }),
    [sourceId, ownerTenant, policy, editedTargets, editedTenants],
  )

  // Registered targets to test against the policy.
  const targets = useQuery<TargetRow[]>({
    enabled,
    queryKey: ['policy-targets'],
    queryFn: () =>
      apiGet<TargetRow[]>('/registry/descriptors?family=target&head_only=true&limit=500'),
  })

  const candidates: TargetCandidate[] = useMemo(
    () =>
      (targets.data ?? []).map((r) => ({
        target_id: r.descriptor_id,
        target_tenant: targetTenant(r, defaultTenant),
      })),
    [targets.data, defaultTenant],
  )

  const decisions = useMemo(
    () => candidates.map((c) => decideSubscription(policySlice, c, grantedTargets)),
    [candidates, policySlice, grantedTargets],
  )

  const allowedCount = decisions.filter((d) => d.allowed).length
  const refusedCount = decisions.length - allowedCount

  function markPolicy(p: SubscriptionPolicy) {
    setPolicy(p)
    setDirty(true)
  }

  async function save() {
    if (!desc.data) return
    setSaving(true)
    setSaveError(null)
    setSavedVersion(null)
    try {
      // Patch the existing body in place — the source schema is extra="forbid",
      // so we only touch the three policy fields and re-stamp the version.
      const patched = ensureSentinelVersion({
        ...(desc.data.body as Record<string, unknown>),
        subscription_policy: policy,
        allowed_targets: editedTargets,
        allowed_tenants: editedTenants,
      })
      const result = await putSource(sourceId, patched)
      setSavedVersion(result.version)
      setDirty(false)
      void desc.refetch()
    } catch (e) {
      setSaveError(
        e instanceof ApiError ? JSON.stringify(e.body, null, 2) : (e as Error).message,
      )
    } finally {
      setSaving(false)
    }
  }

  function toggleGranted(targetId: string) {
    setGrantedTargets((prev) => {
      const next = new Set(prev)
      if (next.has(targetId)) next.delete(targetId)
      else next.add(targetId)
      return next
    })
  }

  return (
    <PanelChrome
      registration={registration}
      subtitle={
        enabled
          ? `${policy}${dirty ? ' (unsaved)' : ''} · ${allowedCount} allowed / ${refusedCount} refused`
          : 'select a source'
      }
      onRefresh={
        enabled
          ? () => {
              desc.refetch()
              targets.refetch()
            }
          : undefined
      }
    >
      <div className="flex items-center gap-2 mb-3 text-xs">
        <ScopePicker
          family="source"
          value={sourceId}
          onChange={setSourceId}
          placeholder="select a source to lock…"
          className="flex-1 bg-surface-200 border border-slate-700 rounded p-1 px-2 font-mono text-slate-200"
          testId="policy-source-id"
        />
      </div>

      {!enabled && (
        <div className="text-slate-500 text-sm py-4 text-center" data-testid="policy-empty">
          pick a source — or open one from the Source Registry / Detail panel
        </div>
      )}

      {desc.error instanceof Error && (
        <div className="text-rose-400 text-sm" data-testid="policy-error">
          error: {desc.error.message}
        </div>
      )}

      {enabled && desc.data && (
        <div className="flex-1 overflow-auto text-xs space-y-3" data-testid="policy-body">
          {/* policy editor */}
          <section className="bg-surface-100 border border-slate-800 rounded p-2 space-y-2">
            <div className="flex items-baseline gap-2">
              <span className="text-slate-400 text-[10px] uppercase tracking-wide">
                subscription policy
              </span>
              <span className="text-slate-500 text-[10px] ml-auto">
                owner tenant: <span className="font-mono text-slate-300">{ownerTenant}</span>
              </span>
            </div>
            <div className="flex gap-1">
              {SUBSCRIPTION_POLICIES.map((p) => (
                <button
                  key={p}
                  onClick={() => markPolicy(p)}
                  className={`rounded px-2 py-0.5 text-[11px] ${
                    policy === p
                      ? p === 'open'
                        ? 'bg-emerald-900 text-emerald-200'
                        : p === 'allowlist'
                          ? 'bg-amber-900 text-amber-200'
                          : 'bg-rose-900 text-rose-200'
                      : 'bg-surface-200 text-slate-400 hover:text-slate-200'
                  }`}
                  data-testid={`policy-set-${p}`}
                >
                  {p === 'open' ? '🔓 open' : p === 'allowlist' ? '📋 allowlist' : '🔒 grant'}
                </button>
              ))}
            </div>
            <div className="text-slate-500 text-[10px]" data-testid="policy-help">
              {SUBSCRIPTION_POLICY_HELP[policy]}
            </div>

            {/* allowlist editors — only meaningful for the allowlist policy */}
            {policy === 'allowlist' && (
              <div className="grid grid-cols-2 gap-2" data-testid="policy-allowlist">
                <label className="block">
                  <span className="text-slate-500 text-[10px]">
                    allowed_targets ({editedTargets.length})
                  </span>
                  <textarea
                    className="w-full bg-surface-100 border border-slate-800 rounded p-1 px-2 text-[11px] font-mono h-20"
                    placeholder={'target.brazil.osint\ntarget.argentina.osint'}
                    value={allowTargets}
                    onChange={(e) => {
                      setAllowTargets(e.target.value)
                      setDirty(true)
                    }}
                    data-testid="policy-allowed-targets"
                  />
                </label>
                <label className="block">
                  <span className="text-slate-500 text-[10px]">
                    allowed_tenants ({editedTenants.length})
                  </span>
                  <textarea
                    className="w-full bg-surface-100 border border-slate-800 rounded p-1 px-2 text-[11px] font-mono h-20"
                    placeholder={'default\nacme'}
                    value={allowTenants}
                    onChange={(e) => {
                      setAllowTenants(e.target.value)
                      setDirty(true)
                    }}
                    data-testid="policy-allowed-tenants"
                  />
                </label>
              </div>
            )}

            <div className="flex items-center gap-2 pt-1">
              <button
                onClick={save}
                disabled={!dirty || saving}
                className="bg-sky-900 hover:bg-sky-800 disabled:opacity-40 text-sky-200 rounded px-2 py-1 text-[11px]"
                data-testid="policy-save"
              >
                {saving ? 'saving…' : dirty ? 'save policy' : 'saved'}
              </button>
              {savedVersion && (
                <span className="text-emerald-400 text-[10px]" data-testid="policy-saved">
                  ✓ saved @{savedVersion.slice(0, 12)}
                </span>
              )}
            </div>
            {saveError && (
              <pre
                className="text-rose-300 text-[10px] bg-rose-900/20 border border-rose-800 rounded p-1 overflow-x-auto"
                data-testid="policy-save-error"
              >
                {saveError}
              </pre>
            )}
          </section>

          {/* refusal preview */}
          <section
            className="bg-surface-100 border border-slate-800 rounded p-2"
            data-testid="policy-refusal"
          >
            <div className="flex items-center gap-2 mb-1">
              <span className="text-slate-400 text-[10px] uppercase tracking-wide">
                would-be subscriptions ({decisions.length} targets)
              </span>
              <label className="ml-auto flex items-center gap-1 text-[10px] text-slate-500">
                default tenant
                <input
                  className="bg-surface-200 border border-slate-700 rounded px-1 py-0.5 text-[10px] font-mono w-20"
                  value={defaultTenant}
                  onChange={(e) => setDefaultTenant(e.target.value)}
                  data-testid="policy-default-tenant"
                />
              </label>
            </div>
            <div className="flex gap-3 mb-1 text-[10px]">
              <span className="text-emerald-400" data-testid="policy-allowed-count">
                {allowedCount} allowed
              </span>
              <span className="text-rose-400" data-testid="policy-refused-count">
                {refusedCount} refused
              </span>
            </div>

            {targets.isLoading && <div className="text-slate-500">loading targets…</div>}
            {!targets.isLoading && decisions.length === 0 && (
              <div className="text-slate-500">no registered targets to evaluate</div>
            )}

            <div className="space-y-0.5 max-h-72 overflow-auto">
              {decisions.map((d) => {
                const cand = candidates.find((c) => c.target_id === d.target_id)
                return (
                  <div
                    key={d.target_id}
                    className={`rounded px-2 py-1 text-[11px] border ${
                      d.allowed
                        ? 'bg-emerald-950/40 border-emerald-900'
                        : 'bg-rose-950/40 border-rose-900'
                    }`}
                    data-testid={`policy-decision-${d.target_id}`}
                  >
                    <div className="flex items-baseline gap-2">
                      <span
                        className={`shrink-0 ${d.allowed ? 'text-emerald-400' : 'text-rose-400'}`}
                      >
                        {d.allowed ? 'ALLOWED' : 'REFUSED'}
                      </span>
                      <span className="text-slate-200 truncate flex-1 font-mono">{d.target_id}</span>
                      <span className="text-slate-600 shrink-0">
                        tenant {cand?.target_tenant ?? '—'}
                      </span>
                    </div>
                    {!d.allowed && (
                      <div className="text-rose-300/80 mt-0.5">{d.reason}</div>
                    )}
                    {/* grant policy: per-refused-target grant id + local toggle */}
                    {policy === 'grant' && d.grantRequired && (
                      <div className="mt-1 flex items-center gap-2">
                        <code className="text-amber-300/90 text-[10px] truncate flex-1">
                          {grantDescriptorId(sourceId, d.target_id)}
                        </code>
                        <button
                          onClick={() => toggleGranted(d.target_id)}
                          className="shrink-0 rounded px-1.5 py-0.5 text-[10px] border border-amber-800 text-amber-300 hover:bg-amber-900/40"
                          data-testid={`policy-grant-${d.target_id}`}
                        >
                          treat as granted
                        </button>
                      </div>
                    )}
                    {policy === 'grant' && d.allowed && grantedTargets.has(d.target_id) && (
                      <div className="mt-0.5 flex items-center gap-2">
                        <span className="text-amber-300/70 text-[10px]">granted (local)</span>
                        <button
                          onClick={() => toggleGranted(d.target_id)}
                          className="rounded px-1.5 py-0.5 text-[10px] border border-slate-700 text-slate-400 hover:text-slate-200"
                          data-testid={`policy-revoke-${d.target_id}`}
                        >
                          undo
                        </button>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </section>

          {/* grant body helper — only for the grant policy */}
          {policy === 'grant' && grantedTargets.size > 0 && (
            <section data-testid="policy-grant-bodies">
              <div className="text-slate-400 text-[10px] uppercase tracking-wide mb-1">
                subscription_grant wiring bodies — record these as
                wiring_descriptors ({grantedTargets.size})
              </div>
              <pre
                className="bg-surface-200 p-2 rounded overflow-x-auto text-[10px] text-amber-200 max-h-48"
                data-testid="policy-grant-json"
              >
                {JSON.stringify(
                  [...grantedTargets].map((t) => ({
                    descriptor_id: grantDescriptorId(sourceId, t),
                    schema_uri: 'legba/wiring/subscription_grant/1.0.0',
                    body: {
                      kind: 'subscription_grant',
                      source_id: sourceId,
                      target_id: t,
                      reason: '',
                    },
                  })),
                  null,
                  2,
                )}
              </pre>
              <button
                onClick={() => {
                  void navigator.clipboard?.writeText(
                    document.querySelector('[data-testid="policy-grant-json"]')?.textContent ?? '',
                  )
                }}
                className="mt-1 bg-amber-900 hover:bg-amber-800 text-amber-200 rounded px-2 py-1 text-[11px]"
                data-testid="policy-grant-copy"
              >
                copy grant bodies
              </button>
            </section>
          )}
        </div>
      )}
    </PanelChrome>
  )
}
