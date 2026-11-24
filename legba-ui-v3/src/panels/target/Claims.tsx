/**
 * T10. The subject's findings (`target.claims`; the subject page's
 * "Findings" tab, id `claims`) — UI-3 (Tier B) rebuilt.
 *
 * What it lists: FINDINGS — `analyst_outputs` rows of kind `finding`, read
 * from `GET /api/v1/findings?target_id=…` (`FindingRow`). It does not show
 * the `facts` table (which is live and separate; no console route reads it
 * here). The internal names (`Claim`, `toClaims`, `claimsModel`) are kept
 * so saved layouts and recorded cases still resolve. The
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
import { requestCrossFraming } from '@/lib/crossFramingLink'
import { humanizeId } from '@/lib/deskNames'
import {
  CLAIM_SEVERITY_SCALE,
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

/**
 * Bare tags the engine stamps for its own bookkeeping (a run's kind, the
 * component that wrote the row, a pipeline step). They are not topics a reader
 * filters by. U-1, target brief §5.
 */
const MACHINE_BARE_TAGS = new Set([
  'meta',
  'deterministic',
  'assembly',
  'assessment',
  'lead',
  'spine',
  'unstructured',
  'rollup',
  'proposal',
  'thematic_proposal',
  'escalation',
  'regime',
  'country',
  'region',
  'horizon',
  'driver',
  'mechanism',
  'decay',
  'correctness_measurement',
  'correctness_grader',
  'reference_builder',
  'indicator_tracker',
  'indicator_triggered',
  'graph_mining',
  'mining_abandoned',
  'narrative_mapper',
  'fact_decay_scan',
  'desk_baseline',
  'analyst_traces_retention',
  'traces_purged',
])

/**
 * The reader-facing topic tags on a claim (U-1). A `prefix:value` tag is kept
 * only when its prefix is `topic:` — `severity:` / `severity_delta:` /
 * `spine:` / `target:` / `analyst:` and the like are engine facets (severity
 * has its own filter). A bare tag is kept unless it is engine bookkeeping.
 */
function topicTags(data: Record<string, unknown> | null | undefined): string[] {
  const raw = data && Array.isArray(data.tags) ? (data.tags as unknown[]) : []
  return raw
    .filter((t): t is string => typeof t === 'string' && t.trim() !== '')
    .filter((t) => {
      const i = t.indexOf(':')
      if (i >= 0) return t.slice(0, i) === 'topic' && t.length > i + 1
      return !MACHINE_BARE_TAGS.has(t)
    })
}

/** A topic tag in reader words: `topic:economic_coercion` → "Economic coercion". */
function topicLabel(tag: string): string {
  const v = tag.startsWith('topic:') ? tag.slice('topic:'.length) : tag
  const words = v.replace(/[_-]+/g, ' ').trim()
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : tag
}

