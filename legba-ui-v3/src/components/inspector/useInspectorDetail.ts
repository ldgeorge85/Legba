/**
 * useInspectorDetail — Move 1's only new data code.
 *
 * A pure function of `selection` + a per-kind detail fetch. Keyed by
 * `${kind}:${id}`, react-query cached, skeleton while pending. A small resolver
 * registry maps each `SelectionKind` to one endpoint and projects its response
 * into the uniform `InspectorDetail` shape the Inspector renders. Unknown kinds
 * (or fetch failures) degrade to a label + raw selection — never blank, never
 * crash.
 *
 * Endpoint honesty (verified against the registry API):
 *   - finding/situation/signal → reuse the Why lineage walk
 *       GET /lineage/{kind}/{id}?direction=upstream&depth=6
 *     (there is no /findings/{id} GET; the lineage `root` carries the row's
 *      title/target/analyst/schema, and the report gives DERIVED-FROM refs).
 *   - entity   → GET /entities/{id}            (profile + linked signals + rels)
 *   - source   → GET /sources/{id}             (source descriptor)
 *   - target   → GET /descriptors/target/{id}  (target descriptor)
 *   - analyst  → GET /descriptors/analyst/{id} (analyst descriptor)
 */
import { useQuery } from '@tanstack/react-query'
import { apiGet, ApiError } from '@/lib/api'
import type { LineageReport } from '@/lib/graphModel'
import type { Selection, SelectionKind } from '@/state/selection'

/** A forward/related reference rendered as a RecordLink in the Inspector. */
export interface Ref {
  kind: SelectionKind
  id: string
  label?: string
  /** e.g. "derived from", "linked signal", "relationship". */
  relation?: string
}

/** The uniform detail shape the Inspector renders. */
export interface InspectorDetail {
  kind: SelectionKind
  id: string
  label: string
  /** Identity fields surfaced at the top (severity, ts, status, target…). */
  core: Record<string, unknown>
  /** The full record → DescriptorView. */
  body: Record<string, unknown>
  /** Forward references (DERIVED FROM / linked) — Move 4 RecordLinks. */
  refs: Ref[]
  /** Reverse / related counts — clickable badges. */
  related: Array<{ label: string; kind: SelectionKind; count: number }>
  /** The lineage report, when the kind is lineage-walkable (reused by the trail). */
  lineage?: LineageReport
}

/** Selection kinds whose detail comes from the lineage walk. */
const WALKABLE = new Set<SelectionKind>(['finding', 'situation', 'signal'])

function str(v: unknown): string | undefined {
  return typeof v === 'string' && v.length > 0 ? v : undefined
}

// ---------------------------------------------------------------------------
// Resolvers — one per kind. Each fetches + projects into InspectorDetail.
// ---------------------------------------------------------------------------

/** A `/findings` row — only the fields we read for the Inspector report body. */
interface FindingsBodyRow {
  id: string
  body?: string | null
  data?: Record<string, unknown> | null
}

/**
 * The lineage `root` node carries metadata only (title/target/analyst/schema) —
 * NOT the report text, which lives in the finding's `data` payload. So for a
 * `finding` root we fetch the actual report from `/findings` (the only endpoint
 * that returns `data`), narrowed by the root's target/analyst and matched by id,
 * and return its payload to merge into the Inspector body. Degrades to null
 * (Inspector shows metadata only, as before) when the row is outside the window
 * or the kind isn't a plain `finding` (the endpoint is hard-fixed to that kind).
 */
async function fetchFindingReport(root: {
  id: string
  row_kind: string
  target_id: string | null
  analyst_id: string | null
}): Promise<Record<string, unknown> | null> {
  if (root.row_kind !== 'finding') return null
  const params = new URLSearchParams({ limit: '100' })
  if (root.target_id) params.set('target_id', root.target_id)
  if (root.analyst_id) params.set('analyst_id', root.analyst_id)
  try {
    const resp = await apiGet<{ data: FindingsBodyRow[] }>(`/findings?${params.toString()}`)
    const match = resp.data.find((r) => r.id === root.id)
    if (!match) return null
    const payload: Record<string, unknown> =
      match.data && typeof match.data === 'object' ? { ...match.data } : {}
    if (str(match.body) && payload.body == null) payload.body = match.body
    return Object.keys(payload).length > 0 ? payload : null
  } catch {
    return null
  }
}

