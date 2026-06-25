/**
 * O4. Wiring Editor / Inspector (`registry.wirings`).
 *
 * legba_ui_panels_v2.md §3.4 O4 — "the active wiring": which sources feed
 * which targets, which analysts each target runs, and the analyst↔analyst /
 * target↔target coordination edges. Under the source-first pivot there is no
 * `wiring_descriptors` table — the wiring is *derived* from the descriptor
 * bodies the registry already serves:
 *
 *   - source → target   : target.sources[] is a list of SourceRef. Each ref
 *                          either names an explicit `source_id`, or carries a
 *                          `source_selector` (tags / kinds / geo / tenant).
 *                          A selector wires every live source whose `scope`
 *                          matches (the same predicate the runtime fan-out
 *                          binder uses). We resolve the selector against the
 *                          live source roster and list the matches.
 *   - target → analyst  : target.analyst.use names the inline analyst kind.
 *   - target ↔ target   : target.coordination.{subscribes_to, publishes}.
 *   - analyst ↔ analyst : analyst.subscription.other_analysts[].
 *   - analyst → channel : analyst.outputs[].config.channel (NATS streams).
 *
 * This is a *read* surface (no `wiring_descriptors` to mutate) — edits happen
 * on the underlying target / analyst descriptors (the registry.targets /
 * registry.analysts panels own the inline + guided editors). O4's job is to
 * make the otherwise-invisible fan-out legible and to flag coordination
 * cycles client-side before they bite the reconcile loop.
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { ScopePicker } from '@/components/ScopePicker'
import { apiGet } from '@/lib/api'
import type { PanelProps } from '@/types'

/* -------------------------------------------------------------------------- */
/* registry row shapes (subset we read)                                       */
/* -------------------------------------------------------------------------- */

interface DescriptorRow {
  descriptor_id: string
  version: string
  state: string
  name: string
  family: string
  body: Record<string, unknown>
}

interface SourceSelector {
  tags?: string[]
  kinds?: string[]
  geo?: string[]
  languages?: string[]
  owner_tenant?: string | null
  predicate?: string | null
}

interface SourceRef {
  source_id?: string | null
  source_selector?: SourceSelector | null
  subscription?: { predicate?: string | null; geo?: string[] } | null
}

interface TargetBody {
  sources?: SourceRef[]
  analyst?: { use?: string }
  coordination?: {
    subscribes_to?: string[]
    publishes?: string[]
    allow_cycles?: boolean
    cycle_hop_limit?: number
  }
}

interface AnalystBody {
  identity?: { kind?: string }
  subscription?: {
    other_analysts?: Array<string | { analyst_id?: string }>
    targets?: unknown
  }
  outputs?: Array<{ kind?: string; config?: { channel?: string; skill_id?: string } }>
}

interface SourceBody {
  identity?: { kind?: string }
  scope?: {
    tags?: string[]
    geo?: string[]
    languages?: string[]
    owner_tenant?: string | null
  }
}

/* -------------------------------------------------------------------------- */
/* selector resolution — mirrors the runtime fan-out binder semantics         */
/* -------------------------------------------------------------------------- */

/**
 * Does `selector` match a source's scope? Empty selector facets are
 * wildcards (match anything); a non-empty facet requires intersection with
 * the source's corresponding scope facet (ANY-of, matching the binder's
 * tag/kind/geo overlap test). `owner_tenant` is an equality gate when set
 * to anything other than the wildcard "shared".
 *
 * NOTE: the selector `predicate` (a CEL-ish string) is NOT evaluated
 * client-side — we surface it as an advisory the runtime applies. So a
 * "match" here is "matches the structural facets"; predicate-narrowing is
 * shown but not simulated.
 */
function selectorMatches(sel: SourceSelector, src: SourceBody): boolean {
  const scope = src.scope ?? {}
  const kind = src.identity?.kind
  const anyOverlap = (want?: string[], have?: string[]): boolean => {
    if (!want || want.length === 0) return true // wildcard
    if (!have || have.length === 0) return false
    const set = new Set(have)
    return want.some((w) => set.has(w))
  }
  if (!anyOverlap(sel.tags, scope.tags)) return false
  if (!anyOverlap(sel.geo, scope.geo)) return false
  if (!anyOverlap(sel.languages, scope.languages)) return false
  if (sel.kinds && sel.kinds.length > 0) {
    if (!kind || !sel.kinds.includes(kind)) return false
  }
  if (sel.owner_tenant && sel.owner_tenant !== 'shared') {
    if ((scope.owner_tenant ?? 'default') !== sel.owner_tenant) return false
  }
  return true
}

