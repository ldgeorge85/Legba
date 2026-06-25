/**
 * P-UI6-D. Tenant / Customer View (`system.tenant_view`) — UI-6 (Tier G).
 *
 * The multi-tenant surface (TRAVIS_ASM_BRIEF §7): one Legba instance hosts many
 * customers' descriptors. There is NO `owner_tenant` column on the registry
 * read; the tenancy key is the descriptor **`owner`** (top-level on every head
 * descriptor, mirrored at `body.identity.owner`). This panel is the
 * per-customer portfolio dashboard:
 *
 *  - fetch HEAD descriptors across families (target · source · analyst) and
 *    roll them up per `owner`,
 *  - pick an owner → the panel scopes to it: a per-family breakdown (counts +
 *    by-state) and a drill-in roster of that owner's descriptors by family, and
 *  - **broadcast** the active owner via `legba:set-tenant` (detail carries both
 *    `owner` and, for back-compat, `owner_tenant`) so any panel that opts into
 *    tenant scoping picks it up (cross-panel pattern, same shape as
 *    `legba:open-lineage`).
 *
 * Tenant scoping is UI-level over the frozen registry reads; no backend change.
 * The roll-up logic stays trivially testable by feeding mocked endpoint rows.
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet } from '@/lib/api'
import type { PanelProps } from '@/types'

/** A head descriptor row (subset of fields this panel reads). */
interface DescriptorRow {
  descriptor_id: string
  name: string | null
  family: string
  state: string
  owner: string | null
  body?: { identity?: { owner?: string | null } }
}

const FAMILIES = ['target', 'source', 'analyst'] as const
type Family = (typeof FAMILIES)[number]

const UNOWNED = '(unowned)'

async function softList(path: string): Promise<DescriptorRow[]> {
  try {
    const r = await apiGet<unknown>(path)
    if (Array.isArray(r)) return r as DescriptorRow[]
    if (r && typeof r === 'object' && Array.isArray((r as { data?: unknown }).data)) {
      return (r as { data: DescriptorRow[] }).data
    }
    return []
  } catch {
    return []
  }
}

/** Resolve a descriptor's owner: top-level `owner`, then body.identity.owner. */
export function ownerOf(d: DescriptorRow): string {
  return d.owner || d.body?.identity?.owner || UNOWNED
}

/** Broadcast the active owner so opt-in panels can scope to it. */
export function broadcastTenant(owner: string) {
  window.dispatchEvent(
    new CustomEvent('legba:set-tenant', { detail: { owner, owner_tenant: owner } }),
  )
}

function familyQuery(family: Family) {
  return {
    queryKey: ['tenant-descriptors', family],
    queryFn: () => softList(`/registry/descriptors?family=${family}&head_only=true&limit=500`),
  }
}

