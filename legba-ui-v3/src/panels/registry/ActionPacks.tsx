/**
 * O6. Action-Pack Grants (`registry.action_packs`).
 *
 * Action packs are a peer descriptor family (source / target / analyst /
 * action_pack). Per PIVOT §4.8 the EFFECTIVE capability of an analyst on a
 * target is the three-way intersection, gated by each pack's governor:
 *
 *     analyst.action_packs  ∩  target/domain.allowed_action_packs  ∩  pack.applicability
 *
 * This operator panel:
 *   (a) lists every ActionPack descriptor (GET /registry/action_packs — the
 *       P-05 ActionPackOut projection, tools / channels / tags / governor
 *       lifted to the row),
 *   (b) grants / revokes a pack to an analyst (its `action_packs`) or a
 *       target/domain (its `allowed_action_packs`) by editing the chosen
 *       descriptor body and PUT-ing it back — the registry re-stamps the
 *       content hash, so a grant is a real, audited new version, and
 *   (c) shows the resulting EFFECTIVE intersection (the GRANT / ALLOW /
 *       APPLICABLE legs) + the pack's governor caps, mirroring the backend's
 *       `resolution.resolve_pack` so the operator sees exactly which rail
 *       would deny a call.
 *
 * Scope selection reuses ScopePicker (descriptor dropdown sourced from the
 * live registry), and — because the three-way intersection needs BOTH an
 * analyst grant set and a target allow set + the target's scope tags — the
 * panel lets the operator bind one analyst AND one target at once so the
 * effective column is the true cross-product, not a single leg.
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { ScopePicker } from '@/components/ScopePicker'
import { DescriptorView } from '@/components/DescriptorView'
import { apiGet, ApiError, readErrorBody } from '@/lib/api'
import type { PanelProps } from '@/types'

// --- ActionPackOut (FROZEN P-05 shape — mirrors registry.api.ActionPackOut) ---
interface ActionPackOut {
  descriptor_id: string
  version: string
  schema_uri: string
  is_head: boolean
  state: string
  owner: string
  name: string
  abstraction_level: string | null
  inherits: string[]
  created_at: string
  retire_after: string | null
  tool_names: string[]
  channel_names: string[]
  applies_to_tags: string[]
  has_governor: boolean
  body: Record<string, unknown>
}

// --- Governor caps, mirrors schemas.action_pack.PackGovernor ---
interface PackGovernor {
  budget_account?: string | null
  max_invocations_per_hour?: number | null
  max_cost_usd_per_day?: number | null
  max_sources_per_window?: number | null
  crawl_max_depth?: number | null
  crawl_max_pages?: number | null
  api_rate_per_minute?: number | null
}

interface ActionPackRef {
  pack_id: string
  governor_override?: PackGovernor | null
}

// --- DescriptorRowOut (generic /descriptors read) ---
interface DescriptorRow {
  descriptor_id: string
  version: string
  state: string
  body: Record<string, unknown>
}

type GrantFamily = 'analyst' | 'target'

/** Field name the grant list lives under, per family. */
const GRANT_FIELD: Record<GrantFamily, 'action_packs' | 'allowed_action_packs'> = {
  analyst: 'action_packs',
  target: 'allowed_action_packs',
}

function stateClass(state: string): string {
  switch (state) {
    case 'active':
      return 'bg-emerald-900 text-emerald-200'
    case 'paused':
      return 'bg-amber-900 text-amber-200'
    case 'retired':
      return 'bg-slate-800 text-slate-400'
    case 'configured':
      return 'bg-sky-900 text-sky-200'
    default:
      return 'bg-slate-700 text-slate-200'
  }
}

const GOVERNOR_CAPS: Array<[keyof PackGovernor, string]> = [
  ['max_invocations_per_hour', 'inv/hr'],
  ['max_cost_usd_per_day', '$/day'],
  ['max_sources_per_window', 'sources/win'],
  ['crawl_max_depth', 'crawl depth'],
  ['crawl_max_pages', 'crawl pages'],
  ['api_rate_per_minute', 'api/min'],
]