/** finding / situation / signal — the lineage walk is the honest detail source. */
async function resolveWalkable(sel: Selection): Promise<InspectorDetail> {
  // `instanceKey` carries the true substrate kind when the cross-room kind was
  // coerced (e.g. hypothesis→finding) — walk the real table.
  const walkKind = sel.instanceKey ?? sel.kind
  const report = await apiGet<LineageReport>(
    `/lineage/${encodeURIComponent(walkKind)}/${encodeURIComponent(sel.id)}?direction=upstream&depth=6`,
  )
  const root = report.root
  // The report text. The backend now ships the payload on the lineage root
  // (`root.body`) for EVERY walkable kind + any age. Fall back to the /findings
  // window fetch only when it's absent (the deploy transition, or a kind the
  // backend left null) — keeps the Inspector showing the report either way.
  let report_body: Record<string, unknown> | null =
    root.body && typeof root.body === 'object' && Object.keys(root.body).length > 0
      ? (root.body as Record<string, unknown>)
      : null
  if (!report_body) report_body = await fetchFindingReport(root)
  const core: Record<string, unknown> = {}
  if (root.row_kind) core.kind = root.row_kind
  if (root.target_id) core.target = root.target_id
  if (root.analyst_id) core.analyst = root.analyst_id
  if (root.produced_at) core.produced_at = root.produced_at
  if (root.schema_uri) core.schema = root.schema_uri

  // DERIVED FROM — the immediate parents of the root (one hop upstream).
  const parentIds = new Set(report.edges.filter((e) => e.child === root.id).map((e) => e.parent))
  const byId = new Map(report.nodes.map((n) => [n.id, n]))
  const refs: Ref[] = []
  for (const pid of parentIds) {
    const n = byId.get(pid)
    if (!n) continue
    refs.push({
      kind: rowKindToSelection(n.row_kind),
      id: n.id,
      label: n.title ?? n.id,
      relation: 'derived from',
    })
  }

  return {
    kind: sel.kind,
    id: sel.id,
    label: root.title ?? sel.label ?? sel.id,
    core,
    body: {
      // The report payload FIRST (summary / body / assessment / …), then the
      // identity fields — so DescriptorView's BODY_PRIMARY floats the actual
      // report text to the top instead of showing metadata only.
      ...(report_body ?? {}),
      title: root.title,
      target_id: root.target_id,
      analyst_id: root.analyst_id,
      schema_uri: root.schema_uri,
      produced_at: root.produced_at,
    },
    refs,
    related: [],
    lineage: report,
  }
}

/** entity — full profile + linked signals + relationships. */
interface EntityDetailResp {
  id: string
  name?: string | null
  kind?: string | null
  geo?: unknown
  mention_count?: number | null
  linked_signals?: Array<{ id: string; title?: string | null }>
  relationships?: Array<{ id?: string; name?: string | null; rel?: string | null; entity_id?: string }>
  [k: string]: unknown
}

async function resolveEntity(sel: Selection): Promise<InspectorDetail> {
  const d = await apiGet<EntityDetailResp>(`/entities/${encodeURIComponent(sel.id)}`)
  const refs: Ref[] = []
  for (const s of d.linked_signals ?? []) {
    refs.push({ kind: 'signal', id: s.id, label: s.title ?? s.id, relation: 'mentions signal' })
  }
  for (const r of d.relationships ?? []) {
    const rid = r.entity_id ?? r.id
    if (rid) {
      refs.push({ kind: 'entity', id: rid, label: r.name ?? rid, relation: r.rel ?? 'related' })
    }
  }
  const { linked_signals: _ls, relationships: _rel, ...body } = d
  return {
    kind: 'entity',
    id: sel.id,
    label: str(d.name) ?? sel.label ?? sel.id,
    core: {
      kind: d.kind ?? undefined,
      mentions: d.mention_count ?? undefined,
    },
    body: body as Record<string, unknown>,
    refs,
    related: [{ label: 'Linked signals', kind: 'signal', count: (d.linked_signals ?? []).length }],
  }
}

