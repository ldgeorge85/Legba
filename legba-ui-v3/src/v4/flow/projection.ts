/**
 * The Flow — F.A projection. Turns the four descriptor families (sources,
 * targets, analysts, action_packs) into a {@link GraphProjection}: one
 * {@link FlowNode} per HEAD descriptor + the wiring edges between them.
 *
 * Reads the live registry (no mutation):
 *   - GET /registry/descriptors?family=source|target|analyst&head_only=true
 *   - GET /registry/action_packs
 *
 * Body shapes are mirrored from the authoritative pydantic schemas
 * (src/legba/data/schemas/{source,target,analyst,action_pack}.py):
 *   - target body.scope.{geo,tags}  (scope is domain-discriminated; geo lives
 *     on the geo + entity domains), target body.sources[].{source_id,
 *     source_selector.{geo,tags}}, target body.allowed_action_packs[].pack_id
 *   - analyst body.subscription.targets (a SubscriptionTargets *predicate*
 *     object — {predicate,data_types,time_window} — NOT a target-id list;
 *     null/absent ⇒ a meta analyst with no target subscription),
 *     analyst body.action_packs[].pack_id, analyst body.method.kind
 *   - source body.kind, source body.scope.{geo,tags}
 *
 * The orchestrator owns node `type` (omitted here) and the live store; this
 * file produces *positions-at-origin* nodes — layout.ts assigns geometry.
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import type {
  FlowNode,
  FlowNodeKind,
  FlowEdge,
  FlowEdgeKind,
  GraphProjection,
  LifecycleState,
} from './types'

// ---------------------------------------------------------------------------
// Wire shapes (the head_only descriptor list + the action_pack projection).
// Only the fields the projection reads are typed; everything else stays in
// `body` as Record<string, unknown> and is probed defensively.
// ---------------------------------------------------------------------------

interface DescriptorRow {
  descriptor_id: string
  family?: string
  kind?: string | null
  state: string
  name: string
  body?: Record<string, unknown>
}

/** Families fetched as plain registry descriptors. */
const DESCRIPTOR_FAMILIES = ['source', 'target', 'analyst'] as const

/** Map a descriptor family string → FlowNodeKind (action_pack collapses to pack). */
function familyToKind(family: string): FlowNodeKind {
  switch (family) {
    case 'source':
      return 'source'
    case 'target':
      return 'target'
    case 'analyst':
      return 'analyst'
    case 'action_pack':
    case 'pack':
      return 'pack'
    default:
      // Defensive: unknown family lands in the source lane rather than crashing.
      return 'source'
  }
}

const RETIRED: LifecycleState = 'retired'

function asRecord(v: unknown): Record<string, unknown> | undefined {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined
}

function asStringArray(v: unknown): string[] {
  if (!Array.isArray(v)) return []
  return v.filter((x): x is string => typeof x === 'string')
}

/** Lower-cased, de-duped token set — overlap is computed case-insensitively. */
function tokenSet(values: string[]): Set<string> {
  const s = new Set<string>()
  for (const v of values) s.add(v.toLowerCase())
  return s
}

function setsOverlap(a: Set<string>, b: Set<string>): boolean {
  if (a.size === 0 || b.size === 0) return false
  const [small, big] = a.size <= b.size ? [a, b] : [b, a]
  for (const v of small) if (big.has(v)) return true
  return false
}

/** Read a descriptor body's `scope.geo` + `scope.tags` (target/source). */
function readScope(body: Record<string, unknown> | undefined): { geo: string[]; tags: string[] } {
  const scope = asRecord(body?.scope)
  return {
    geo: asStringArray(scope?.geo),
    tags: asStringArray(scope?.tags),
  }
}

/** ActionPackRef[] → the pack ids it grants (each ref is `{pack_id, ...}`). */
function readPackIds(body: Record<string, unknown> | undefined, field: string): string[] {
  const raw = body?.[field]
  if (!Array.isArray(raw)) return []
  const out: string[] = []
  for (const r of raw) {
    const rec = asRecord(r)
    const id = rec?.pack_id
    if (typeof id === 'string' && id) out.push(id)
  }
  return out
}

/** One target's source-binding refs (target body.sources[] = SourceRef[]). */
interface TargetSourceRef {
  sourceId: string | null
  selectorGeo: Set<string>
  selectorTags: Set<string>
}

function readTargetSourceRefs(body: Record<string, unknown> | undefined): TargetSourceRef[] {
  const raw = body?.sources
  if (!Array.isArray(raw)) return []
  const out: TargetSourceRef[] = []
  for (const r of raw) {
    const rec = asRecord(r)
    if (!rec) continue
    const sourceId = typeof rec.source_id === 'string' && rec.source_id ? rec.source_id : null
    const sel = asRecord(rec.source_selector)
    out.push({
      sourceId,
      selectorGeo: tokenSet(asStringArray(sel?.geo)),
      selectorTags: tokenSet(asStringArray(sel?.tags)),
    })
  }
  return out
}

