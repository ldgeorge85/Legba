/**
 * S5 / UI-5 (Tier F). Dead-letter Inspector (`system.dead_letter`) — polished.
 *
 * Reads `GET /api/v1/registry/dead_letter?namespace=&since=&include_resolved=&limit=`
 * → `DLQEntryOut[]` (the authoritative `descriptor_dead_letter` projection in
 * `legba.data.registry.api`). Namespaces:
 *   descriptor       — validation failures during register/update/promote
 *   output           — analyst-output write failures (DLQ envelope per L-107)
 *   stack            — stack-component validation failures (e.g. missing vault
 *                      secrets for an LLM/NLP stack ref)
 *   discovery_resync — discovery cursor / resync failures
 *
 * UI-5 polish over the basic inspector:
 *  - **Resubmit mutation wired in** (was curl-only): `POST
 *    /api/v1/registry/dead_letter/{id}/resubmit` with an optional shallow-merge
 *    patch over `attempted_payload` (operator edits inline, validated client-side).
 *  - **since-window** filter + a resolved/unresolved toggle that drives the
 *    backend `include_resolved` flag (the endpoint hides resolved by default).
 *  - Resolved rows (non-null `resolution`) are dimmed + badged; failed
 *    resubmits surface inline.
 *
 * Field names track `DLQEntryOut` exactly: `attempted_at` / `actor` /
 * `validation_error` / `resolution` / `attempted_payload`.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, apiPost } from '@/lib/api'
import type { PanelProps } from '@/types'

/** Mirrors `DLQEntryOut` in `src/legba/data/registry/api.py`. */
interface DLQRow {
  id: string
  attempted_at: string
  /** signing identity (DID/key hash) that attempted the write */
  actor: string
  namespace: string
  declared_schema_uri: string | null
  /** structured validator failure — e.g. {kind, summary} */
  validation_error: Record<string, unknown> | null
  /** non-null once an operator resolves/resubmits the entry */
  resolution: string | null
  attempted_payload: Record<string, unknown> | null
}

/** Pull a one-line reason out of the structured validation_error. */
function dlqReason(ve: Record<string, unknown> | null): string {
  if (!ve) return '(no validator detail)'
  const summary = ve.summary ?? ve.message ?? ve.detail ?? ve.error
  const kind = typeof ve.kind === 'string' ? ve.kind : null
  if (typeof summary === 'string') return kind ? `${kind}: ${summary}` : summary
  return kind ?? JSON.stringify(ve).slice(0, 200)
}

type Namespace = 'all' | 'descriptor' | 'output' | 'stack' | 'discovery_resync'
type SinceWindow = 'all' | '1h' | '24h' | '7d'

const SINCE_MS: Record<Exclude<SinceWindow, 'all'>, number> = {
  '1h': 3600_000,
  '24h': 86_400_000,
  '7d': 604_800_000,
}

