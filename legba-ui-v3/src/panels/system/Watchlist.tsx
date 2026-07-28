/**
 * P5-6 — Watchlist v2 (`system.watchlist`): SERVER-side standing watches.
 *
 * The operator names a watch — an entity ("Wagner Group"), a free-text topic
 * ("Strait of Hormuz"), or a place (ISO2 countries, or a point+radius) — and
 * the `watchlist_hit` trigger class inside `alert_trigger_scan` pages on any
 * VERIFIED finding touching it, regardless of desk/severity (unless the watch
 * sets a min-severity floor). Alerts flow the shared P1-1 dispatcher → ntfy.
 *
 * DISTINCT from the Alert Center's localStorage subscriptions (client-only,
 * fire while the panel is open): these rows live in the `watchlist` table so
 * the server-side scan evaluates them on its own cadence whether or not any
 * UI is open. This panel is MANAGEMENT (list + add + soft-delete + per-watch
 * 7-day hit count); the alerts themselves are the product.
 *
 * Deletes are SOFT (`active=false` via DELETE /v3/watchlist/{id}) — the
 * watch's no-refire watermark history survives, so re-activating never
 * re-pages already-seen hits.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiDelete, apiGet, apiPost, apiPut } from '@/lib/api'
import type { PanelProps } from '@/types'

export interface WatchRow {
  id: string
  kind: 'entity' | 'text' | 'geo'
  pattern: Record<string, unknown>
  label: string
  min_severity: string | null
  created_by: string
  active: boolean
  created_at: string
  updated_at: string
  hits_7d: number
}

type WatchKind = WatchRow['kind']
const SEVERITY_FLOORS = ['any', 'info', 'low', 'medium', 'high', 'critical'] as const

/** Human one-liner for a stored pattern (mirrors the server vocabulary). */
export function patternSummary(kind: WatchKind, pattern: Record<string, unknown>): string {
  if (kind === 'entity') {
    const name = typeof pattern.name === 'string' ? pattern.name : null
    const id = typeof pattern.entity_id === 'string' ? pattern.entity_id : null
    return name ?? (id ? `id ${id.slice(0, 8)}…` : '(empty)')
  }
  if (kind === 'text') {
    return typeof pattern.query === 'string' ? `“${pattern.query}”` : '(empty)'
  }
  const countries = Array.isArray(pattern.countries) ? pattern.countries : null
  if (countries) return countries.join(', ')
  if (typeof pattern.lat === 'number' && typeof pattern.lon === 'number') {
    return `${pattern.lat}, ${pattern.lon} ±${pattern.radius_km ?? '?'} km`
  }
  return '(empty)'
}

/** Build the create-pattern from the form fields; null = invalid/incomplete. */
export function buildPattern(
  kind: WatchKind,
  value: string,
): Record<string, unknown> | null {
  const v = value.trim()
  if (!v) return null
  if (kind === 'entity') return { name: v }
  if (kind === 'text') return v.length >= 2 ? { query: v } : null
  // geo: "IR, IQ" (ISO2 list) or "lat, lon, radius_km".
  const parts = v.split(',').map((p) => p.trim()).filter(Boolean)
  if (parts.length > 0 && parts.every((p) => /^[A-Za-z]{2}$/.test(p))) {
    return { countries: parts.map((p) => p.toUpperCase()) }
  }
  if (parts.length === 3 && parts.every((p) => /^-?\d+(\.\d+)?$/.test(p))) {
    return {
      lat: Number(parts[0]),
      lon: Number(parts[1]),
      radius_km: Number(parts[2]),
    }
  }
  return null
}

const KIND_PLACEHOLDER: Record<WatchKind, string> = {
  entity: 'entity name or alias (e.g. Wagner Group)',
  text: 'topic terms (e.g. Strait of Hormuz)',
  geo: 'ISO2 list "IR, IQ" — or point "36.3, 43.1, 50" (lat, lon, km)',
}