/** Descriptor row shape for target/analyst/source. */
interface DescriptorResp {
  descriptor_id?: string
  id?: string
  title?: string | null
  name?: string | null
  body?: Record<string, unknown>
  spec?: Record<string, unknown>
  [k: string]: unknown
}

function descriptorDetail(kind: SelectionKind, sel: Selection, d: DescriptorResp): InspectorDetail {
  const body = (d.body ?? d.spec ?? d) as Record<string, unknown>
  return {
    kind,
    id: sel.id,
    label: str(d.title) ?? str(d.name) ?? sel.label ?? sel.id,
    core: {
      descriptor_id: d.descriptor_id ?? d.id ?? sel.id,
    },
    body,
    refs: [],
    related: [],
  }
}

async function resolveSource(sel: Selection): Promise<InspectorDetail> {
  const d = await apiGet<DescriptorResp>(`/sources/${encodeURIComponent(sel.id)}`)
  return descriptorDetail('source', sel, d)
}

async function resolveTarget(sel: Selection): Promise<InspectorDetail> {
  const d = await apiGet<DescriptorResp>(`/descriptors/target/${encodeURIComponent(sel.id)}`)
  return descriptorDetail('target', sel, d)
}

async function resolveAnalyst(sel: Selection): Promise<InspectorDetail> {
  const d = await apiGet<DescriptorResp>(`/descriptors/analyst/${encodeURIComponent(sel.id)}`)
  return descriptorDetail('analyst', sel, d)
}

/** Fallback — never blank, never crash: render the raw selection. */
function rawDetail(sel: Selection): InspectorDetail {
  return {
    kind: sel.kind,
    id: sel.id,
    label: sel.label ?? sel.id,
    core: { kind: sel.kind, id: sel.id },
    body: { id: sel.id, kind: sel.kind, label: sel.label ?? null, origin: sel.origin ?? null },
    refs: [],
    related: [],
  }
}

/** Local copy of the row→selection coercion (avoids a store import cycle). */
function rowKindToSelection(rowKind: string): SelectionKind {
  switch (rowKind) {
    case 'finding':
    case 'meta_finding':
    case 'hypothesis':
    case 'prediction':
    case 'alert':
    case 'critique':
    case 'prompt_module_candidate':
      return 'finding'
    case 'situation':
      return 'situation'
    case 'signal':
      return 'signal'
    case 'entity':
      return 'entity'
    case 'target':
      return 'target'
    case 'analyst':
      return 'analyst'
    case 'source':
      return 'source'
    default:
      return 'finding'
  }
}

const RESOLVERS: Record<SelectionKind, (sel: Selection) => Promise<InspectorDetail>> = {
  finding: resolveWalkable,
  situation: resolveWalkable,
  signal: resolveWalkable,
  entity: resolveEntity,
  source: resolveSource,
  target: resolveTarget,
  analyst: resolveAnalyst,
}

async function resolveDetail(sel: Selection): Promise<InspectorDetail> {
  const resolver = WALKABLE.has(sel.kind) ? resolveWalkable : RESOLVERS[sel.kind]
  if (!resolver) return rawDetail(sel)
  try {
    return await resolver(sel)
  } catch (e) {
    // 404 / not-found / missing endpoint — degrade to the raw selection rather
    // than a blank or an error wall. The operator still sees what they picked.
    if (e instanceof ApiError) return rawDetail(sel)
    throw e
  }
}

export interface UseInspectorDetailResult {
  detail: InspectorDetail | null
  isLoading: boolean
  isError: boolean
  error: unknown
  refetch: () => void
}

/**
 * Fetch + cache the detail for the current selection. Returns `detail: null`
 * with `isLoading: false` when nothing is selected (the Inspector renders its
 * empty state — the world-assessment one-pager).
 */
export function useInspectorDetail(selection: Selection | null): UseInspectorDetailResult {
  const q = useQuery<InspectorDetail>({
    enabled: selection != null,
    // instanceKey participates in the key so a coerced kind re-fetches.
    queryKey: ['inspector-detail', selection?.kind, selection?.id, selection?.instanceKey],
    queryFn: () => resolveDetail(selection as Selection),
    staleTime: 30_000,
  })
  return {
    detail: selection ? q.data ?? null : null,
    isLoading: selection != null && q.isLoading,
    isError: q.isError,
    error: q.error,
    refetch: () => void q.refetch(),
  }
}
