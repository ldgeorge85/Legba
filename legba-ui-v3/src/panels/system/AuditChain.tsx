/**
 * S9 / UI-5 (Tier F). Audit-Chain Browser (`system.audit`).
 *
 * Walk the descriptor audit log — every register/update/promote is signed
 * (Ed25519) and re-verified inline by the registry against its signing
 * identity. Each entry carries `signature_verified` (true/false, or null in
 * reader-only mode where no verifier is configured).
 *
 * Reads `GET /api/v1/registry/audit?descriptor_id=&family=&since=&limit=` →
 * `AuditEntryOut[]`. A chain-health banner summarises verified / failed /
 * unverifiable; a FAILED entry flags chain tamper and is highlighted.
 *
 * Verify-status + chain-health logic lives in `@/lib/evalOps` (unit-tested).
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet } from '@/lib/api'
import {
  chainHealth,
  verifyStatus,
  type AuditEntryRow,
  type VerifyStatus,
} from '@/lib/evalOps'
import type { PanelProps } from '@/types'

const VERIFY_PILL: Record<VerifyStatus, string> = {
  verified: 'bg-emerald-900 text-emerald-200',
  failed: 'bg-rose-900 text-rose-200',
  unverifiable: 'bg-slate-800 text-slate-400',
}
const VERIFY_GLYPH: Record<VerifyStatus, string> = {
  verified: '✓ verified',
  failed: '✗ FAILED',
  unverifiable: '? unverifiable',
}

const FAMILIES = ['all', 'target', 'analyst', 'source', 'stack', 'action_pack'] as const

/** Roll the entries up by `action` and by `actor_id` for the scannable
 *  summary header — the audit stream is "noisy" otherwise. Pure + local
 *  (the shared `chainHealth` already covers verify counts). */
function summariseAudit(rows: AuditEntryRow[]): {
  byAction: Array<[string, number]>
  byActor: Array<[string, number]>
  descriptors: number
} {
  const action: Record<string, number> = {}
  const actor: Record<string, number> = {}
  const descriptors = new Set<string>()
  for (const e of rows) {
    action[e.action] = (action[e.action] ?? 0) + 1
    actor[e.actor_id] = (actor[e.actor_id] ?? 0) + 1
    descriptors.add(e.descriptor_id)
  }
  const sortDesc = (o: Record<string, number>): Array<[string, number]> =>
    Object.entries(o).sort((a, b) => b[1] - a[1])
  return {
    byAction: sortDesc(action),
    byActor: sortDesc(actor),
    descriptors: descriptors.size,
  }
}

/** Group entries by descriptor_id (most-recently-touched first) for the
 *  collapse-by-descriptor view. Each group keeps its rows newest-first. */
function groupByDescriptor(
  rows: AuditEntryRow[],
): Array<{ descriptor_id: string; namespace: string; rows: AuditEntryRow[] }> {
  const groups = new Map<string, AuditEntryRow[]>()
  for (const e of rows) {
    const list = groups.get(e.descriptor_id) ?? []
    list.push(e)
    groups.set(e.descriptor_id, list)
  }
  const out = Array.from(groups.entries()).map(([descriptor_id, list]) => {
    const sorted = [...list].sort(
      (a, b) => Date.parse(b.occurred_at) - Date.parse(a.occurred_at),
    )
    return { descriptor_id, namespace: sorted[0]?.namespace ?? '', rows: sorted }
  })
  return out.sort(
    (a, b) =>
      Date.parse(b.rows[0]?.occurred_at ?? '') -
      Date.parse(a.rows[0]?.occurred_at ?? ''),
  )
}

