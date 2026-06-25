/**
 * D3 (Tier C) — shared types + helpers for the discovery-pipeline surface.
 *
 * Discovery is the source-first pivot's "materialise candidates" half: a
 * descriptor carrying a `discovery` block (an L2/L3 TARGET or SOURCE template)
 * seeds N candidates per cycle via a discovery kind (list / crawl / query),
 * rewrites each candidate's raw labels through the relabel chain, and the
 * registry-side materialiser writes the merged body into the substrate as an
 * L1 instance — with a validate-before-register gate on the source side.
 *
 * There is NO bespoke discovery REST surface: everything the panel renders is
 * lifted from the FROZEN generic descriptor read views (P-05):
 *
 *   - discovery descriptors  ⇐ GET /registry/descriptors?family=target
 *                               + GET /registry/sources  (rows whose body
 *                               carries a `discovery` block / has_discovery)
 *   - candidate pipeline      ⇐ GET /registry/descriptors?family=<fam>
 *                               (the materialised L1 children: a child carries
 *                               the discovery descriptor's id in `inherits`,
 *                               appended by both materialisers —
 *                               discovered_materializer.py L443 /
 *                               source_materializer.py L214)
 *   - rejected candidates     ⇐ GET /registry/dead_letter  (source candidates
 *                               that failed validate-before-register, plus
 *                               target candidates dropped by relabel/validation)
 *
 * Pipeline-stage ⇐ descriptor-state mapping (both materialisers force a fresh
 * child to `state=draft`; the runtime promotes it as it proves out):
 *
 *   proposed   ⇐ draft            (just materialised, not yet proven)
 *   validated  ⇐ configured       (passed validate-before-register)
 *   registered ⇐ active | paused  (live / temporarily paused, still registered)
 *   rejected   ⇐ retired          (disappeared this cycle, or relabel-dropped)
 *            + DLQ rows           (validate-before-register failures — never
 *                                  written to the substrate)
 *
 * Mirrors `DescriptorRowOut` / `DLQEntryOut` from
 * src/legba/data/registry/api.py — if the backend bumps those, mirror here.
 * The panel MOCKS fetch at the HTTP boundary in its test (no live backend).
 */

import type { ScopeFamily } from '@/components/ScopePicker'

// ---------------------------------------------------------------------------
// Wire shapes (mirror the registry REST read views)
// ---------------------------------------------------------------------------

/** GET /api/v1/registry/descriptors[/{family}/{id}] — generic descriptor row.
 *  Mirrors api.py::DescriptorRowOut. */
export interface DescriptorRowOut {
  descriptor_id: string
  version: string
  schema_uri: string
  is_head: boolean
  state: string
  owner: string
  name: string
  family: string
  body: Record<string, unknown>
  created_at: string
  abstraction_level: string | null
  inherits: string[]
  retire_after: string | null
  kind: string | null
  type_signature: Record<string, unknown> | null
}

/** GET /api/v1/registry/dead_letter — mirrors api.py::DLQEntryOut. */
export interface DLQEntryOut {
  id: string
  attempted_at: string
  actor: string
  namespace: string
  declared_schema_uri: string | null
  validation_error: Record<string, unknown>
  resolution: string | null
  attempted_payload: Record<string, unknown> | null
}

// ---------------------------------------------------------------------------
// Discovery descriptor (lifted view)
// ---------------------------------------------------------------------------

/** The `discovery` sub-block on a TARGET/SOURCE template body. Mirrors
 *  schemas/target.py::DiscoveryBlock (source.py mirrors the same shape). */
export interface DiscoveryBlock {
  kind: string
  list_source?: string
  emit_per_match?: boolean
  relabel?: unknown[]
  resync_policy?: Record<string, unknown> | null
  config?: Record<string, unknown>
}

/** A discovery descriptor lifted from a DescriptorRowOut whose body carries a
 *  `discovery` block. `family` distinguishes target- vs source-discovery. */
export interface DiscoveryDescriptor {
  descriptorId: string
  version: string
  name: string
  state: string
  family: 'target' | 'source'
  abstractionLevel: string | null
  /** L2 template this discovery inherits + merges candidates against. */
  inheritsTemplate: string | null
  block: DiscoveryBlock
  body: Record<string, unknown>
}

export function readDiscoveryBlock(
  body: Record<string, unknown>,
): DiscoveryBlock | null {
  const d = body?.discovery
  if (d && typeof d === 'object' && !Array.isArray(d) && 'kind' in d) {
    return d as DiscoveryBlock
  }
  return null
}

/** Lift a raw descriptor row into a DiscoveryDescriptor, or null if the row
 *  carries no discovery block. */
export function liftDiscovery(
  row: DescriptorRowOut,
  family: 'target' | 'source',
): DiscoveryDescriptor | null {
  const block = readDiscoveryBlock(row.body)
  if (!block) return null
  return {
    descriptorId: row.descriptor_id,
    version: row.version,
    name: row.name,
    state: row.state,
    family,
    abstractionLevel: row.abstraction_level,
    inheritsTemplate: row.inherits.length > 0 ? row.inherits[0] : null,
    block,
    body: row.body,
  }
}