export default function DeadLetterPanel({ registration }: PanelProps) {
  const qc = useQueryClient()
  const [namespace, setNamespace] = useState<Namespace>('all')
  const [since, setSince] = useState<SinceWindow>('all')
  const [includeResolved, setIncludeResolved] = useState(false)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [patchDraft, setPatchDraft] = useState<Record<string, string>>({})
  const [patchError, setPatchError] = useState<Record<string, string>>({})

  const { data, isLoading, error, refetch } = useQuery<DLQRow[]>({
    queryKey: ['dlq', namespace, since, includeResolved],
    queryFn: () => {
      const qs = new URLSearchParams({ limit: '100' })
      if (namespace !== 'all') qs.set('namespace', namespace)
      if (since !== 'all') {
        qs.set('since', new Date(Date.now() - SINCE_MS[since]).toISOString())
      }
      if (includeResolved) qs.set('include_resolved', 'true')
      return apiGet<DLQRow[]>(`/registry/dead_letter?${qs.toString()}`)
    },
    refetchInterval: 30_000,
  })

  const resubmit = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch?: Record<string, unknown> }) =>
      apiPost(`/registry/dead_letter/${id}/resubmit`, patch ? { patch } : {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['dlq'] })
    },
    onError: (err, vars) => {
      setPatchError((p) => ({ ...p, [vars.id]: (err as Error).message }))
    },
  })

  const rows = data ?? []
  const unresolved = rows.filter((r) => !r.resolution).length

  function doResubmit(r: DLQRow) {
    setPatchError((p) => ({ ...p, [r.id]: '' }))
    const raw = patchDraft[r.id]?.trim()
    let patch: Record<string, unknown> | undefined
    if (raw) {
      try {
        patch = JSON.parse(raw)
        if (typeof patch !== 'object' || patch === null || Array.isArray(patch)) {
          throw new Error('patch must be a JSON object')
        }
      } catch (e) {
        setPatchError((p) => ({ ...p, [r.id]: `invalid patch JSON: ${(e as Error).message}` }))
        return
      }
    }
    resubmit.mutate({ id: r.id, patch })
  }

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${unresolved} unresolved / ${rows.length} shown`}
      onRefresh={() => refetch()}
    >
      <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={namespace}
          onChange={(e) => setNamespace(e.target.value as Namespace)}
          data-testid="dlq-namespace-filter"
        >
          <option value="all">all namespaces</option>
          <option value="descriptor">descriptor</option>
          <option value="output">output</option>
          <option value="stack">stack</option>
          <option value="discovery_resync">discovery_resync</option>
        </select>
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={since}
          onChange={(e) => setSince(e.target.value as SinceWindow)}
          data-testid="dlq-since-filter"
        >
          <option value="all">since: all</option>
          <option value="1h">since: 1h</option>
          <option value="24h">since: 24h</option>
          <option value="7d">since: 7d</option>
        </select>
        <label className="inline-flex items-center gap-1 text-slate-400">
          <input
            type="checkbox"
            checked={includeResolved}
            onChange={(e) => setIncludeResolved(e.target.checked)}
            data-testid="dlq-include-resolved"
          />
          include resolved
        </label>
        {isLoading && <span className="text-slate-500">loading…</span>}
      </div>

      {error instanceof Error && (
        <div className="text-rose-400 text-sm">error: {error.message}</div>
      )}

      <div className="flex-1 overflow-auto text-xs space-y-1" data-testid="dlq-list">
        {rows.length === 0 && !isLoading && (
          <div className="text-slate-500 text-center py-4">
            no dead-letter entries — substrate is clean
          </div>
        )}
        {rows.map((r) => {
          const expanded = expandedId === r.id
          const resolved = !!r.resolution
          const busy = resubmit.isPending && resubmit.variables?.id === r.id
          return (
            <div
              key={r.id}
              className={`bg-surface-100 border rounded p-2 ${
                resolved ? 'border-slate-800 opacity-60' : 'border-rose-900/40'
              }`}
              data-testid={`dlq-row-${r.id}`}
            >
              <button
                onClick={() => setExpandedId(expanded ? null : r.id)}
                className="w-full text-left"
              >
                <div className="flex items-baseline gap-3">
                  <span className="text-rose-300 shrink-0">{r.namespace}</span>
                  <span className="text-slate-200 truncate flex-1">
                    {r.declared_schema_uri ?? '(no schema uri)'}
                  </span>
                  {resolved && (
                    <span className="shrink-0 rounded px-1 bg-emerald-900 text-emerald-200 text-[10px]">
                      resolved
                    </span>
                  )}
                  <span className="text-slate-600 shrink-0">
                    {new Date(r.attempted_at).toLocaleString()}
                  </span>
                </div>
                <div className="text-slate-500 mt-1 truncate">reason: {dlqReason(r.validation_error)}</div>
              </button>
              {expanded && (
                <div className="mt-2 space-y-2 border-t border-slate-800 pt-2">
                  <div className="text-slate-600 text-[10px] font-mono break-all">
                    actor: {r.actor}
                  </div>
                  {r.validation_error !== null && (
                    <div>
                      <div className="text-slate-500 text-[10px] uppercase tracking-wide mb-1">
                        validation error
                      </div>
                      <pre className="bg-surface-200 p-2 rounded overflow-x-auto text-[10px] text-rose-200">
                        {JSON.stringify(r.validation_error, null, 2)}
                      </pre>
                    </div>
                  )}
                  {r.attempted_payload !== null && (
                    <div>
                      <div className="text-slate-500 text-[10px] uppercase tracking-wide mb-1">
                        attempted payload
                      </div>
                      <pre className="bg-surface-200 p-2 rounded overflow-x-auto text-[10px] text-slate-300 max-h-64">
                        {JSON.stringify(r.attempted_payload, null, 2)}
                      </pre>
                    </div>
                  )}
                  {!resolved && (
                    <div className="space-y-1">
                      <div className="text-slate-500 text-[10px] uppercase tracking-wide">
                        resubmit (optional shallow-merge patch — JSON object)
                      </div>
                      <textarea
                        className="w-full bg-surface-200 border border-slate-700 rounded p-1 px-2 font-mono text-[10px] h-16"
                        placeholder='{"field": "corrected value"}'
                        value={patchDraft[r.id] ?? ''}
                        onChange={(e) =>
                          setPatchDraft((p) => ({ ...p, [r.id]: e.target.value }))
                        }
                        data-testid={`dlq-patch-${r.id}`}
                      />
                      {patchError[r.id] && (
                        <div className="text-rose-400 text-[10px]" data-testid={`dlq-error-${r.id}`}>
                          {patchError[r.id]}
                        </div>
                      )}
                      <button
                        disabled={busy}
                        onClick={() => doResubmit(r)}
                        className="bg-amber-900 hover:bg-amber-800 disabled:opacity-50 text-amber-100 rounded px-2 py-1 text-xs"
                        data-testid={`dlq-resubmit-${r.id}`}
                      >
                        {busy ? 'resubmitting…' : 'resubmit'}
                      </button>
                    </div>
                  )}
                  {resolved && (
                    <div className="text-emerald-400 text-[10px]">
                      resolved: {r.resolution}
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </PanelChrome>
  )
}
