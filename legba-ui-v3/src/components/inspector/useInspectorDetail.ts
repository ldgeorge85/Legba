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
 *   - source   → GET /registry/descriptors/source/{id}  (source descriptor)
 *   - target   → GET /registry/descriptors/target/{id}  (target descriptor)
 *   - analyst  → GET /registry/descriptors/analyst/{id} (analyst descriptor)
 */
import { useQuery } from '@tanstack/react-query'
import {
  contentionFences,
  contentionPages,
  decisiveRef,
  type ContentionsResponse,
} from '@/lib/contentionsModel'
import { apiGet, ApiError, fetchJournalEntry } from '@/lib/api'
import type { LineageReport } from '@/lib/graphModel'
import {
  ABSENCE_KIND_LABEL,
  absenceExtent,
  absenceHeadline,
  matchAbsence,
  matchAbsenceSubject,
  notMeasuredReason,
  parseAbsenceId,
  type AbsenceKind,
  type AbsenceResponse,
} from '@/lib/absenceModel'
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
const WALKABLE = new Set<SelectionKind>(['finding', 'situation', 'signal', 'event'])

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
  /** P0-T3 faithfulness-verify block — `/findings` projects it as a top-level
   *  sibling of `data` (null on a legacy/unverified row). */
  verification?: Record<string, unknown> | null
}

/** What the `/findings` window fetch yields for the Inspector merge. */
interface FindingReportFetch {
  payload: Record<string, unknown> | null
  verification: Record<string, unknown> | null
}

/**
 * The lineage `root` node carries metadata only (title/target/analyst/schema) —
 * NOT the report text, which lives in the finding's `data` payload, and NOT
 * the faithfulness `verification` block, which only the `/findings` projection
 * carries (it is derived from the verify critique row, never stored inside the
 * `data` envelope). So for a `finding` root we fetch the row from `/findings`,
 * narrowed by the root's target/analyst and matched by id, and return payload
 * + verification to merge into the Inspector body — the verify block feeds
 * both the report's VerdictBadge and the citation chips' per-claim hover
 * verdicts (P1-8). Degrades to nulls (Inspector shows what it has; the chips
 * honestly say "claim-level verdict not recorded") when the row is outside the
 * window or the kind isn't a plain `finding` (the endpoint is fixed to it).
 */
