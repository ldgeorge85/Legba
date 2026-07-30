/**
 * T10. Target Claims (`target.claims`, ex-Facts) — UI-3 (Tier B) rebuilt.
 *
 * Post-L-090 the facts table collapsed into the analyst-output substrate.
 * There is no `/claims` / `/facts` endpoint (frozen surface); the honest
 * source for claim-like rows carrying confidence AND corroboration is the
 * findings read (`GET /api/v1/findings?target_id=…` → `FindingRow`). The
 * deterministic `corroboration_scoring` handler writes
 * `corroboration_score` + `corroboration_sources` into the row `data`.
 *
 * v2 parity (Facts→Claims scored inspire-new):
 *   - claim statements as a faceted, confidence-sorted list;
 *   - facet by severity (the available facet on findings);
 *   - per-claim confidence + corroboration (score + independent-source
 *     count), with a corroboration bar;
 *   - evidence chain: `derived_from` ids → click→lineage.
 *
 * Normalization lives in `@/lib/claimsModel` (pure + unit-tested).
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import ContestedBadge from '@/v4/components/ContestedBadge'
import { apiGet, ApiError } from '@/lib/api'
import type { PanelProps } from '@/types'
import { cn } from '@/lib/cn'
import { selectRow } from '@/state/selection'
import { humanizeAnalystId } from '@/lib/analystNames'
import {
  claimSeverities,
  toClaims,
  type Claim,
  type DecayInfo,
  type FindingRow,
} from '@/lib/claimsModel'

interface Page<T> {
  data: T[]
  next_cursor: string | null
}

type SortMode = 'confidence' | 'corroboration' | 'recency'

function openLineage(kind: string, id: string) {
  // Redesign Move 2: drive the unified selection store (opens the Inspector +
  // brushes every room) instead of firing a legacy window event into the void.
  selectRow(kind, id)
}

/** Drop the `target:` / `analyst:` bookkeeping tags the analyst stamps. */
function topicTags(data: Record<string, unknown> | null | undefined): string[] {
  const raw = data && Array.isArray(data.tags) ? (data.tags as unknown[]) : []
  return raw
    .filter((t): t is string => typeof t === 'string')
    .filter((t) => !t.startsWith('target:') && !t.startsWith('analyst:'))
}

function severityDot(sev: string): string {
  switch (sev) {
    case 'critical':
      return 'bg-accent-critical'
    case 'high':
      return 'bg-accent-warning'
    case 'medium':
      return 'bg-accent-info'
    case 'low':
      return 'bg-accent-ok'
    default:
      return 'bg-slate-500'
  }
}

/** Tailwind classes for the freshness/decay chip, by bucket. */
function decayChipClass(label: DecayInfo['label']): string {
  switch (label) {
    case 'expired':
      return 'bg-accent-critical/20 text-accent-critical'
    case 'stale':
      return 'bg-accent-warning/20 text-accent-warning'
    default: // 'decaying'
      return 'bg-slate-600/40 text-slate-400'
  }
}

/** A human tooltip explaining WHY a claim reads as decaying/stale/expired. */
function freshnessTitle(decay: DecayInfo): string {
  const parts: string[] = []
  if (decay.label === 'expired') parts.push('expired — past its valid-until')
  else if (decay.label === 'stale') parts.push('stale — past its valid-until')
  else if (decay.label === 'decaying') parts.push('confidence decaying with age')
  if (decay.decay !== null && decay.decay < 0)
    parts.push(`confidence decayed ${decay.decay.toFixed(2)}`)
  if (decay.validUntil) parts.push(`valid until ${new Date(decay.validUntil).toLocaleString()}`)
  if (decay.lastDecayAt) parts.push(`last decayed ${new Date(decay.lastDecayAt).toLocaleString()}`)
  return parts.join(' · ')
}