export default function TenantViewPanel({ registration }: PanelProps) {
  const [owner, setOwner] = useState<string>('')

  // One query per family — explicit calls keep hook order fixed (rules-of-hooks).
  const targetsQ = useQuery<DescriptorRow[]>(familyQuery('target'))
  const sourcesQ = useQuery<DescriptorRow[]>(familyQuery('source'))
  const analystsQ = useQuery<DescriptorRow[]>(familyQuery('analyst'))
  const queries = [targetsQ, sourcesQ, analystsQ] as const

  const isLoading = queries.some((q) => q.isLoading)
  const error = queries.find((q) => q.error)?.error
  function refetchAll() {
    queries.forEach((q) => void q.refetch())
  }

  // Tag every descriptor with its family + resolved owner.
  const all = useMemo(() => {
    const out: Array<DescriptorRow & { _owner: string; _family: Family }> = []
    const byFamily: Array<[Family, DescriptorRow[] | undefined]> = [
      ['target', targetsQ.data],
      ['source', sourcesQ.data],
      ['analyst', analystsQ.data],
    ]
    for (const [fam, rows] of byFamily) {
      for (const d of rows ?? []) out.push({ ...d, _owner: ownerOf(d), _family: fam })
    }
    return out
  }, [targetsQ.data, sourcesQ.data, analystsQ.data])

  // Owners discovered across all families, with per-family counts.
  const owners = useMemo(() => {
    const byOwner = new Map<string, Record<Family, number>>()
    for (const d of all) {
      const row = byOwner.get(d._owner) ?? { target: 0, source: 0, analyst: 0 }
      row[d._family] += 1
      byOwner.set(d._owner, row)
    }
    return Array.from(byOwner.entries())
      .map(([name, counts]) => ({ name, counts, total: counts.target + counts.source + counts.analyst }))
      .sort((a, b) => b.total - a.total || a.name.localeCompare(b.name))
  }, [all])

  // Default the active owner to the largest portfolio once data lands.
  const activeOwner = owner || owners[0]?.name || ''

  const scoped = useMemo(() => all.filter((d) => d._owner === activeOwner), [all, activeOwner])

  // Per-family roll-up for the active owner (count + by-state).
  const familyRollup = useMemo(() => {
    const out: Record<Family, { count: number; states: Record<string, number> }> = {
      target: { count: 0, states: {} },
      source: { count: 0, states: {} },
      analyst: { count: 0, states: {} },
    }
    for (const d of scoped) {
      const r = out[d._family]
      r.count += 1
      r.states[d.state] = (r.states[d.state] ?? 0) + 1
    }
    return out
  }, [scoped])

  function pick(o: string) {
    setOwner(o)
    broadcastTenant(o)
  }

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${owners.length} owner${owners.length === 1 ? '' : 's'}${
        activeOwner ? ` · ${activeOwner}: ${scoped.length} descriptors` : ''
      }`}
      onRefresh={refetchAll}
    >
      {/* owner picker */}
      <div className="flex items-center gap-2 mb-3 text-xs flex-wrap">
        <span className="text-slate-500">owner:</span>
        {owners.map((o) => (
          <button
            key={o.name}
            onClick={() => pick(o.name)}
            className={`px-2 py-0.5 rounded border ${
              o.name === activeOwner
                ? 'border-accent-info text-accent-info'
                : 'border-slate-700 text-slate-400 hover:text-slate-200'
            }`}
            data-testid={`tenant-pick-${o.name}`}
          >
            {o.name} <span className="text-slate-600">({o.total})</span>
          </button>
        ))}
        <input
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2 w-40"
          placeholder="custom owner…"
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              const v = (e.target as HTMLInputElement).value.trim()
              if (v) pick(v)
            }
          }}
          data-testid="tenant-custom"
        />
      </div>

      {isLoading && <div className="text-slate-500 text-sm">loading descriptors…</div>}
      {error instanceof Error && (
        <div className="text-rose-400 text-sm">error: {error.message}</div>
      )}

      {/* per-family roll-up cards */}
      <div className="grid grid-cols-3 gap-2 mb-3 text-xs" data-testid="tenant-rollup">
        {FAMILIES.map((fam) => {
          const r = familyRollup[fam]
          return (
            <div key={fam} className="bg-surface-100 border border-slate-800 rounded p-2" data-testid={`tenant-family-${fam}`}>
              <div className="flex justify-between mb-1">
                <span className="text-slate-300 capitalize">{fam}</span>
                <span className="text-slate-400 font-mono" data-testid={`tenant-family-count-${fam}`}>{r.count}</span>
              </div>
              {Object.keys(r.states).length === 0 && <div className="text-slate-600">none</div>}
              {Object.entries(r.states).map(([st, n]) => (
                <div key={st} className="flex justify-between" data-testid={`tenant-${fam}-state-${st}`}>
                  <span className="text-slate-500">{st}</span>
                  <span className="text-slate-500 font-mono">{n}</span>
                </div>
              ))}
            </div>
          )
        })}
      </div>

      {/* drill-in roster, grouped by family */}
      <div className="flex-1 overflow-auto" data-testid="tenant-descriptors">
        <div className="text-[11px] text-slate-500 mb-1">{activeOwner || '—'} descriptors</div>
        {scoped.length === 0 && !isLoading && (
          <div className="text-slate-600 text-xs">no descriptors for this owner</div>
        )}
        {FAMILIES.map((fam) => {
          const rows = scoped.filter((d) => d._family === fam)
          if (rows.length === 0) return null
          return (
            <div key={fam} className="mb-2">
              <div className="text-[10px] uppercase tracking-wide text-slate-600 mb-0.5">{fam}</div>
              <div className="space-y-1 text-xs">
                {rows.map((d) => (
                  <div
                    key={d.descriptor_id}
                    className="flex items-center gap-2 bg-surface-100 border border-slate-800 rounded p-1.5"
                    data-testid={`tenant-descriptor-${d.descriptor_id}`}
                  >
                    <span className="rounded px-1 bg-violet-900 text-violet-200 shrink-0">{fam}</span>
                    <span className="font-mono text-slate-200 truncate">{d.descriptor_id}</span>
                    {d.name && <span className="text-slate-500 truncate">{d.name}</span>}
                    <span className="ml-auto shrink-0 text-slate-500">{d.state}</span>
                  </div>
                ))}
              </div>
            </div>
          )
        })}
      </div>
    </PanelChrome>
  )
}