async function fetchFindingReport(root: {
  id: string
  row_kind: string
  target_id: string | null
  analyst_id: string | null
}): Promise<FindingReportFetch> {
  if (root.row_kind !== 'finding') return { payload: null, verification: null }
  const params = new URLSearchParams({ limit: '100' })
  if (root.target_id) params.set('target_id', root.target_id)
  if (root.analyst_id) params.set('analyst_id', root.analyst_id)
  try {
    const resp = await apiGet<{ data: FindingsBodyRow[] }>(`/findings?${params.toString()}`)
    const match = resp.data.find((r) => r.id === root.id)
    if (!match) return { payload: null, verification: null }
    const payload: Record<string, unknown> =
      match.data && typeof match.data === 'object' ? { ...match.data } : {}
    if (str(match.body) && payload.body == null) payload.body = match.body
    const verification =
      match.verification && typeof match.verification === 'object' ? match.verification : null
    return { payload: Object.keys(payload).length > 0 ? payload : null, verification }
  } catch {
    return { payload: null, verification: null }
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
  // The faithfulness verify block lives ONLY on the /findings projection (it
  // is a critique-derived sibling, never inside the data envelope), so a
  // finding hydrates it from the window fetch even when the lineage root
  // already supplied the report body — it drives the per-claim citation-chip
  // verdicts (P1-8). Absent ⇒ the chips honestly say "not recorded".
  let verification: Record<string, unknown> | null =
    report_body?.verification && typeof report_body.verification === 'object'
      ? (report_body.verification as Record<string, unknown>)
      : null
  if (!report_body || (!verification && root.row_kind === 'finding')) {
    const fetched = await fetchFindingReport(root)
    if (!report_body) report_body = fetched.payload
    if (!verification) verification = fetched.verification
  }
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
      // The /findings-hydrated verify block (P1-8) — only set when it exists.
      ...(verification ? { verification } : {}),
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
  const d = await apiGet<DescriptorResp>(`/registry/descriptors/source/${encodeURIComponent(sel.id)}`)
  return descriptorDetail('source', sel, d)
}

async function resolveTarget(sel: Selection): Promise<InspectorDetail> {
  const d = await apiGet<DescriptorResp>(`/registry/descriptors/target/${encodeURIComponent(sel.id)}`)
  return descriptorDetail('target', sel, d)
}

async function resolveAnalyst(sel: Selection): Promise<InspectorDetail> {
  const d = await apiGet<DescriptorResp>(`/registry/descriptors/analyst/${encodeURIComponent(sel.id)}`)
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

/**
 * A REPORT is a `kind='finding'` row (design §0), so it resolves through the
 * same lineage walk — but `/lineage/report/<id>` is not a table, so the walk
 * kind is pinned to `finding` here rather than read off `sel.kind`.
 */
function resolveReport(sel: Selection): Promise<InspectorDetail> {
  return resolveWalkable({ ...sel, instanceKey: sel.instanceKey ?? 'finding' })
}

/**
 * A journal entry / consolidation — `GET /journal/{id}` (decision 6). Before
 * this existed the Journal could not put its rows in the shared selection at
 * all, so it carried a parallel reader stack keyed on a component-local
 * `selectedRowId`.
 */
async function resolveJournalEntry(sel: Selection): Promise<InspectorDetail> {
  const entry = (await fetchJournalEntry(sel.id)) as unknown as Record<string, unknown>
  return {
    kind: 'journal_entry',
    id: sel.id,
    label: str(entry.title) ?? sel.label ?? sel.id,
    core: {
      kind: str(entry.kind) ?? undefined,
      produced_at: str(entry.created_at) ?? str(entry.produced_at) ?? undefined,
    },
    body: entry,
    refs: [],
    related: [],
  }
}

/** The absence kinds a bounded UNIT can be classed under by the route. */
const UNIT_KINDS: AbsenceKind[] = ['not_collected', 'source_stale']

/**
 * The `proof.ref_kind`s that resolve to a real Inspector selection. The ROUTE
 * says what a ref is; this maps only the two that have a resolver, so a
 * scorecard row or a map_version is shown as text instead of as a link that
 * would 404.
 */
const REF_KIND_TO_SELECTION: Record<string, SelectionKind> = {
  finding: 'finding',
  source: 'source',
}

/**
 * A TYPED ABSENCE (7b/k5) — the selection whose record does not exist.
 *
 * The id encodes `scope|kind|subject`; the detail comes from
 * `GET /v3/absence?scope=` and is the matching item's PROOF. Three honest
 * outcomes, never a blank:
 *
 *   * a match → the reason, the extent, and `proof.what_was_checked /
 *     checked_at / ref`, with `ref` offered as a RecordLink when the kind's ref
 *     is a substrate row (a silent unit's latest read, an audit row's read);
 *   * the kind was NOT MEASURED for this desk → the route's own sentence
 *     saying so, so "not checked" never reads as "nothing absent";
 *   * read cleanly, no item → "no typed absence is recorded for this subject",
 *     which is a real answer about a desk whose gap has since closed.
 */
async function resolveAbsence(sel: Selection): Promise<InspectorDetail> {
  const parsed = parseAbsenceId(sel.id)
  if (!parsed) return rawDetail(sel)
  const { scope, kind, subject } = parsed
  const resp = await apiGet<AbsenceResponse>(
    `/v3/absence?scope=${encodeURIComponent(scope)}`,
  )
  // A silent unit is `not_collected` or `source_stale` depending on whether it
  // ever ran, and that is the ROUTE's call — so a unit drill matches on the
  // subject across the unit kinds rather than asserting a classification the
  // clicked cell does not own. Every other kind is addressed exactly.
  const item = UNIT_KINDS.includes(kind)
    ? matchAbsenceSubject(resp, subject, UNIT_KINDS)
    : matchAbsence(resp, kind, subject)
  const label = sel.label ?? `${subject} — ${ABSENCE_KIND_LABEL[kind]}`
  const core: Record<string, unknown> = { subject, scope, read_at: resp.read_at }
  if (!item) {
    // Two different answers, and the panel must never collapse them: the kind
    // was not CHECKED for this desk, or it was checked and this subject is not
    // absent. A blank would say the first when the second is true.
    const why = notMeasuredReason(resp, kind)
    core.absence_kind = ABSENCE_KIND_LABEL[kind]
    core.state = why ? 'not measured' : 'no typed absence recorded'
    return {
      kind: 'absence',
      id: sel.id,
      label,
      core,
      body: {
        reason: why
          ? `not measured for this desk: ${why}`
          : 'no typed absence is recorded for this subject as of the read above',
        proof: null,
      },
      refs: [],
      related: [],
    }
  }
  core.absence_kind = ABSENCE_KIND_LABEL[item.kind]
  core.extent = absenceExtent(item)
  // The one line that says when this was established and whether anyone has
  // looked since — a stale item reads "last known absence, not re-checked"
  // rather than passing for a current one.
  core.measured = absenceHeadline(item)
  if (item.stale) core.stale = true
  return {
    kind: 'absence',
    id: sel.id,
    label,
    core,
    body: {
      reason: item.reason,
      what_was_checked: item.proof.what_was_checked,
      checked_at: item.proof.checked_at,
      ref: item.proof.ref,
      since: item.since,
      window: item.window,
      as_of: item.as_of,
      as_of_basis: item.as_of_basis,
      expires_at: item.expires_at,
      review: item.review ?? null,
      stale: item.stale,
    },
    // A silent unit's ref is its latest read, an audited claim's is the read
    // that published it, a silent source's is the source — so the proof stays
    // one click from the record it was measured against wherever a resolver
    // exists. A scorecard card or a map_version has none and is shown as text
    // in the body, never as a link that would 404.
    refs:
      item.proof.ref && item.proof.ref_kind && REF_KIND_TO_SELECTION[item.proof.ref_kind]
        ? [
            {
              kind: REF_KIND_TO_SELECTION[item.proof.ref_kind],
              id: item.proof.ref,
              relation: 'what was checked',
            },
          ]
        : [],
    related: [],
  }
}


/**
 * A CONTENTION (7a) — the retrieval that went looking for the claim's opposite.
 *
 * The id is the claim key; the detail comes from
 * `GET /v3/contentions?claim_id=` and is the RECORD, not a judgement:
 *
 *   * `core` states what was asked and on which rung, how many words the
 *     counter-query carried that the claim did not (the pass's own
 *     paraphrase-quality number), THE FOUR FENCES' readings for the decisive
 *     page, and whether the record is still live;
 *   * `body` carries the hedged statement, the page's quoted sentence, its
 *     URL, the date the PAGE states (absent when it states none — never
 *     today's), the hash of the bytes this platform holds, and EVERY page the
 *     pass fetched with the fence readings that admitted or refused it;
 *   * `refs` links the read the claim was published in, so the claim is one
 *     click back.
 *
 * THE FENCES ARE PART OF THE ANSWER, not a footnote to it (migration 0222).
 * "A page we hold states the opposite" is only worth reading because a
 * reference host could not have said it, an undated page could not have said
 * it, an off-subject sentence could not have said it, and one page alone would
 * have been a qualification — so `independent_pages` reads as "N of 2
 * independent pages" with what falling short of the bar meant, and a reading
 * nobody took reads as `not measured` rather than as 0.
 *
 * NOTHING HERE ADJUDICATES, and the detail says so in words rather than
 * leaving a reader to infer it: `contested` is a fact about a retrieval, and
 * which side is right is a judgement this platform does not make here.
 */
async function resolveContention(sel: Selection): Promise<InspectorDetail> {
  const resp = await apiGet<ContentionsResponse>(
    `/v3/contentions?claim_id=${encodeURIComponent(sel.id)}`,
  )
  const row = (resp?.contentions ?? [])[0] ?? null
  const label = sel.label ?? 'contested by retrieval'
  if (!row) {
    // Read cleanly, no row. A real answer about a claim whose record has since
    // been superseded or pruned — never a blank.
    return {
      kind: 'contention',
      id: sel.id,
      label,
      core: { state: 'no contention record for this claim' },
      body: { note: resp?.note ?? '' },
      refs: [],
      related: [],
    }
  }
  const ref = decisiveRef(row)
  return {
    kind: 'contention',
    id: sel.id,
    label,
    core: {
      stance: row.stance,
      derivation:
        row.derivation === 'negation'
          ? 'negation (uncalibrated — read the page)'
          : row.derivation,
      counter_query: row.query,
      query_source: row.query_source,
      new_words_vs_the_claim: row.query_novel_tokens,
      rung: row.rung,
      // The four fences, each a reading or `not measured` — never 0.
      ...contentionFences(row),
      retrieved_at: row.retrieved_at,
      state: row.live ? 'live' : 'expired, not re-checked',
    },
    body: {
      claim: row.claim_text,
      statement: row.statement,
      quote: ref?.quote ?? '',
      counter_url: ref?.url ?? '',
      // The page's OWN date. Absent renders as absence — the fetch leg
      // discovers a date and never invents one.
      published_at: ref?.published_at ?? 'no publication date stated',
      sha256: ref?.sha256 ?? '',
      pages_read: row.refs.length,
      // EVERY page, not just the decisive one: a bar of two independent outlets
      // cannot be checked against a single row, and "three of these four were
      // encyclopedia entries" is a thing the count alone does not say.
      pages: contentionPages(row),
      linked_corpus_rows: row.linked_signals,
      note: resp?.note ?? '',
    },
    refs: row.finding_id
      ? [{ kind: 'finding', id: row.finding_id, relation: 'the read that published this claim' }]
      : [],
    related: [],
  }
}

const RESOLVERS: Record<SelectionKind, (sel: Selection) => Promise<InspectorDetail>> = {
  finding: resolveWalkable,
  situation: resolveWalkable,
  signal: resolveWalkable,
  // V3/P6 — `event` is walkable (a first-class root kind in _TABLES_BY_KIND).
  event: resolveWalkable,
  entity: resolveEntity,
  source: resolveSource,
  target: resolveTarget,
  analyst: resolveAnalyst,
  report: resolveReport,
  journal_entry: resolveJournalEntry,
  absence: resolveAbsence,
  contention: resolveContention,
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