export default function WatchlistPanel({ registration }: PanelProps) {
  const qc = useQueryClient()
  const [kind, setKind] = useState<WatchKind>('entity')
  const [value, setValue] = useState('')
  const [label, setLabel] = useState('')
  const [floor, setFloor] = useState<(typeof SEVERITY_FLOORS)[number]>('any')
  const [error, setError] = useState<string | null>(null)
  const [showInactive, setShowInactive] = useState(false)

  const { data: watches, refetch } = useQuery<WatchRow[]>({
    queryKey: ['watchlist', showInactive],
    queryFn: () =>
      apiGet<WatchRow[]>(`/v3/watchlist${showInactive ? '?include_inactive=true' : ''}`),
    refetchInterval: 60_000,
  })

  const invalidate = () => qc.invalidateQueries({ queryKey: ['watchlist'] })

  const create = useMutation({
    mutationFn: (body: Record<string, unknown>) => apiPost<WatchRow>('/v3/watchlist', body),
    onSuccess: () => {
      setValue('')
      setLabel('')
      setError(null)
      invalidate()
    },
    onError: (e: unknown) => setError(String((e as Error)?.message ?? e)),
  })
  const softDelete = useMutation({
    mutationFn: (id: string) => apiDelete<WatchRow>(`/v3/watchlist/${id}`),
    onSuccess: invalidate,
  })
  const reactivate = useMutation({
    mutationFn: (id: string) => apiPut<WatchRow>(`/v3/watchlist/${id}`, { active: true }),
    onSuccess: invalidate,
  })

  function addWatch() {
    const pattern = buildPattern(kind, value)
    if (!pattern) {
      setError(
        kind === 'geo'
          ? 'geo needs an ISO2 list ("IR, IQ") or "lat, lon, radius_km"'
          : 'enter the thing to watch (2+ characters)',
      )
      return
    }
    create.mutate({
      kind,
      pattern,
      label: label.trim() || value.trim(),
      min_severity: floor === 'any' ? null : floor,
    })
  }

  const rows = watches ?? []
  const activeCount = rows.filter((w) => w.active).length

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${activeCount} active watch${activeCount === 1 ? '' : 'es'} · server-side, alerts via the trigger scan`}
      onRefresh={() => refetch()}
      actions={
        <button
          onClick={() => setShowInactive((v) => !v)}
          className={`text-[10px] px-2 py-0.5 rounded border ${
            showInactive ? 'border-accent-info text-accent-info' : 'border-slate-700 text-slate-500'
          }`}
          data-testid="watchlist-show-inactive"
        >
          {showInactive ? 'showing inactive' : 'active only'}
        </button>
      }
    >
      {/* add form */}
      <div className="flex items-center gap-2 mb-1 text-xs flex-wrap">
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={kind}
          onChange={(e) => {
            setKind(e.target.value as WatchKind)
            setError(null)
          }}
          data-testid="watchlist-kind"
        >
          <option value="entity">entity</option>
          <option value="text">topic</option>
          <option value="geo">place</option>
        </select>
        <input
          className="flex-1 min-w-[180px] bg-surface-200 border border-slate-700 rounded p-1 px-2 text-slate-200"
          placeholder={KIND_PLACEHOLDER[kind]}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && addWatch()}
          data-testid="watchlist-value"
        />
        <input
          className="w-28 bg-surface-200 border border-slate-700 rounded p-1 px-2 text-slate-200"
          placeholder="label (opt)"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          data-testid="watchlist-label"
        />
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={floor}
          onChange={(e) => setFloor(e.target.value as (typeof SEVERITY_FLOORS)[number])}
          data-testid="watchlist-floor"
        >
          {SEVERITY_FLOORS.map((s) => (
            <option key={s} value={s}>
              floor: {s}
            </option>
          ))}
        </select>
        <button
          onClick={addWatch}
          disabled={create.isPending}
          className="bg-surface-200 hover:bg-surface-300 border border-slate-700 rounded px-2 py-1 disabled:opacity-50"
          data-testid="watchlist-add"
        >
          + watch
        </button>
      </div>
      {error && (
        <div className="text-[11px] text-rose-400 mb-2" data-testid="watchlist-error">
          {error}
        </div>
      )}

      {/* watches */}
      <div className="flex-1 overflow-auto mt-2" data-testid="watchlist-rows">
        {rows.length === 0 && (
          <div className="text-slate-600 text-xs">
            no watches — name an entity, topic, or place above; any verified
            finding touching it will page, regardless of desk or severity
          </div>
        )}
        <div className="space-y-1">
          {rows.map((w) => (
            <div
              key={w.id}
              className={`flex items-center gap-2 text-xs bg-surface-100 border border-slate-800 rounded p-1.5 ${
                w.active ? '' : 'opacity-50'
              }`}
              data-testid={`watchlist-row-${w.id}`}
            >
              <span className="rounded px-1 bg-slate-700 text-slate-200 shrink-0">
                {w.kind === 'text' ? 'topic' : w.kind === 'geo' ? 'place' : 'entity'}
              </span>
              <span className="text-slate-200 font-medium truncate" title={w.label}>
                {w.label}
              </span>
              <span className="font-mono text-slate-500 truncate">
                {patternSummary(w.kind, w.pattern)}
              </span>
              {w.min_severity && (
                <span className="text-slate-500 shrink-0">≥ {w.min_severity}</span>
              )}
              <span
                className={`ml-auto shrink-0 rounded px-1 tabular-nums ${
                  w.hits_7d > 0 ? 'bg-amber-900 text-amber-200' : 'bg-slate-800 text-slate-500'
                }`}
                title="alert rows naming this watch in the last 7 days (rollups count once)"
                data-testid={`watchlist-hits-${w.id}`}
              >
                {w.hits_7d} / 7d
              </span>
              {w.active ? (
                <button
                  onClick={() => softDelete.mutate(w.id)}
                  className="text-slate-600 hover:text-rose-400 shrink-0"
                  title="stop watching (soft delete — history kept)"
                  data-testid={`watchlist-del-${w.id}`}
                >
                  ×
                </button>
              ) : (
                <button
                  onClick={() => reactivate.mutate(w.id)}
                  className="text-slate-500 hover:text-accent-ok shrink-0"
                  title="re-activate this watch"
                  data-testid={`watchlist-reactivate-${w.id}`}
                >
                  ↻
                </button>
              )}
            </div>
          ))}
        </div>
      </div>
    </PanelChrome>
  )
}