export default function TargetClaimsPanel({ registration, scope }: PanelProps) {
  const target_id = scope.target_id ?? registration.descriptor_id
  const [open, setOpen] = useState<string | null>(null)
  const [severity, setSeverity] = useState<string>('')
  const [sort, setSort] = useState<SortMode>('confidence')
  const [tagFilter, setTagFilter] = useState<string | null>(null)

  const { data, error, isLoading, refetch, isFetching } = useQuery<Page<FindingRow>>({
    enabled: !!target_id,
    queryKey: ['target-claims', target_id, severity],
    queryFn: async () => {
      const qs = new URLSearchParams({ target_id, limit: '200' })
      if (severity) qs.set('severity', severity)
      try {
        return await apiGet<Page<FindingRow>>(`/findings?${qs.toString()}`)
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { data: [], next_cursor: null }
        throw e
      }
    },
    refetchInterval: 60_000,
  })

  const rows = data?.data ?? []
  const claims = useMemo(() => toClaims(rows), [rows])
  const severities = useMemo(() => claimSeverities(claims), [claims])

  // Topic-tag index (claim id → tags) + the tag universe, faceted from the
  // raw findings rows (claimsModel doesn't carry tags). Bookkeeping tags
  // (`target:` / `analyst:`) are dropped.
  const tagsById = useMemo(() => {
    const m = new Map<string, string[]>()
    for (const r of rows) m.set(r.id, topicTags(r.data))
    return m
  }, [rows])
  const allTags = useMemo(() => {
    const counts = new Map<string, number>()
    for (const tags of tagsById.values()) {
      for (const t of tags) counts.set(t, (counts.get(t) ?? 0) + 1)
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  }, [tagsById])

  const filtered = useMemo(
    () =>
      tagFilter ? claims.filter((c) => tagsById.get(c.id)?.includes(tagFilter)) : claims,
    [claims, tagFilter, tagsById],
  )
  const sorted = useMemo(() => sortClaims(filtered, sort), [filtered, sort])

  const actions = (
    <div className="flex items-center gap-1">
      <select
        className="bg-surface-200 border border-slate-700 rounded px-2 py-0.5 text-xs"
        value={severity}
        onChange={(e) => setSeverity(e.target.value)}
        data-testid="target-claims-severity"
      >
        <option value="">all severities</option>
        {/* Offer the union of what's loaded plus the static taxonomy. */}
        {Array.from(new Set([...severities, 'critical', 'high', 'medium', 'low'])).map((s) => (
          <option key={s} value={s}>
            {s}
          </option>
        ))}
      </select>
      <select
        className="bg-surface-200 border border-slate-700 rounded px-2 py-0.5 text-xs"
        value={sort}
        onChange={(e) => setSort(e.target.value as SortMode)}
        data-testid="target-claims-sort"
      >
        <option value="confidence">confidence</option>
        <option value="corroboration">corroboration</option>
        <option value="recency">recency</option>
      </select>
    </div>
  )

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${sorted.length}${
        sorted.length !== claims.length ? `/${claims.length}` : ''
      } claim${claims.length === 1 ? '' : 's'} · target ${target_id}`}
      actions={actions}
      onRefresh={() => refetch()}
    >
      {/* Topic-tag facet chips. */}
      {allTags.length > 0 && (
        <div className="flex flex-wrap gap-1 mb-2" data-testid="target-claims-tag-chips">
          {tagFilter && (
            <button
              onClick={() => setTagFilter(null)}
              className="px-1.5 py-0.5 rounded text-[10px] border border-slate-600 text-slate-400 hover:text-slate-200"
            >
              clear ✕
            </button>
          )}
          {allTags.map(([tag, count]) => (
            <button
              key={tag}
              onClick={() => setTagFilter(tagFilter === tag ? null : tag)}
              className={cn(
                'px-1.5 py-0.5 rounded text-[10px] border',
                tagFilter === tag
                  ? 'bg-accent-info/30 text-accent-info border-accent-info/50'
                  : 'bg-surface-200 text-slate-300 border-slate-700 hover:border-slate-500',
              )}
              data-testid={`target-claims-tag-${tag}`}
            >
              {tag} <span className="text-slate-500">{count}</span>
            </button>
          ))}
        </div>
      )}

      {isLoading && <div className="text-xs text-slate-400">Loading claims…</div>}
      {error && (
        <div className="text-xs text-accent-critical">
          Failed to load: {(error as Error).message}
        </div>
      )}
      {!isLoading && !error && sorted.length === 0 && (
        <div className="text-xs text-slate-400" data-testid="target-claims-empty">
          {isFetching
            ? 'Loading…'
            : tagFilter || severity
              ? 'No claims match the current filter.'
              : 'No claims for this target yet.'}
        </div>
      )}
      {sorted.length > 0 && (
        <ul className="space-y-1">
          {sorted.map((c) => (
            <ClaimItem
              key={c.id}
              claim={c}
              tags={tagsById.get(c.id) ?? []}
              expanded={open === c.id}
              onToggle={() => setOpen(open === c.id ? null : c.id)}
            />
          ))}
        </ul>
      )}
    </PanelChrome>
  )
}

function ClaimItem({
  claim: c,
  tags,
  expanded,
  onToggle,
}: {
  claim: Claim
  tags: string[]
  expanded: boolean
  onToggle: () => void
}) {
  // Uncorroborated: not yet scored, or scored with <2 independent sources.
  const uncorroborated = c.corroborationSources === null || c.corroborationSources < 2
  return (
    <li data-testid="target-claim-item">
      <button
        onClick={onToggle}
        className={cn(
          'w-full text-left px-2 py-1.5 rounded text-xs flex items-center gap-2',
          'hover:bg-surface-50/40',
          expanded && 'bg-surface-50/60',
        )}
      >
        <span
          className={cn('inline-block w-2 h-2 rounded-full shrink-0', severityDot(c.severity))}
          title={c.severity}
        />
        <span
          className="flex-1 truncate"
          style={c.decay.opacity < 1 ? { opacity: c.decay.opacity } : undefined}
        >
          {c.statement}
        </span>
        {/* P1-T6 freshness/decay indicator — a stale/expired claim fades + flags
            here so an aged claim is never silently surfaced as current. */}
        {c.decay.label !== 'fresh' && (
          <span
            className={cn(
              'px-1 py-0.5 rounded text-[9px] uppercase tracking-wide shrink-0',
              decayChipClass(c.decay.label),
            )}
            title={freshnessTitle(c.decay)}
            data-testid={`target-claim-decay-${c.id}`}
          >
            {c.decay.label}
          </span>
        )}
        {uncorroborated && (
          <span
            className="px-1 py-0.5 rounded bg-accent-warning/20 text-accent-warning text-[9px] uppercase tracking-wide shrink-0"
            title="fewer than 2 independent sources"
            data-testid={`target-claim-uncorrob-${c.id}`}
          >
            uncorrob
          </span>
        )}
        <span className="font-mono text-[11px] text-slate-300 shrink-0" title="confidence">
          {(c.confidence * 100).toFixed(0)}%
        </span>
        <span
          className="font-mono text-[10px] text-slate-500 shrink-0"
          title="corroborating sources"
          data-testid={`target-claim-corrob-${c.id}`}
        >
          {c.corroborationSources === null ? '—' : `×${c.corroborationSources}`}
        </span>
      </button>
      {expanded && (
        <div className="ml-4 mt-1 p-2 bg-surface-50/40 rounded text-xs space-y-2">
          {c.body && <div className="text-slate-300">{c.body}</div>}

          {/* #101 contested-claims surface. Findings carry no real `facts.id`,
              so we look the dispute up by the claim's subject (statement),
              lower-cased server-side via `?subject=`. Renders nothing when the
              subject has no live dispute (the common case → zero noise). */}
          <ContestedBadge subject={c.statement} />

          {/* P1-T6 why-NOT (verify path): the faithfulness verify pass flagged
              span(s) of this claim's prose as unsupported — surfaced inline,
              never silently dropped. The OTHER why-not path — a live dispute —
              is the ContestedBadge above. Absent verify block → nothing here. */}
          {c.whyNot && (
            <div
              className="rounded border border-accent-warning/40 bg-accent-warning/10 p-2 space-y-1"
              data-testid={`target-claim-whynot-${c.id}`}
            >
              <div className="flex flex-wrap items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-accent-warning">
                why not — unsupported by verify
                {c.whyNot.faithfulnessScore !== null && (
                  <span className="font-mono normal-case text-slate-400">
                    faithfulness {(c.whyNot.faithfulnessScore * 100).toFixed(0)}%
                  </span>
                )}
                {c.whyNot.judgeStatus && (
                  <span className="font-mono normal-case text-slate-500">
                    · {c.whyNot.judgeStatus}
                  </span>
                )}
              </div>
              <ul className="space-y-1">
                {c.whyNot.unsupportedSpans.map((s, i) => (
                  <li key={`${i}-${s.reason}`} className="text-[11px] text-slate-300">
                    <span className="text-slate-200">“{s.text}”</span>{' '}
                    <span className="text-accent-warning">— {s.reasonLabel}</span>
                    {s.markers.length > 0 && (
                      <span className="ml-1 font-mono text-slate-500">
                        cited {s.markers.map((m) => `[${m}]`).join(' ')}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {tags.length > 0 && (
            <div className="flex flex-wrap gap-1" data-testid={`target-claim-tags-${c.id}`}>
              {tags.map((t) => (
                <span
                  key={t}
                  className="px-1.5 py-0.5 rounded bg-surface-200 text-[10px] text-slate-300"
                >
                  {t}
                </span>
              ))}
            </div>
          )}

          <ConfidenceBar label="confidence" value={c.confidence} color="#38bdf8" />
          {c.corroborationScore !== null ? (
            <ConfidenceBar
              label="corroboration"
              value={c.corroborationScore}
              color="#34d399"
              testid={`target-claim-corrob-bar-${c.id}`}
            />
          ) : (
            <div className="text-[11px] text-slate-500">corroboration not yet scored</div>
          )}

          <div className="text-[10px] text-slate-500" title={c.analyst_id ?? undefined}>
            {humanizeAnalystId(c.analyst_id)} · {new Date(c.produced_at).toLocaleString()}
            {c.corroborationSources !== null && ` · ${c.corroborationSources} independent source(s)`}
          </div>

          {/* P1-T6 freshness/decay detail — rendered only when the row actually
              carries a decay/temporal signal (degrade to nothing otherwise). */}
          {(c.decay.decay !== null || c.decay.validUntil || c.decay.expired) && (
            <div
              className="text-[10px] text-slate-500"
              data-testid={`target-claim-freshness-${c.id}`}
            >
              freshness: <span className="text-slate-400">{c.decay.label}</span>
              {c.decay.decay !== null &&
                c.decay.decay < 0 &&
                ` · confidence decayed ${c.decay.decay.toFixed(2)}`}
              {c.decay.validUntil &&
                ` · valid until ${new Date(c.decay.validUntil).toLocaleDateString()}`}
              {c.decay.lastDecayAt &&
                ` · last decayed ${new Date(c.decay.lastDecayAt).toLocaleDateString()}`}
            </div>
          )}

          <div>
            <span className="text-[10px] uppercase tracking-wide text-slate-400">
              evidence chain ({c.derived_from.length})
            </span>
            {c.derived_from.length > 0 && (
              <span className="ml-2 inline-flex flex-wrap gap-1 align-top">
                {c.derived_from.map((id) => (
                  <button
                    key={id}
                    title={`open lineage for ${id}`}
                    onClick={() => openLineage('signal', id)}
                    className="font-mono text-[10px] underline text-accent-info"
                  >
                    {id.slice(0, 8)}…
                  </button>
                ))}
              </span>
            )}
          </div>

          <button
            onClick={() => openLineage('finding', c.id)}
            className="text-[10px] underline text-accent-info"
          >
            trace this claim →
          </button>
        </div>
      )}
    </li>
  )
}

function ConfidenceBar({
  label,
  value,
  color,
  testid,
}: {
  label: string
  value: number
  color: string
  testid?: string
}) {
  const pct = Math.max(0, Math.min(1, value)) * 100
  return (
    <div className="flex items-center gap-2" data-testid={testid}>
      <span className="text-[10px] text-slate-400 w-24 shrink-0">{label}</span>
      <div className="flex-1 h-1.5 bg-surface-200 rounded overflow-hidden">
        <div className="h-full rounded" style={{ width: `${pct}%`, backgroundColor: color }} />
      </div>
      <span className="font-mono text-[10px] text-slate-300 w-9 text-right">{pct.toFixed(0)}%</span>
    </div>
  )
}

function sortClaims(claims: Claim[], mode: SortMode): Claim[] {
  const xs = [...claims]
  switch (mode) {
    case 'confidence':
      return xs.sort((a, b) => b.confidence - a.confidence)
    case 'corroboration':
      return xs.sort(
        (a, b) => (b.corroborationSources ?? -1) - (a.corroborationSources ?? -1),
      )
    case 'recency':
      return xs.sort((a, b) => b.produced_at.localeCompare(a.produced_at))
  }
}
