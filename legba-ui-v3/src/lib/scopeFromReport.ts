/**
 * scopeFromReport — projecting a SCOPE out of a record the reader already has.
 *
 * The design's load-bearing claim is that **scope members cost no backend work**:
 * every id a following panel needs is already on the row the Navigator fetched.
 * `AssemblyPayload.blocks[]` carries, per block, `finding_id`, `desk`,
 * `target_id`, `target_name` and `signals[]{signal_id, source_id}`, plus
 * `coverage[]`, `drops` and `as_of` at the top level (see `lib/assemblyModel.ts`,
 * which already parses all of it). So this module is PURE: it takes rows and
 * payloads that are in hand and returns a `Scope`. It issues no fetch, imports
 * no query client, and is unit-testable against the fixtures that already exist
 * in `v4/read/__fixtures__`.
 *
 * Three entry points, one per payload shape the products actually take:
 *
 *  * {@link scopeFromAssembly} — an `assembly.v1` row (world / region / country
 *    composition). The rich case: desks, findings, signals and sources all come
 *    off the blocks.
 *  * {@link scopeFromLineage}  — a row with no assembly block, scoped from the
 *    lineage walk the Inspector already performs
 *    (`useInspectorDetail.ts:122-124`). Covers `region_rollup.v1` and the
 *    journal consolidation.
 *  * {@link scopeFromTarget}   — a bare desk. The cheapest scope there is, and
 *    the one a choropleth / desk click produces.
 *
 * ## The window
 *
 * `windowHours` is DERIVED, never invented: it is the age of the oldest piece of
 * evidence the record actually quotes (`block.evidence_age_h`, else the oldest
 * `coverage[].age_h`), rounded up to the hour and floored at
 * {@link MIN_WINDOW_HOURS}. A record that carries no age at all gets `null`, and
 * `scopeParams` then pushes no `since` — an absent window renders as absent,
 * never as a default that quietly narrows the operator's feed.
 */
import type { AssemblyPayload, ReadFindingRow } from './assemblyModel'
import { projectAssembly, readPayload } from './assemblyModel'
import type { LineageReport } from './graphModel'
import { emptyMembers, useScope, type Scope, type ScopeMembers } from '@/state/scope'
import { useSelection, type Selection } from '@/state/selection'

/**
 * The analyst ids whose `kind='finding'` rows ARE reports (design §0: "a report
 * *is* a `finding` row"). This is the whole definition of the `report` scope
 * kind — there is no separate table and no new endpoint.
 */
export const REPORT_ANALYSTS: ReadonlySet<string> = new Set([
  'world_assessor',
  'world_assessment',
  'region_composition',
  'country_composition',
  // The country-grain twin of `world_assessment` (Program 3 lane C): a fenced
  // interpretive row, a report in exactly the sense the world voice already is.
  // `lib/assessmentChannel` owns which spine it belongs to.
  'country_assessment',
  'chronicle_assessor',
])

/** A window narrower than this is not a window; it is a rounding artefact. */
export const MIN_WINDOW_HOURS = 6

/** Whether a findings row is one of the composition products. */
export function isReportRow(row: { analyst_id?: string | null }): boolean {
  return !!row.analyst_id && REPORT_ANALYSTS.has(row.analyst_id)
}

function uniq(xs: (string | null | undefined)[]): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  for (const x of xs) {
    if (!x) continue
    if (seen.has(x)) continue
    seen.add(x)
    out.push(x)
  }
  return out
}

/**
 * The evidence window a payload actually reaches back over, in hours.
 *
 * Returns null when nothing on the record carries an age — the honest answer,
 * which downstream renders as "no `since` filter" rather than as a default.
 */
export function windowHoursOf(p: AssemblyPayload): number | null {
  const ages: number[] = []
  for (const b of p.blocks) if (b.evidence_age_h != null) ages.push(b.evidence_age_h)
  for (const c of p.coverage) if (c.age_h != null) ages.push(c.age_h)
  for (const b of p.blocks) for (const s of b.signals) if (s.age_h != null) ages.push(s.age_h)
  if (ages.length === 0) return null
  const oldest = Math.max(...ages)
  if (!Number.isFinite(oldest) || oldest <= 0) return null
  return Math.max(MIN_WINDOW_HOURS, Math.ceil(oldest))
}

/**
 * Entity names a payload names, harvested from the desk/target labels it
 * carries. The assembly does not carry an NER entity list — inventing one would
 * be exactly the derivation this reader refuses — so the honest seed set for the
 * graph is the set of desk SUBJECTS the record is about.
 */
function entityNamesOf(p: AssemblyPayload): string[] {
  return uniq([
    ...p.blocks.map((b) => b.target_name),
    ...p.coverage.map((c) => c.unit_name ?? null),
  ])
}

/** Members off an `assembly.v1` payload. Pure; every id is already on the row. */
export function membersFromAssembly(p: AssemblyPayload): ScopeMembers {
  const signals = p.blocks.flatMap((b) => b.signals)
  return {
    findingIds: uniq(p.blocks.map((b) => b.finding_id)),
    targetIds: uniq(p.blocks.map((b) => b.target_id)),
    signalIds: uniq(signals.map((s) => s.signal_id)),
    sourceIds: uniq(signals.map((s) => s.source_id)),
    entityNames: entityNamesOf(p),
    analystIds: [],
    asOf: p.as_of || null,
    windowHours: windowHoursOf(p),
  }
}