// ---------------------------------------------------------------------------
// Candidate pipeline
// ---------------------------------------------------------------------------

export type PipelineStage = 'proposed' | 'validated' | 'registered' | 'rejected'

export const PIPELINE_STAGES: readonly PipelineStage[] = [
  'proposed',
  'validated',
  'registered',
  'rejected',
] as const

export const STAGE_LABEL: Record<PipelineStage, string> = {
  proposed: 'proposed',
  validated: 'validated',
  registered: 'registered',
  rejected: 'rejected',
}

/** Map a materialised L1 child's descriptor state onto a pipeline stage.
 *  Both materialisers stamp a fresh child `draft`; the runtime promotes. */
export function stageForState(state: string): PipelineStage {
  switch (state) {
    case 'draft':
      return 'proposed'
    case 'configured':
      return 'validated'
    case 'active':
    case 'paused':
      return 'registered'
    case 'retired':
    default:
      return 'rejected'
  }
}

export function stageClass(stage: PipelineStage): string {
  switch (stage) {
    case 'proposed':
      return 'bg-sky-900 text-sky-200'
    case 'validated':
      return 'bg-amber-900 text-amber-200'
    case 'registered':
      return 'bg-emerald-900 text-emerald-200'
    case 'rejected':
      return 'bg-rose-950 text-rose-300'
  }
}

/** One materialised candidate (an L1 child of a discovery descriptor). */
export interface Candidate {
  descriptorId: string
  name: string
  state: string
  stage: PipelineStage
  family: 'target' | 'source'
  /** The candidate's stable natural key (iso2 in scope.geo[0], or the host). */
  naturalKey: string | null
  /** Every discovery descriptor id this child inherits (usually one). */
  discoveryParents: string[]
  inherits: string[]
  createdAt: string
}

/** Read the natural key the materialiser uses for disappearance tracking:
 *  `_discovery_natural_key` if set, else scope.geo[0]
 *  (discovered_materializer.py::_prior_active_keys). */
export function readNaturalKey(body: Record<string, unknown>): string | null {
  const explicit = body?._discovery_natural_key
  if (typeof explicit === 'string' && explicit) return explicit
  const scope = body?.scope
  if (scope && typeof scope === 'object') {
    const geo = (scope as Record<string, unknown>).geo
    if (Array.isArray(geo) && geo.length > 0) return String(geo[0])
  }
  return null
}

/**
 * Build the candidate list for a set of discovery descriptors out of the full
 * descriptor rows of a family. A row is a candidate of discovery D iff its
 * `inherits` array contains D's descriptor_id — exactly the link both
 * materialisers write. Discovery descriptors themselves are excluded.
 */
export function buildCandidates(
  rows: DescriptorRowOut[],
  discoveryIds: ReadonlySet<string>,
  family: 'target' | 'source',
): Candidate[] {
  const out: Candidate[] = []
  for (const row of rows) {
    if (discoveryIds.has(row.descriptor_id)) continue // the discovery itself
    const parents = row.inherits.filter((id) => discoveryIds.has(id))
    if (parents.length === 0) continue
    out.push({
      descriptorId: row.descriptor_id,
      name: row.name,
      state: row.state,
      stage: stageForState(row.state),
      family,
      naturalKey: readNaturalKey(row.body),
      discoveryParents: parents,
      inherits: row.inherits,
      createdAt: row.created_at,
    })
  }
  return out
}

/** Group candidates by pipeline stage, preserving the canonical stage order. */
export function groupByStage(
  candidates: Candidate[],
): Record<PipelineStage, Candidate[]> {
  const out: Record<PipelineStage, Candidate[]> = {
    proposed: [],
    validated: [],
    registered: [],
    rejected: [],
  }
  for (const c of candidates) out[c.stage].push(c)
  return out
}

/** Heuristic: does this DLQ entry look like a discovery validate/relabel
 *  rejection? The materialisers namespace these payloads and tag the actor /
 *  resolution with "discovery" / "source_discovery". */
export function isDiscoveryRejection(entry: DLQEntryOut): boolean {
  const hay = `${entry.actor} ${entry.namespace} ${entry.resolution ?? ''}`.toLowerCase()
  if (hay.includes('discovery')) return true
  const payload = entry.attempted_payload ?? {}
  return (
    '_discovery_natural_key' in payload ||
    'discovery' in payload ||
    'natural_key' in payload
  )
}

/** Discovery descriptors live in two families. The picker filters by either. */
export const DISCOVERY_FAMILIES: readonly Extract<ScopeFamily, 'target' | 'source'>[] = [
  'target',
  'source',
] as const

export type FamilyFilter = 'all' | 'target' | 'source'