function readGrantList(body: Record<string, unknown> | undefined, field: string): ActionPackRef[] {
  const raw = body?.[field]
  if (!Array.isArray(raw)) return []
  return raw
    .map((r) => (typeof r === 'object' && r ? (r as ActionPackRef) : null))
    .filter((r): r is ActionPackRef => !!r && typeof r.pack_id === 'string')
}

/** PUT a descriptor body back to the registry — apiPost only does POST. */
async function putDescriptor(
  family: string,
  id: string,
  body: Record<string, unknown>,
): Promise<{ version: string }> {
  const token = localStorage.getItem('legba_token')
  const headers: HeadersInit = {
    'Content-Type': 'application/json',
    Accept: 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
  const res = await fetch(
    `/api/v1/registry/descriptors/${family}/${encodeURIComponent(id)}`,
    { method: 'PUT', headers, body: JSON.stringify(body) },
  )
  if (!res.ok) {
    throw new ApiError(res.status, await readErrorBody(res))
  }
  return res.json() as Promise<{ version: string }>
}

const VERSION_SENTINEL = '0'.repeat(16)

/** Re-stamp identity.version with the sentinel so the registry hashes it. */
function ensureSentinelVersion(body: Record<string, unknown>): Record<string, unknown> {
  const out = { ...body }
  const identity = (out.identity as Record<string, unknown> | undefined) ?? {}
  out.identity = { ...identity, version: VERSION_SENTINEL }
  return out
}

interface ResolutionLeg {
  granted: boolean
  allowed: boolean
  applicable: boolean
  applicableReason: string
  effective: boolean
}

/**
 * Mirror `resolution.resolve_pack`'s tag-applicability + grant/allow legs,
 * client-side, for the (analyst, target) the operator has bound.
 *
 * The Starlark `applicability_predicate` is a SERVER-evaluated gate — we can't
 * run it in the browser, so when present we surface it (and treat its leg as
 * "pending server eval") rather than guessing. The tag overlap leg is fully
 * computable here and matches the backend.
 */
function resolvePack(
  pack: ActionPackOut,
  analystGrants: ActionPackRef[],
  targetAllows: ActionPackRef[],
  targetTags: string[],
): ResolutionLeg {
  const id = pack.descriptor_id
  const granted = analystGrants.some((r) => r.pack_id === id)
  const allowed = targetAllows.some((r) => r.pack_id === id)

  const tags = pack.applies_to_tags ?? []
  const hasPredicate = !!(pack.body?.applicability_predicate)
  let applicable = true
  let applicableReason = 'no applicability constraint'
  if (tags.length > 0) {
    const overlap = tags.filter((t) => targetTags.includes(t))
    if (overlap.length === 0) {
      applicable = false
      applicableReason = `tags [${tags.join(', ')}] do not overlap target tags [${targetTags.join(', ')}]`
    } else {
      applicableReason = `tag overlap: ${overlap.join(', ')}`
    }
  }
  if (hasPredicate) {
    applicableReason += applicable
      ? ' · predicate gate evaluated server-side'
      : ''
  }

  return {
    granted,
    allowed,
    applicable,
    applicableReason,
    // Predicate leg is server-evaluated; effective here is the operator-visible
    // approximation (all browser-computable legs pass). The runtime gate is
    // authoritative.
    effective: granted && allowed && applicable,
  }
}

function Leg({ ok, label, pending }: { ok: boolean; label: string; pending?: boolean }) {
  return (
    <span
      className={`rounded px-1 text-[10px] ${
        pending
          ? 'bg-slate-800 text-slate-400'
          : ok
            ? 'bg-emerald-900 text-emerald-200'
            : 'bg-rose-900 text-rose-200'
      }`}
    >
      {ok ? '✓' : pending ? '?' : '✗'} {label}
    </span>
  )
}

export default function RegistryActionPacksPanel({ registration }: PanelProps) {
  const qc = useQueryClient()
  const [query, setQuery] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [analystId, setAnalystId] = useState('')
  const [targetId, setTargetId] = useState('')
  const [busyPackId, setBusyPackId] = useState<string | null>(null)
  const [grantError, setGrantError] = useState<string | null>(null)

  const packsQ = useQuery<ActionPackOut[]>({
    queryKey: ['registry-action-packs'],
    queryFn: () =>
      apiGet<ActionPackOut[]>('/registry/action_packs?head_only=true&limit=500'),
    refetchInterval: 60_000,
  })

  // The two grant-bearing descriptors (only fetched when a scope is picked).
  const analystQ = useQuery<DescriptorRow>({
    queryKey: ['action-pack-grant', 'analyst', analystId],
    enabled: !!analystId,
    queryFn: () =>
      apiGet<DescriptorRow>(
        `/registry/descriptors/analyst/${encodeURIComponent(analystId)}`,
      ),
  })
  const targetQ = useQuery<DescriptorRow>({
    queryKey: ['action-pack-grant', 'target', targetId],
    enabled: !!targetId,
    queryFn: () =>
      apiGet<DescriptorRow>(
        `/registry/descriptors/target/${encodeURIComponent(targetId)}`,
      ),
  })

  const analystGrants = useMemo(
    () => readGrantList(analystQ.data?.body, 'action_packs'),
    [analystQ.data],
  )
  const targetAllows = useMemo(
    () => readGrantList(targetQ.data?.body, 'allowed_action_packs'),
    [targetQ.data],
  )
  const targetTags = useMemo(() => {
    const scope = targetQ.data?.body?.scope as { tags?: unknown } | undefined
    const t = scope?.tags
    return Array.isArray(t) ? (t as string[]) : []
  }, [targetQ.data])

  /** Add or remove a pack from one scope's grant list, then PUT it back. */
  async function toggleGrant(family: GrantFamily, packId: string, grant: boolean) {
    const id = family === 'analyst' ? analystId : targetId
    const row = (family === 'analyst' ? analystQ.data : targetQ.data) ?? null
    if (!id || !row) return
    setBusyPackId(packId)
    setGrantError(null)
    try {
      const field = GRANT_FIELD[family]
      const current = readGrantList(row.body, field)
      const next = grant
        ? current.some((r) => r.pack_id === packId)
          ? current
          : [...current, { pack_id: packId }]
        : current.filter((r) => r.pack_id !== packId)
      const body = ensureSentinelVersion({ ...row.body, [field]: next })
      await putDescriptor(family, id, body)
      await qc.invalidateQueries({ queryKey: ['action-pack-grant', family, id] })
      if (family === 'analyst') await analystQ.refetch()
      else await targetQ.refetch()
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? typeof e.body === 'object' && e.body && 'detail' in e.body
            ? String((e.body as { detail: unknown }).detail)
            : `HTTP ${e.status}`
          : (e as Error).message
      setGrantError(`grant update failed: ${msg}`)
    } finally {
      setBusyPackId(null)
    }
  }

  const filtered = useMemo(() => {
    return (packsQ.data ?? []).filter((row) => {
      if (!query) return true
      const q = query.toLowerCase()
      const hay = `${row.descriptor_id} ${row.name} ${(row.applies_to_tags ?? []).join(' ')} ${(
        row.tool_names ?? []
      ).join(' ')}`.toLowerCase()
      return hay.includes(q)
    })
  }, [packsQ.data, query])

  const haveScope = !!analystId || !!targetId

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${filtered.length} action pack${filtered.length === 1 ? '' : 's'}`}
      onRefresh={() => packsQ.refetch()}
    >
      {/* Grant-scope selectors — pick an analyst and/or a target to resolve
          the effective intersection against. */}
      <div className="bg-surface-200 border border-slate-700 rounded p-2 mb-2 text-xs space-y-2">
        <div className="text-slate-400 text-[10px] uppercase tracking-wide">
          effective capability = analyst.action_packs ∩ target.allowed_action_packs ∩ pack.applicability
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-slate-500">analyst grant:</span>
          <ScopePicker
            family="analyst"
            value={analystId}
            onChange={setAnalystId}
            placeholder="select analyst…"
            testId="action-pack-analyst-picker"
          />
          <span className="text-slate-500">target allow:</span>
          <ScopePicker
            family="target"
            value={targetId}
            onChange={setTargetId}
            placeholder="select target/domain…"
            testId="action-pack-target-picker"
          />
        </div>
        {targetId && (
          <div className="text-slate-600 text-[10px]" data-testid="action-pack-target-tags">
            target scope tags: {targetTags.length ? targetTags.join(', ') : '(none)'}
          </div>
        )}
        {(analystQ.isLoading || targetQ.isLoading) && (
          <span className="text-slate-500 text-[10px]">loading scope…</span>
        )}
        {grantError && (
          <div className="text-rose-400 text-[10px]" data-testid="action-pack-grant-error">
            {grantError}
          </div>
        )}
      </div>

      <div className="flex items-center gap-2 mb-2 text-xs">
        <input
          className="flex-1 bg-surface-200 border border-slate-700 rounded p-1 px-2"
          placeholder="filter by id / name / tag / tool…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          data-testid="action-packs-search"
        />
        {packsQ.isLoading && <span className="text-slate-500">loading…</span>}
      </div>

      {packsQ.error instanceof Error && (
        <div className="text-rose-400 text-sm">error: {packsQ.error.message}</div>
      )}

      <div className="flex-1 overflow-auto text-xs space-y-1" data-testid="action-packs-list">
        {filtered.length === 0 && !packsQ.isLoading && (
          <div className="text-slate-500 text-center py-4">no action packs match</div>
        )}
        {filtered.map((row) => {
          const expanded = expandedId === row.descriptor_id
          const governor = (row.body?.governor as PackGovernor) ?? {}
          const granted = analystGrants.some((r) => r.pack_id === row.descriptor_id)
          const allowed = targetAllows.some((r) => r.pack_id === row.descriptor_id)
          const res = resolvePack(row, analystGrants, targetAllows, targetTags)
          const hasPredicate = !!row.body?.applicability_predicate
          const busy = busyPackId === row.descriptor_id
          return (
            <div
              key={row.descriptor_id}
              className="bg-surface-100 border border-slate-800 rounded p-2"
              data-testid={`action-pack-row-${row.descriptor_id}`}
            >
              <button
                onClick={() => setExpandedId(expanded ? null : row.descriptor_id)}
                className="w-full text-left"
              >
                <div className="flex items-baseline gap-2">
                  <span className={`shrink-0 rounded px-1 text-[10px] ${stateClass(row.state)}`}>
                    {row.state}
                  </span>
                  {row.has_governor && (
                    <span className="shrink-0 rounded px-1 text-[10px] bg-violet-900 text-violet-200">
                      governed
                    </span>
                  )}
                  <span className="text-slate-200 truncate flex-1">{row.descriptor_id}</span>
                  <span className="text-slate-600 font-mono text-[10px] shrink-0">
                    @{row.version.slice(0, 8)}
                  </span>
                </div>
                <div className="text-slate-500 mt-1 truncate">{row.name}</div>
                <div className="text-slate-600 text-[10px] mt-0.5 flex flex-wrap gap-2">
                  {row.tool_names.length > 0 && <span>tools: {row.tool_names.join(', ')}</span>}
                  {row.channel_names.length > 0 && (
                    <span>channels: {row.channel_names.join(', ')}</span>
                  )}
                  {row.applies_to_tags.length > 0 && (
                    <span>tags: {row.applies_to_tags.join(', ')}</span>
                  )}
                </div>
                {/* Effective intersection legs — only meaningful once a scope
                    is bound; otherwise grey out. */}
                {haveScope && (
                  <div
                    className="mt-1 flex flex-wrap items-center gap-1"
                    data-testid={`action-pack-legs-${row.descriptor_id}`}
                  >
                    <Leg ok={res.granted} label="GRANT" pending={!analystId} />
                    <Leg ok={res.allowed} label="ALLOW" pending={!targetId} />
                    <Leg ok={res.applicable} label="APPLICABLE" />
                    <span
                      className={`rounded px-1 text-[10px] font-semibold ${
                        res.effective && analystId && targetId
                          ? 'bg-emerald-800 text-emerald-100'
                          : 'bg-slate-800 text-slate-400'
                      }`}
                      data-testid={`action-pack-effective-${row.descriptor_id}`}
                    >
                      {res.effective && analystId && targetId ? 'EFFECTIVE' : 'not effective'}
                    </span>
                  </div>
                )}
              </button>

              {expanded && (
                <div className="mt-2 space-y-2">
                  {/* Grant / revoke controls */}
                  <div className="flex flex-wrap items-center gap-2">
                    {analystId && (
                      <button
                        onClick={() => toggleGrant('analyst', row.descriptor_id, !granted)}
                        disabled={busy || analystQ.isLoading}
                        className={`rounded px-2 py-1 text-[10px] disabled:opacity-50 ${
                          granted
                            ? 'bg-rose-900 hover:bg-rose-800 text-rose-200'
                            : 'bg-emerald-900 hover:bg-emerald-800 text-emerald-200'
                        }`}
                        data-testid={`action-pack-grant-analyst-${row.descriptor_id}`}
                      >
                        {busy
                          ? '…'
                          : granted
                            ? `revoke from analyst ${analystId}`
                            : `grant to analyst ${analystId}`}
                      </button>
                    )}
                    {targetId && (
                      <button
                        onClick={() => toggleGrant('target', row.descriptor_id, !allowed)}
                        disabled={busy || targetQ.isLoading}
                        className={`rounded px-2 py-1 text-[10px] disabled:opacity-50 ${
                          allowed
                            ? 'bg-rose-900 hover:bg-rose-800 text-rose-200'
                            : 'bg-emerald-900 hover:bg-emerald-800 text-emerald-200'
                        }`}
                        data-testid={`action-pack-allow-target-${row.descriptor_id}`}
                      >
                        {busy
                          ? '…'
                          : allowed
                            ? `revoke from target ${targetId}`
                            : `allow on target ${targetId}`}
                      </button>
                    )}
                    {!haveScope && (
                      <span className="text-slate-500 text-[10px]">
                        pick an analyst and/or target above to grant / revoke
                      </span>
                    )}
                  </div>

                  {haveScope && (
                    <div
                      className="text-slate-500 text-[10px]"
                      data-testid={`action-pack-reason-${row.descriptor_id}`}
                    >
                      applicability: {res.applicableReason}
                    </div>
                  )}

                  {/* Governor caps */}
                  <div>
                    <div className="text-slate-400 text-[10px] uppercase tracking-wide mb-1">
                      governor caps
                    </div>
                    {row.has_governor ? (
                      <div
                        className="flex flex-wrap gap-2 text-[10px]"
                        data-testid={`action-pack-governor-${row.descriptor_id}`}
                      >
                        {governor.budget_account && (
                          <span className="bg-surface-200 rounded px-1 text-slate-300">
                            account: {governor.budget_account}
                          </span>
                        )}
                        {GOVERNOR_CAPS.map(([key, label]) => {
                          const v = governor[key]
                          if (v === null || v === undefined) return null
                          return (
                            <span
                              key={key}
                              className="bg-surface-200 rounded px-1 text-slate-300"
                            >
                              {label}: {String(v)}
                            </span>
                          )
                        })}
                      </div>
                    ) : (
                      <div className="text-slate-600 text-[10px]">uncapped (no governor)</div>
                    )}
                  </div>

                  {hasPredicate && (
                    <div className="text-amber-300/80 text-[10px]">
                      applicability_predicate present — evaluated server-side at run time
                    </div>
                  )}

                  <DescriptorView
                    body={row.body}
                    primaryKeys={['name', 'tools', 'channels', 'tags', 'governor', 'description']}
                  />
                </div>
              )}
            </div>
          )
        })}
      </div>
    </PanelChrome>
  )
}