/** The chip/pin label for a report row — "World read · 09-07", never a UUID. */
export function reportLabel(row: ReadFindingRow, asOf?: string | null): string {
  const stamp = asOf ?? row.produced_at
  const day = stamp ? stamp.slice(0, 10) : ''
  const title = (row.title ?? '').trim()
  if (title) return day ? `${title} · ${day}` : title
  const producer = row.analyst_id ?? 'report'
  return day ? `${producer} · ${day}` : producer
}

/**
 * THE projector the Navigator uses. One row + the payload it already fetched ⇒
 * the world that row is about.
 */
export function scopeFromAssembly(
  row: ReadFindingRow,
  p: AssemblyPayload,
  origin = 'navigator',
): Scope {
  return {
    kind: 'report',
    id: row.id,
    label: reportLabel(row, p.as_of),
    members: { ...membersFromAssembly(p), analystIds: uniq([row.analyst_id]) },
    origin,
  }
}

/**
 * Scope a row that carries no assembly block, off the lineage walk the Inspector
 * already performs. `region_rollup.v1` and the journal consolidation land here.
 *
 * The walk's nodes carry their own `row_kind`, so findings and signals separate
 * without a second request; `target_id` is read off whichever nodes have one.
 */
export function scopeFromLineage(
  row: ReadFindingRow,
  report: LineageReport | null,
  origin = 'navigator',
): Scope {
  const nodes = report?.nodes ?? []
  const findingIds = uniq(
    nodes.filter((n) => n.row_kind === 'finding' || n.row_kind === 'meta_finding').map((n) => n.id),
  )
  const signalIds = uniq(nodes.filter((n) => n.row_kind === 'signal').map((n) => n.id))
  const targetIds = uniq([row.target_id, ...nodes.map((n) => n.target_id)])
  return {
    kind: 'report',
    id: row.id,
    label: reportLabel(row),
    members: {
      ...emptyMembers(),
      findingIds: findingIds.length > 0 ? findingIds : uniq([row.id]),
      signalIds,
      targetIds,
      analystIds: uniq([row.analyst_id]),
      asOf: row.produced_at || null,
    },
    origin,
  }
}

/**
 * The cheapest scope: one desk. What a choropleth click, a desk chip or a
 * `kind:'target'` selection means.
 */
export function scopeFromTarget(targetId: string, label?: string, origin = 'desk'): Scope {
  return {
    kind: 'target',
    id: targetId,
    label: label?.trim() || targetId,
    members: { ...emptyMembers(), targetIds: [targetId] },
    origin,
  }
}

/**
 * Mirror deliberate DESK selections into the scope — the bridge that keeps
 * "pick a desk → see its findings" working after `feedView.seedDeskFilter` was
 * deleted.
 *
 * ## Why this is a bridge rather than a per-panel call site
 *
 * A desk click is a scoping act wherever it happens — the sidebar tree, a
 * choropleth polygon, a registry row, ⌘K. The design names three of those ("a
 * choropleth click, a desk chip, ⌘K") as scope-setters, and wiring each
 * separately would make scope depend on which panels happen to be mounted. One
 * subscription in the shell covers all of them and cannot drift.
 *
 * ONLY `kind:'target'` bridges. Every other kind — finding, signal, entity,
 * report — is FOCUS and leaves the scope exactly where it was, which is the
 * property `feedView.ts:7-26` could not obtain: the operator reads twenty rows
 * under one desk and the desk survives all twenty.
 *
 * Returns an unsubscribe fn.
 */
export function installDeskScopeBridge(): () => void {
  const bridge = (sel: Selection | null) => {
    if (sel?.kind !== 'target' || !sel.id) return
    const cur = useScope.getState().scope
    // Already scoped to this desk — never churn the store (a re-render must not
    // manufacture a scope-history entry).
    if (cur && cur.kind === 'target' && cur.id === sel.id) return
    useScope.getState().setScope(scopeFromTarget(sel.id, sel.label, 'selection-bridge'))
  }
  // Adopt a desk already selected when the shell mounted (`#sel=target:…`).
  bridge(useSelection.getState().selection)
  return useSelection.subscribe((s) => bridge(s.selection))
}

/** A journal consolidation / chronicle entry as a scope. */
export function scopeFromJournalEntry(
  entryId: string,
  label?: string,
  origin = 'navigator',
): Scope {
  return {
    kind: 'journal_entry',
    id: entryId,
    label: label?.trim() || entryId,
    members: { ...emptyMembers(), asOf: null },
    origin,
  }
}

/**
 * The Navigator's one-call convenience: project the best scope this row can
 * support WITHOUT a second request.
 *
 * An `assembly.v1` row scopes richly; anything else scopes to itself plus its
 * own desk, which is still a working scope (the feed filters, the map flies)
 * and is upgraded in place the moment the lineage walk resolves. Returning a
 * degraded scope beats returning none: a click that does nothing reads as a
 * broken link, which is the defect this whole train exists to remove.
 */
export function scopeFromRow(row: ReadFindingRow, origin = 'navigator'): Scope {
  const assembly = projectAssembly(row)
  if (assembly) return scopeFromAssembly(row, assembly, origin)
  const payload = readPayload(row)
  const asOf = typeof payload.as_of === 'string' ? payload.as_of : row.produced_at
  return {
    kind: 'report',
    id: row.id,
    label: reportLabel(row, asOf),
    members: {
      ...emptyMembers(),
      findingIds: [row.id],
      targetIds: uniq([row.target_id]),
      analystIds: uniq([row.analyst_id]),
      asOf: asOf || null,
    },
    origin,
  }
}