/** Human-readable summary of a selector's non-wildcard facets. */
function describeSelector(sel: SourceSelector): string {
  const parts: string[] = []
  if (sel.tags?.length) parts.push(`tags∈[${sel.tags.join(', ')}]`)
  if (sel.kinds?.length) parts.push(`kind∈[${sel.kinds.join(', ')}]`)
  if (sel.geo?.length) parts.push(`geo∈[${sel.geo.join(', ')}]`)
  if (sel.languages?.length) parts.push(`lang∈[${sel.languages.join(', ')}]`)
  if (sel.owner_tenant && sel.owner_tenant !== 'shared') parts.push(`tenant=${sel.owner_tenant}`)
  return parts.length ? parts.join(' · ') : 'any source (wildcard)'
}

/* -------------------------------------------------------------------------- */
/* coordination-graph cycle detection                                         */
/* -------------------------------------------------------------------------- */

/**
 * Find cycles in the target↔target coordination graph (an edge from A to B
 * exists when A.coordination.publishes ⟨channel⟩ and B.coordination
 * .subscribes_to ⟨channel⟩, or directly via subscribes_to naming a target).
 * Returns the list of cyclic node-id sequences (one per back-edge found).
 *
 * We treat each subscribes_to / publishes entry as a node id (the simplest
 * read of the field — operators wire these as target ids or channel names);
 * an entry that names a known target produces a real edge, others are inert.
 */
export function detectCoordinationCycles(
  edges: ReadonlyArray<readonly [string, string]>,
): string[][] {
  const adj = new Map<string, string[]>()
  for (const [a, b] of edges) {
    if (!adj.has(a)) adj.set(a, [])
    adj.get(a)!.push(b)
  }
  const cycles: string[][] = []
  const seen = new Set<string>()
  const onStack = new Set<string>()
  const stack: string[] = []

  function dfs(node: string): void {
    seen.add(node)
    onStack.add(node)
    stack.push(node)
    for (const next of adj.get(node) ?? []) {
      if (onStack.has(next)) {
        const idx = stack.indexOf(next)
        cycles.push([...stack.slice(idx), next])
      } else if (!seen.has(next)) {
        dfs(next)
      }
    }
    onStack.delete(node)
    stack.pop()
  }

  for (const node of adj.keys()) {
    if (!seen.has(node)) dfs(node)
  }
  return cycles
}

/* -------------------------------------------------------------------------- */
/* component                                                                  */
/* -------------------------------------------------------------------------- */

function useFamily(family: 'target' | 'analyst' | 'source') {
  return useQuery<DescriptorRow[]>({
    queryKey: ['registry-wiring', family],
    queryFn: () =>
      apiGet<DescriptorRow[]>(
        `/registry/descriptors?family=${family}&head_only=true&limit=500`,
      ),
    refetchInterval: 60_000,
  })
}