export default function AuditChainPanel({ registration }: PanelProps) {
  const [family, setFamily] = useState<(typeof FAMILIES)[number]>('all')
  const [descriptorId, setDescriptorId] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [groupMode, setGroupMode] = useState(false)
  const [openGroup, setOpenGroup] = useState<string | null>(null)

  const params = useMemo(() => {
    const p = new URLSearchParams({ limit: '200' })
    if (family !== 'all') p.set('family', family)
    if (descriptorId.trim()) p.set('descriptor_id', descriptorId.trim())
    return p
  }, [family, descriptorId])

  const { data, isLoading, error, refetch } = useQuery<AuditEntryRow[]>({
    queryKey: ['audit-chain', params.toString()],
    queryFn: () => apiGet<AuditEntryRow[]>(`/registry/audit?${params.toString()}`),
    refetchInterval: 60_000,
  })

  const rows = data ?? []
  const health = useMemo(() => chainHealth(rows), [rows])
  const summary = useMemo(() => summariseAudit(rows), [rows])
  const groups = useMemo(
    () => (groupMode ? groupByDescriptor(rows) : []),
    [groupMode, rows],
  )

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${rows.length} entr${rows.length === 1 ? 'y' : 'ies'} · ${summary.descriptors} descriptor${summary.descriptors === 1 ? '' : 's'}`}
      actions={
        <button
          onClick={() => setGroupMode((v) => !v)}
          className={`text-[10px] px-2 py-0.5 rounded border ${
            groupMode ? 'border-accent-ok text-accent-ok' : 'border-slate-700 text-slate-500'
          }`}
          title="Collapse the audit stream by descriptor"
          data-testid="audit-group-toggle"
        >
          {groupMode ? '▣ grouped' : '☰ flat'}
        </button>
      }
      onRefresh={() => refetch()}
    >
      <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={family}
          onChange={(e) => setFamily(e.target.value as (typeof FAMILIES)[number])}
          data-testid="audit-family-filter"
        >
          {FAMILIES.map((f) => (
            <option key={f} value={f}>
              family: {f}
            </option>
          ))}
        </select>
        <input
          className="flex-1 min-w-[140px] bg-surface-200 border border-slate-700 rounded p-1 px-2"
          placeholder="descriptor_id filter…"
          value={descriptorId}
          onChange={(e) => setDescriptorId(e.target.value)}
          data-testid="audit-descriptor-filter"
        />
        {isLoading && <span className="text-slate-500">loading…</span>}
      </div>

      {/* chain-health banner */}
      {rows.length > 0 && (
        <div
          className={`mb-2 rounded p-2 text-xs border flex items-center gap-3 ${
            health.intact
              ? 'border-emerald-900/50 bg-emerald-950/30'
              : 'border-rose-900/60 bg-rose-950/40'
          }`}
          data-testid="audit-health"
        >
          <span className={`font-semibold ${health.intact ? 'text-emerald-300' : 'text-rose-300'}`}>
            {health.intact ? '✓ chain intact' : '✗ chain tamper detected'}
          </span>
          <span className="text-emerald-400">{health.verified} verified</span>
          {health.failed > 0 && (
            <span className="text-rose-400" data-testid="audit-health-failed">
              {health.failed} failed
            </span>
          )}
          {health.unverifiable > 0 && (
            <span className="text-slate-500">{health.unverifiable} unverifiable</span>
          )}
        </div>
      )}

      {/* action / actor breakdown — makes the noisy stream scannable */}
      {rows.length > 0 && (
        <div className="mb-2 space-y-1" data-testid="audit-summary">
          <div className="flex flex-wrap items-center gap-1">
            <span className="text-slate-500 text-[10px] uppercase tracking-wide mr-1">
              by action
            </span>
            {summary.byAction.map(([action, n]) => (
              <span
                key={action}
                className="rounded px-1.5 py-0.5 text-[10px] bg-surface-200 text-slate-300 border border-slate-700"
              >
                {action}: {n}
              </span>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-1">
            <span className="text-slate-500 text-[10px] uppercase tracking-wide mr-1">
              by actor
            </span>
            {summary.byActor.map(([actor, n]) => (
              <span
                key={actor}
                className="rounded px-1.5 py-0.5 text-[10px] bg-surface-200 text-slate-400 border border-slate-800 font-mono truncate max-w-[160px]"
                title={actor}
              >
                {actor}: {n}
              </span>
            ))}
          </div>
        </div>
      )}

      {error instanceof Error && (
        <div className="text-rose-400 text-sm">error: {error.message}</div>
      )}

      <div className="flex-1 overflow-auto text-xs space-y-1" data-testid="audit-list">
        {!isLoading && rows.length === 0 && (
          <div className="text-slate-500 text-center py-4">no audit entries</div>
        )}

        {/* grouped-by-descriptor view */}
        {groupMode &&
          groups.map((g) => {
            const open = openGroup === g.descriptor_id
            const latest = g.rows[0]
            const groupFailed = g.rows.some((e) => verifyStatus(e) === 'failed')
            return (
              <div
                key={g.descriptor_id}
                className={`bg-surface-100 border rounded ${
                  groupFailed ? 'border-rose-900/60' : 'border-slate-800'
                }`}
                data-testid={`audit-group-${g.descriptor_id}`}
              >
                <button
                  className="w-full text-left p-2"
                  onClick={() => setOpenGroup(open ? null : g.descriptor_id)}
                  data-testid={`audit-group-header-${g.descriptor_id}`}
                >
                  <div className="flex items-baseline gap-2">
                    <span className="text-slate-500 shrink-0">{open ? '▾' : '▸'}</span>
                    {groupFailed && (
                      <span className={`shrink-0 rounded px-1 text-[10px] ${VERIFY_PILL.failed}`}>
                        {VERIFY_GLYPH.failed}
                      </span>
                    )}
                    <span className="text-slate-500 shrink-0">{g.namespace}</span>
                    <span className="text-slate-200 truncate flex-1">{g.descriptor_id}</span>
                    <span className="shrink-0 rounded px-1 text-[10px] bg-surface-200 text-slate-400">
                      {g.rows.length} change{g.rows.length === 1 ? '' : 's'}
                    </span>
                    <span className="text-slate-600 shrink-0">
                      {new Date(latest.occurred_at).toLocaleString()}
                    </span>
                  </div>
                </button>
                {open && (
                  <div className="border-t border-slate-800 p-2 space-y-1">
                    {g.rows.map((e) => {
                      const status = verifyStatus(e)
                      return (
                        <div
                          key={e.id}
                          className="flex items-baseline gap-2 text-[10px]"
                          data-testid={`audit-group-row-${e.id}`}
                        >
                          <span className={`shrink-0 rounded px-1 ${VERIFY_PILL[status]}`}>
                            {VERIFY_GLYPH[status]}
                          </span>
                          <span className="shrink-0 rounded px-1 bg-surface-200 text-slate-300">
                            {e.action}
                          </span>
                          <span className="font-mono text-slate-500">
                            {e.from_version ? `${e.from_version.slice(0, 8)} → ` : ''}
                            {e.to_version ? e.to_version.slice(0, 8) : '—'}
                          </span>
                          <span className="text-slate-500 truncate">
                            by {e.actor_id} ({e.actor_role})
                          </span>
                          <span className="ml-auto text-slate-600 shrink-0">
                            {new Date(e.occurred_at).toLocaleString()}
                          </span>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}

        {/* flat chronological view */}
        {!groupMode &&
          rows.map((e) => {
          const status = verifyStatus(e)
          const open = expanded === e.id
          return (
            <div
              key={e.id}
              className={`bg-surface-100 border rounded p-2 ${
                status === 'failed' ? 'border-rose-900/60' : 'border-slate-800'
              }`}
              data-testid={`audit-row-${e.id}`}
            >
              <button
                className="w-full text-left"
                onClick={() => setExpanded(open ? null : e.id)}
                data-testid={`audit-row-header-${e.id}`}
              >
                <div className="flex items-baseline gap-2">
                  <span
                    className={`shrink-0 rounded px-1 text-[10px] ${VERIFY_PILL[status]}`}
                    data-testid={`audit-verify-${status}`}
                  >
                    {VERIFY_GLYPH[status]}
                  </span>
                  <span className="shrink-0 rounded px-1 text-[10px] bg-surface-200 text-slate-300">
                    {e.action}
                  </span>
                  <span className="text-slate-500 shrink-0">{e.namespace}</span>
                  <span className="text-slate-200 truncate flex-1">{e.descriptor_id}</span>
                  <span className="text-slate-600 shrink-0">
                    {new Date(e.occurred_at).toLocaleString()}
                  </span>
                </div>
                <div className="mt-1 flex flex-wrap gap-x-3 text-[10px] text-slate-500">
                  <span className="font-mono">
                    {e.from_version ? `${e.from_version.slice(0, 8)} → ` : ''}
                    {e.to_version ? e.to_version.slice(0, 8) : '—'}
                  </span>
                  <span>by {e.actor_id} ({e.actor_role})</span>
                </div>
              </button>
              {open && (
                <div className="mt-2 border-t border-slate-800 pt-2 space-y-2">
                  <div className="text-[10px] text-slate-500">
                    <span className="uppercase tracking-wide">signer </span>
                    <code className="font-mono text-slate-400 break-all">{e.signer_did}</code>
                  </div>
                  {status === 'failed' && (
                    <div className="text-[10px] text-rose-300 bg-rose-950/40 rounded p-1.5 border border-rose-900/50">
                      Inline Ed25519 re-verification FAILED against the registry
                      signing identity — this entry's signed payload does not
                      match. Treat the chain as compromised from here.
                    </div>
                  )}
                  {status === 'unverifiable' && (
                    <div className="text-[10px] text-slate-500">
                      No verifier configured (reader-only mode) — signature was
                      not re-checked inline.
                    </div>
                  )}
                  <div>
                    <div className="text-slate-500 text-[10px] uppercase tracking-wide mb-1">
                      change summary
                    </div>
                    <pre className="bg-surface-200 p-2 rounded overflow-x-auto text-[10px] text-slate-300 max-h-48">
                      {JSON.stringify(e.change_summary, null, 2)}
                    </pre>
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </PanelChrome>
  )
}