function severityDot(sev: string): string {
  switch (sev) {
    case 'critical':
      return 'bg-accent-critical'
    case 'high':
    case 'elevated':
      return 'bg-accent-warning'
    case 'moderate':
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
      qs.set('fields', 'full') // reads data (tags, corroboration) and derived_from
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
      claims.filter(
        (c) =>
          (!severity || c.severity === severity) &&
          (!tagFilter || tagsById.get(c.id)?.includes(tagFilter)),
      ),
    [claims, severity, tagFilter, tagsById],
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
        {/* U-1 — the platform's one scale (medium reads as moderate), plus any
            off-scale value actually loaded. Never two names for one rung. */}
        {Array.from(new Set([...CLAIM_SEVERITY_SCALE, ...severities]))
          .filter((s) => s !== 'unknown')
          .map((s) => (
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
      } claim${claims.length === 1 ? '' : 's'} · ${humanizeId(target_id)}`}
      actions={actions}
      onRefresh={() => refetch()}
    >
      {/* Topic-tag facet chips — reader topics only, folded behind a
          disclosure so the tab opens on the claims, not a facet dump (U-1). */}
      {allTags.length > 0 && (
        <details className="mb-2" data-testid="target-claims-topics" open={tagFilter !== null}>
        <summary className="cursor-pointer text-label text-ink-3 select-none">
          Filter by topic{tagFilter ? ` · ${topicLabel(tagFilter)}` : ''}
        </summary>
        <div className="flex flex-wrap gap-1 mt-1" data-testid="target-claims-tag-chips">
          {tagFilter && (
            <button
              onClick={() => setTagFilter(null)}
              className="px-1.5 py-0.5 rounded text-label border border-slate-600 text-slate-400 hover:text-slate-200"
            >
              clear ✕
            </button>
          )}
          {allTags.map(([tag, count]) => (
            <button
              key={tag}
              onClick={() => setTagFilter(tagFilter === tag ? null : tag)}
              className={cn(
                'px-1.5 py-0.5 rounded text-label border',
                tagFilter === tag
                  ? 'bg-accent-info/30 text-accent-info border-accent-info/50'
                  : 'bg-surface-200 text-slate-300 border-slate-700 hover:border-slate-500',
              )}
              data-testid={`target-claims-tag-${tag}`}
            >
              {topicLabel(tag)} <span className="text-slate-500">{count}</span>
            </button>
          ))}
        </div>
        </details>
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
              targetId={target_id}
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
  targetId,
  tags,
  expanded,
  onToggle,
}: {
  claim: Claim
  /** The desk this claim was published on — the cross-framing panel's scope. */
  targetId: string | undefined
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
              'px-1 py-0.5 rounded text-label uppercase tracking-wide shrink-0',
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
            className="px-1 py-0.5 rounded bg-accent-warning/20 text-accent-warning text-label uppercase tracking-wide shrink-0"
            title="fewer than 2 independent sources"
            data-testid={`target-claim-uncorrob-${c.id}`}
          >
            uncorrob
          </span>
        )}
        <span className="font-mono text-label text-slate-300 shrink-0" title="confidence">
          {(c.confidence * 100).toFixed(0)}%
        </span>
        <span
          className="font-mono text-label text-slate-500 shrink-0"
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

          {/* Wave P lane B — CROSS-FRAMING. The contested badge above answers
              "does anything outside the tower disagree with this claim"; this
              answers the question beside it, which no surface answered:
              "what do the desk's OTHER units say about the same evidence, and
              which of them are silent". Parks the claim and fires the open
              event (`lib/crossFramingLink`), so the panel picks the subject up
              whether it is already mounted or opened afterwards from ⌘K. */}
          {targetId && (
            <button
              type="button"
              onClick={() =>
                requestCrossFraming({
                  targetId,
                  claimText: c.statement,
                  origin: 'target.claims',
                })
              }
              className="text-label underline text-accent-info"
              data-testid={`target-claim-cross-framing-${c.id}`}
              title="put this claim against the desk's other bounded units, and see which of them are silent"
            >
              cross-framing ▸
            </button>
          )}

          {/* P1-T6 why-NOT (verify path): the faithfulness verify pass flagged
              span(s) of this claim's prose as unsupported — surfaced inline,
              never silently dropped. The OTHER why-not path — a live dispute —
              is the ContestedBadge above. Absent verify block → nothing here. */}
          {c.whyNot && (
            <div
              className="rounded border border-accent-warning/40 bg-accent-warning/10 p-2 space-y-1"
              data-testid={`target-claim-whynot-${c.id}`}
            >
              <div className="flex flex-wrap items-center gap-1 text-label font-semibold uppercase tracking-wide text-accent-warning">
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
                  <li key={`${i}-${s.reason}`} className="text-label text-slate-300">
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
                  className="px-1.5 py-0.5 rounded bg-surface-200 text-label text-slate-300"
                >
                  {topicLabel(t)}
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
            <div className="text-label text-slate-500">corroboration not yet scored</div>
          )}

          <div className="text-label text-slate-500" title={c.analyst_id ?? undefined}>
            {humanizeAnalystId(c.analyst_id)} · {new Date(c.produced_at).toLocaleString()}
            {c.corroborationSources !== null && ` · ${c.corroborationSources} independent source(s)`}
          </div>

          {/* P1-T6 freshness/decay detail — rendered only when the row actually
              carries a decay/temporal signal (degrade to nothing otherwise). */}
          {(c.decay.decay !== null || c.decay.validUntil || c.decay.expired) && (
            <div
              className="text-label text-slate-500"
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
            <span className="text-label uppercase tracking-wide text-slate-400">
              evidence chain ({c.derived_from.length})
            </span>
            {c.derived_from.length > 0 && (
              <span className="ml-2 inline-flex flex-wrap gap-1 align-top">
                {c.derived_from.map((id) => (
                  <button
                    key={id}
                    title={`open lineage for ${id}`}
                    onClick={() => openLineage('signal', id)}
                    className="font-mono text-label underline text-accent-info"
                  >
                    {id.slice(0, 8)}…
                  </button>
                ))}
              </span>
            )}
          </div>

          <button
            onClick={() => openLineage('finding', c.id)}
            className="text-label underline text-accent-info"
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
      <span className="text-label text-slate-400 w-24 shrink-0">{label}</span>
      <div className="flex-1 h-1.5 bg-surface-200 rounded overflow-hidden">
        <div className="h-full rounded" style={{ width: `${pct}%`, backgroundColor: color }} />
      </div>
      <span className="font-mono text-label text-slate-300 w-9 text-right">{pct.toFixed(0)}%</span>
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