export default function RegistryWiringsPanel({ registration }: PanelProps) {
  const [query, setQuery] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)

  // Three fixed top-level hook calls — stable order (Rules of Hooks).
  const targetsQ = useFamily('target')
  const analystsQ = useFamily('analyst')
  const sourcesQ = useFamily('source')

  const isLoading = targetsQ.isLoading || analystsQ.isLoading || sourcesQ.isLoading
  const error = targetsQ.error ?? analystsQ.error ?? sourcesQ.error
  const refetchAll = () => {
    void targetsQ.refetch()
    void analystsQ.refetch()
    void sourcesQ.refetch()
  }

  const targets = targetsQ.data ?? []
  const analysts = analystsQ.data ?? []
  const sources = sourcesQ.data ?? []

  // index analysts by id for inline-analyst-use lookups
  const analystById = useMemo(() => {
    const m = new Map<string, DescriptorRow>()
    for (const a of analysts) m.set(a.descriptor_id, a)
    return m
  }, [analysts])

  /** Per-target resolved wiring (sources + analyst + coordination). */
  const targetWirings = useMemo(() => {
    return targets.map((t) => {
      const body = (t.body ?? {}) as TargetBody
      const refs = body.sources ?? []
      const resolved = refs.map((ref) => {
        if (ref.source_id) {
          const match = sources.find((s) => s.descriptor_id === ref.source_id)
          return {
            kind: 'explicit' as const,
            label: ref.source_id,
            matches: match ? [match] : [],
            missing: !match,
            predicate: ref.subscription?.predicate ?? null,
          }
        }
        const sel = (ref.source_selector ?? {}) as SourceSelector
        const matches = sources.filter((s) =>
          selectorMatches(sel, (s.body ?? {}) as SourceBody),
        )
        return {
          kind: 'selector' as const,
          label: describeSelector(sel),
          matches,
          missing: false,
          predicate: sel.predicate ?? ref.subscription?.predicate ?? null,
        }
      })
      const totalSources = new Set(resolved.flatMap((r) => r.matches.map((m) => m.descriptor_id)))
      return {
        target: t,
        analystUse: body.analyst?.use ?? null,
        coordination: body.coordination ?? {},
        resolved,
        sourceCount: totalSources.size,
      }
    })
  }, [targets, sources])

  // coordination edges: target -> each subscribes_to / publishes entry
  const coordinationCycles = useMemo(() => {
    const edges: Array<[string, string]> = []
    for (const t of targets) {
      const co = ((t.body ?? {}) as TargetBody).coordination ?? {}
      for (const sub of co.subscribes_to ?? []) edges.push([sub, t.descriptor_id])
      for (const pub of co.publishes ?? []) edges.push([t.descriptor_id, pub])
    }
    return detectCoordinationCycles(edges)
  }, [targets])

  // analyst↔analyst subscription edges (advisory)
  const analystEdges = useMemo(() => {
    const out: Array<{ from: string; to: string }> = []
    for (const a of analysts) {
      const subs = ((a.body ?? {}) as AnalystBody).subscription?.other_analysts ?? []
      for (const s of subs) {
        const to = typeof s === 'string' ? s : s.analyst_id
        if (to) out.push({ from: a.descriptor_id, to })
      }
    }
    return out
  }, [analysts])

  // `query` is an exact target descriptor_id picked from the ScopePicker
  // (the "less raw" replacement for a free-text id box); empty = show all.
  const filtered = useMemo(() => {
    if (!query) return targetWirings
    return targetWirings.filter((w) => w.target.descriptor_id === query)
  }, [targetWirings, query])

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${targets.length} targets · ${sources.length} sources · ${analysts.length} analysts`}
      onRefresh={refetchAll}
    >
      <div className="flex items-center gap-2 mb-2 text-xs">
        {/* less-raw: pick a real target descriptor instead of typing an id */}
        <ScopePicker
          family="target"
          value={query}
          onChange={setQuery}
          placeholder="all targets"
          className="flex-1 bg-surface-200 border border-slate-700 rounded p-1 px-2 text-slate-200"
          testId="wiring-filter"
        />
        {query && (
          <button
            type="button"
            onClick={() => setQuery('')}
            className="text-slate-400 hover:text-slate-200 text-[10px] underline shrink-0"
            data-testid="wiring-filter-clear"
          >
            clear
          </button>
        )}
        {isLoading && <span className="text-slate-500">loading…</span>}
      </div>

      <div className="text-slate-600 text-[10px] mb-2">
        wiring is derived from the live target / source / analyst descriptors — edit it
        in the Target / Analyst registries. Selector matches use the structural facets
        (tags/kind/geo/tenant); a selector <code>predicate</code> further narrows at runtime.
      </div>

      {error instanceof Error && (
        <div className="text-rose-400 text-sm">error: {error.message}</div>
      )}

      {/* coordination cycle warnings */}
      {coordinationCycles.length > 0 && (
        <div
          className="bg-rose-900/20 border border-rose-700 rounded p-2 mb-2 text-[11px] text-rose-200"
          data-testid="wiring-cycles"
        >
          <div className="font-semibold mb-1">⚠ {coordinationCycles.length} coordination cycle(s) detected</div>
          {coordinationCycles.map((c, i) => (
            <div key={i} className="font-mono text-[10px]">{c.join(' → ')}</div>
          ))}
          <div className="text-rose-300/70 mt-1">
            cycles need <code>coordination.allow_cycles=true</code> + a finite{' '}
            <code>cycle_hop_limit</code> or the reconcile loop will refuse to wire them.
          </div>
        </div>
      )}

      {/* analyst↔analyst subscription summary */}
      {analystEdges.length > 0 && (
        <div className="bg-surface-200 border border-slate-800 rounded p-2 mb-2 text-[10px]" data-testid="wiring-analyst-edges">
          <div className="text-slate-400 uppercase tracking-wide mb-1">analyst → analyst subscriptions</div>
          {analystEdges.map((e, i) => (
            <div key={i} className="font-mono text-slate-300">
              {e.from} <span className="text-slate-600">subscribes</span> {e.to}
            </div>
          ))}
        </div>
      )}

      <div className="flex-1 overflow-auto text-xs space-y-1">
        {filtered.length === 0 && !isLoading && (
          <div className="text-slate-500 text-center py-4">no targets match</div>
        )}
        {filtered.map((w) => {
          const t = w.target
          const expanded = expandedId === t.descriptor_id
          const inlineAnalyst =
            w.analystUse && analystById.has(w.analystUse) ? analystById.get(w.analystUse) : null
          const co = w.coordination
          return (
            <div key={t.descriptor_id} className="bg-surface-100 border border-slate-800 rounded p-2">
              <button
                onClick={() => setExpandedId(expanded ? null : t.descriptor_id)}
                className="w-full text-left"
                data-testid={`wiring-row-${t.descriptor_id}`}
              >
                <div className="flex items-baseline gap-2">
                  <span
                    className={`shrink-0 rounded px-1 text-[10px] ${
                      t.state === 'active'
                        ? 'bg-emerald-900 text-emerald-200'
                        : 'bg-slate-700 text-slate-200'
                    }`}
                  >
                    {t.state}
                  </span>
                  <span className="text-slate-200 truncate flex-1">{t.descriptor_id}</span>
                  <span className="shrink-0 text-sky-300 text-[10px]">{w.sourceCount} src</span>
                  {w.analystUse && (
                    <span className="shrink-0 bg-violet-900 text-violet-200 rounded px-1 text-[10px]">
                      {w.analystUse}
                    </span>
                  )}
                </div>
              </button>
              {expanded && (
                <div className="mt-2 space-y-2">
                  {/* sources fan-in */}
                  <div>
                    <div className="text-slate-400 text-[10px] uppercase tracking-wide mb-1">
                      sources → {t.descriptor_id}
                    </div>
                    {w.resolved.length === 0 && (
                      <div className="text-slate-600 text-[10px]">no SourceRefs declared</div>
                    )}
                    {w.resolved.map((r, i) => (
                      <div key={i} className="bg-surface-200 border border-slate-800 rounded p-1.5 mb-1">
                        <div className="flex items-center gap-2 text-[10px]">
                          <span
                            className={`rounded px-1 ${
                              r.kind === 'explicit'
                                ? 'bg-amber-900 text-amber-200'
                                : 'bg-sky-900 text-sky-200'
                            }`}
                          >
                            {r.kind}
                          </span>
                          <span className="text-slate-300 font-mono truncate flex-1">{r.label}</span>
                          <span className="text-slate-500 shrink-0">
                            {r.matches.length} match{r.matches.length === 1 ? '' : 'es'}
                          </span>
                        </div>
                        {r.missing && (
                          <div className="text-rose-400 text-[10px] mt-0.5">
                            ⚠ explicit source_id not found in the live source roster
                          </div>
                        )}
                        {r.predicate && (
                          <div className="text-slate-600 text-[10px] mt-0.5 font-mono truncate">
                            predicate: {r.predicate} <span className="text-slate-700">(runtime-applied)</span>
                          </div>
                        )}
                        {r.matches.length > 0 && (
                          <div className="text-slate-400 text-[10px] mt-0.5 flex flex-wrap gap-1">
                            {r.matches.slice(0, 12).map((m) => (
                              <span key={m.descriptor_id} className="bg-surface-100 border border-slate-800 rounded px-1">
                                {m.descriptor_id}
                              </span>
                            ))}
                            {r.matches.length > 12 && (
                              <span className="text-slate-600">+{r.matches.length - 12} more</span>
                            )}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>

                  {/* analyst */}
                  <div>
                    <div className="text-slate-400 text-[10px] uppercase tracking-wide mb-1">analyst</div>
                    <div className="text-slate-300 text-[10px]">
                      inline use: <span className="font-mono">{w.analystUse ?? '—'}</span>
                      {inlineAnalyst && (
                        <span className="text-slate-500"> · resolves to {inlineAnalyst.name}</span>
                      )}
                    </div>
                  </div>

                  {/* coordination */}
                  {(co.subscribes_to?.length || co.publishes?.length) ? (
                    <div>
                      <div className="text-slate-400 text-[10px] uppercase tracking-wide mb-1">coordination</div>
                      {co.subscribes_to?.length ? (
                        <div className="text-slate-300 text-[10px] font-mono">
                          subscribes_to: {co.subscribes_to.join(', ')}
                        </div>
                      ) : null}
                      {co.publishes?.length ? (
                        <div className="text-slate-300 text-[10px] font-mono">
                          publishes: {co.publishes.join(', ')}
                        </div>
                      ) : null}
                      <div className="text-slate-600 text-[10px]">
                        allow_cycles: {String(co.allow_cycles ?? false)} · hop_limit: {co.cycle_hop_limit ?? 0}
                      </div>
                    </div>
                  ) : null}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </PanelChrome>
  )
}