// ---------------------------------------------------------------------------
// Projection build
// ---------------------------------------------------------------------------

interface FamilyData {
  sources: DescriptorRow[]
  targets: DescriptorRow[]
  analysts: DescriptorRow[]
  packs: DescriptorRow[]
}

function nodeFor(row: DescriptorRow, family: string): FlowNode {
  const kind = familyToKind(family)
  const body = row.body
  const methodKind = asRecord(body?.method)?.kind
  const subkind =
    (typeof row.kind === 'string' && row.kind) ||
    (typeof methodKind === 'string' && methodKind) ||
    (typeof body?.kind === 'string' && (body.kind as string)) ||
    undefined
  return {
    id: row.descriptor_id,
    data: {
      kind,
      descriptorId: row.descriptor_id,
      label: row.name || row.descriptor_id,
      state: row.state as LifecycleState,
      family,
      subkind,
    },
    position: { x: 0, y: 0 },
  }
}

function buildProjection(data: FamilyData): GraphProjection {
  const isLive = (r: DescriptorRow): boolean => r.state !== RETIRED

  const sources = data.sources.filter(isLive)
  const targets = data.targets.filter(isLive)
  const analysts = data.analysts.filter(isLive)
  const packs = data.packs.filter(isLive)

  const nodes: FlowNode[] = [
    ...sources.map((r) => nodeFor(r, 'source')),
    ...targets.map((r) => nodeFor(r, 'target')),
    ...analysts.map((r) => nodeFor(r, 'analyst')),
    ...packs.map((r) => nodeFor(r, 'action_pack')),
  ]

  // Node-id presence sets so we never emit an edge to a missing/retired node.
  const sourceIds = new Set(sources.map((r) => r.descriptor_id))
  const targetIds = new Set(targets.map((r) => r.descriptor_id))
  const analystIds = new Set(analysts.map((r) => r.descriptor_id))
  const packIds = new Set(packs.map((r) => r.descriptor_id))

  // Precompute each ACTIVE source's geo + tag token sets (subscription match).
  const activeSources = sources.filter((r) => r.state === 'active')
  const sourceScopes = activeSources.map((r) => {
    const { geo, tags } = readScope(r.body)
    return { id: r.descriptor_id, geo: tokenSet(geo), tags: tokenSet(tags) }
  })

  const edges: FlowEdge[] = []
  const seen = new Set<string>()
  const push = (id: string, source: string, target: string, kind: FlowEdgeKind): void => {
    if (seen.has(id)) return
    seen.add(id)
    edges.push({ id, source, target, data: { kind } })
  }

  // --- subscription (source → target) ------------------------------------
  // The platform is predicate-fanout (targets pull signals by geo/tags), so a
  // literal all-sources→all-targets graph is a hairball AND misleading. We draw a
  // source→target edge ONLY where there is real affinity — no fan-out fallback:
  //   - an explicit source_id ref on the target → that exact ACTIVE source
  //   - a NON-EMPTY source_selector ref whose geo/tags overlap a source's scope
  //   - the source's own scope.geo/tags overlaps the TARGET's scope.geo/tags
  //     (e.g. a US-scoped source feeds the US target)
  // A target with no affinity simply shows no inbound source wiring (honest:
  // global sources feed the predicate pool, not a specific target).
  for (const t of targets) {
    if (t.state !== 'active') continue
    const refs = readTargetSourceRefs(t.body)
    const tScope = readScope(t.body)
    const tGeo = tokenSet(tScope.geo)
    const tTags = tokenSet(tScope.tags)
    const matched = new Set<string>()

    for (const ref of refs) {
      if (ref.sourceId) {
        if (sourceIds.has(ref.sourceId)) matched.add(ref.sourceId)
        continue
      }
      // Skip empty selectors ("everything") — they would re-create the hairball.
      if (ref.selectorGeo.size === 0 && ref.selectorTags.size === 0) continue
      for (const sc of sourceScopes) {
        if (setsOverlap(sc.geo, ref.selectorGeo) || setsOverlap(sc.tags, ref.selectorTags)) {
          matched.add(sc.id)
        }
      }
    }

    // Geo/tag affinity between a source's own scope and the target's scope.
    if (tGeo.size > 0 || tTags.size > 0) {
      for (const sc of sourceScopes) {
        if (setsOverlap(sc.geo, tGeo) || setsOverlap(sc.tags, tTags)) {
          matched.add(sc.id)
        }
      }
    }

    for (const sid of matched) {
      push(`sub:${sid}->${t.descriptor_id}`, sid, t.descriptor_id, 'subscription')
    }
  }

  // --- analyst_target (analyst → target) ---------------------------------
  // analyst.subscription.targets is a SubscriptionTargets *predicate* object
  // ({predicate, data_types, time_window}); the operator UI cannot statically
  // resolve which targets a predicate selects, so we connect a subscribing
  // analyst to every ACTIVE target. We ALSO accept two looser runtime shapes
  // defensively — a bare target-id string, or an array of target ids/refs —
  // and honour those exactly when present. Meta analysts (targets null/absent)
  // get no analyst_target edges.
  for (const a of analysts) {
    const sub = asRecord(a.body?.subscription)
    const targetsField = sub?.targets
    if (targetsField == null) continue // meta analyst — no target subscription

    const explicit = new Set<string>()
    let predicateShaped = false

    if (typeof targetsField === 'string') {
      if (targetIds.has(targetsField)) explicit.add(targetsField)
    } else if (Array.isArray(targetsField)) {
      for (const item of targetsField) {
        if (typeof item === 'string') {
          if (targetIds.has(item)) explicit.add(item)
        } else {
          const rec = asRecord(item)
          const id = typeof rec?.id === 'string' ? rec.id : undefined
          if (id && targetIds.has(id)) explicit.add(id)
        }
      }
    } else {
      // The canonical SubscriptionTargets object — a predicate selector.
      predicateShaped = true
    }

    const dests = predicateShaped
      ? targets.filter((t) => t.state === 'active').map((t) => t.descriptor_id)
      : Array.from(explicit)

    for (const tid of dests) {
      push(`at:${a.descriptor_id}->${tid}`, a.descriptor_id, tid, 'analyst_target')
    }
  }

  // --- grant (pack → analyst, pack → target) -----------------------------
  // analyst.action_packs[].pack_id and target.allowed_action_packs[].pack_id
  // name a pack descriptor (ActionPackId === descriptor_id).
  for (const a of analysts) {
    for (const pid of readPackIds(a.body, 'action_packs')) {
      if (!packIds.has(pid)) continue
      push(`grant:${pid}->${a.descriptor_id}`, pid, a.descriptor_id, 'grant')
    }
  }
  for (const t of targets) {
    for (const pid of readPackIds(t.body, 'allowed_action_packs')) {
      if (!packIds.has(pid)) continue
      push(`grant:${pid}->${t.descriptor_id}`, pid, t.descriptor_id, 'grant')
    }
  }
  // analystIds is referenced for symmetry of the presence-set surface.
  void analystIds

  return { nodes, edges }
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

const HEAD = (family: (typeof DESCRIPTOR_FAMILIES)[number]): string =>
  `/registry/descriptors?family=${family}&head_only=true&limit=500`

/**
 * Fetch all four descriptor families and project them into a positioned-at-
 * origin {@link GraphProjection}. Memoized over the query payloads — layout.ts
 * assigns geometry downstream.
 */
export function useGraphProjection(): { projection: GraphProjection | undefined; isLoading: boolean } {
  const sourcesQ = useQuery<DescriptorRow[]>({
    queryKey: ['flow-projection', 'source'],
    queryFn: () => apiGet<DescriptorRow[]>(HEAD('source')),
    refetchInterval: 60_000,
  })
  const targetsQ = useQuery<DescriptorRow[]>({
    queryKey: ['flow-projection', 'target'],
    queryFn: () => apiGet<DescriptorRow[]>(HEAD('target')),
    refetchInterval: 60_000,
  })
  const analystsQ = useQuery<DescriptorRow[]>({
    queryKey: ['flow-projection', 'analyst'],
    queryFn: () => apiGet<DescriptorRow[]>(HEAD('analyst')),
    refetchInterval: 60_000,
  })
  const packsQ = useQuery<DescriptorRow[]>({
    queryKey: ['flow-projection', 'action_pack'],
    queryFn: () => apiGet<DescriptorRow[]>('/registry/action_packs?head_only=true&limit=500'),
    refetchInterval: 60_000,
  })

  const sources = sourcesQ.data
  const targets = targetsQ.data
  const analysts = analystsQ.data
  const packs = packsQ.data

  const projection = useMemo<GraphProjection | undefined>(() => {
    if (!sources || !targets || !analysts || !packs) return undefined
    return buildProjection({ sources, targets, analysts, packs })
  }, [sources, targets, analysts, packs])

  const isLoading =
    sourcesQ.isLoading || targetsQ.isLoading || analystsQ.isLoading || packsQ.isLoading

  return { projection, isLoading }
}
