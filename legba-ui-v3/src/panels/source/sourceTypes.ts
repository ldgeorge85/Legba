/**
 * UI-2 (Tier C) — shared types + helpers for the source-first surfaces.
 *
 * Mirrors the FROZEN backend REST shapes (P-05) and the source-side pydantic
 * schemas (P-08) so the four source panels stay 1:1 with the wire format:
 *
 *   - `SourceDescriptorOut`  ⇐ src/legba/data/registry/api.py::SourceDescriptorOut
 *                              (GET /registry/sources, /registry/sources/{id})
 *   - `SignalRow`/`SignalsPage` ⇐ substrate_reads_api.py (GET /signals)
 *   - `LineageReport`        ⇐ lineage_api.py (GET /lineage/{kind}/{id})
 *   - `SourceRef`/`Subscription`/`SourceSelector`
 *                            ⇐ src/legba/data/schemas/source.py
 *
 * If the backend bumps any of these, mirror it here (the api.py docstring
 * carries the same "frozen, mirror on change" contract).
 *
 * No runtime backend dependency — the panels MOCK fetch at the HTTP boundary
 * in their tests (standard component testing, per Consult.test.tsx).
 */

// ---------------------------------------------------------------------------
// Source descriptor (registry read views)
// ---------------------------------------------------------------------------

/** GET /api/v1/registry/sources[/{id}] — projected source descriptor row. */
export interface SourceDescriptorOut {
  descriptor_id: string
  version: string
  schema_uri: string
  is_head: boolean
  state: string
  owner: string
  name: string
  abstraction_level: string | null
  inherits: string[]
  created_at: string
  retire_after: string | null
  // source-specific projection
  kind: string | null
  acquisition: string | null
  subscription_policy: string | null
  owner_tenant: string | null
  geo: string[]
  languages: string[]
  tags: string[]
  has_discovery: boolean
  has_provision: boolean
  output_subject: string | null
  body: Record<string, unknown>
}

export const SOURCE_STATES = [
  'all',
  'draft',
  'configured',
  'active',
  'paused',
  'retired',
] as const
export type SourceStateFilter = (typeof SOURCE_STATES)[number]

/** Lifecycle FSM transitions the source registry exposes (api.py TransitionBody).
 *  `retired` is intentionally excluded — that goes through POST /retire. */
export const FORWARD_TRANSITIONS: Record<string, string[]> = {
  draft: ['configured'],
  configured: ['active', 'draft'],
  active: ['paused'],
  paused: ['active'],
}

// ---------------------------------------------------------------------------
// Signals (substrate reads) — fan-out preview + provenance roots
// ---------------------------------------------------------------------------

/** One row of public.signals (GET /api/v1/signals).
 *
 * Signals are TARGET-AGNOSTIC now: the row keeps a nullable `target_id` column
 * for back-compat, but it is always `null` — routing happens per-target at read
 * time (target_id filters by the target's scope.geo, not by a column on the
 * row). Do NOT group/route by `target_id`; use the indexed facets instead:
 *   - top-level `geo` / `tags` / `entity_classes` are the precomputed coarse
 *     slice the subscription router matches on;
 *   - `data.geo` is the *geocoded* object ({lat,lon,country,country_iso2,…}),
 *     NOT an array — distinct from the top-level `geo: string[]`. */
export interface SignalRow {
  id: string
  data: Record<string, unknown>
  title: string
  source_id: string | null
  source_url: string
  guid: string
  category: string
  event_timestamp: string | null
  language: string
  confidence: number
  classification_scores: Record<string, unknown> | null
  /** Always null — signals are target-agnostic. Kept for wire back-compat. */
  target_id: string | null
  analyst_id: string | null
  produced_at: string
  derived_from: string[]
  schema_uri: string
  descriptor_source_id: string
  // precomputed coarse facets (top-level arrays on the row)
  geo: string[]
  tags: string[]
  entity_classes: string[]
}

/** Geocode object carried in `signal.data.geo` when the signal was geocoded. */
export interface SignalGeo {
  lat?: number
  lon?: number
  country?: string
  country_iso2?: string
  country_iso3?: string
  precision?: string
  source?: string
}

/** Read `signal.data.geo` as the geocode object (or null when absent/!geocoded). */
export function signalGeo(sig: SignalRow): SignalGeo | null {
  const g = (sig.data as Record<string, unknown> | undefined)?.geo
  return g && typeof g === 'object' && !Array.isArray(g) ? (g as SignalGeo) : null
}

export interface SignalsPage {
  data: SignalRow[]
  next_cursor: string | null
}

/** One row of public.findings (GET /api/v1/findings). The fan-out's hop-2:
 *  a finding's `derived_from` lists the SIGNAL ids it was synthesised from, so
 *  joining findings.derived_from ⊇ {signal_id} reconstructs source→signal→
 *  finding provenance without the (currently 500-ing) /lineage endpoint. */
export interface FindingRow {
  id: string
  title: string
  body: string
  severity: string | null
  confidence: number | null
  data: Record<string, unknown>
  target_id: string | null
  analyst_id: string | null
  derived_from: string[]
  produced_at: string
}

export interface FindingsPage {
  data: FindingRow[]
  next_cursor: string | null
}

// ---------------------------------------------------------------------------
// Lineage (provenance walk) — fan-out explorer
// ---------------------------------------------------------------------------

export interface LineageNode {
  id: string
  row_kind: string
  title: string | null
  produced_at: string
  target_id: string | null
  analyst_id: string | null
  schema_uri: string
  depth: number
}

export interface LineageEdge {
  parent: string
  child: string
}

export interface LineageReport {
  root: LineageNode
  nodes: LineageNode[]
  edges: LineageEdge[]
  truncated_at_depth: boolean
}

// ---------------------------------------------------------------------------
// SourceRef / Subscription / SourceSelector (the target-side contract)
// ---------------------------------------------------------------------------

/** Signal-level slice. Mirrors source.py::Subscription. */
export interface Subscription {
  geo: string[]
  languages: string[]
  tags: string[]
  entity_classes: string[]
  modalities: string[]
  predicate: string | null
  canonical_only: boolean
}

/** Coarse query over SOURCE scope. Mirrors source.py::SourceSelector. */
export interface SourceSelector {
  tags: string[]
  geo: string[]
  languages: string[]
  owner_tenant: string | null
  kinds: string[]
  predicate: string | null
}

/** A target's reference to source(s). Mirrors source.py::SourceRef.
 *  Exactly one of source_id / source_selector is set. */
export interface SourceRef {
  source_id: string | null
  source_selector: SourceSelector | null
  subscription: Subscription
}

export function emptySubscription(): Subscription {
  return {
    geo: [],
    languages: [],
    tags: [],
    entity_classes: [],
    modalities: [],
    predicate: null,
    canonical_only: true,
  }
}

export function emptySelector(): SourceSelector {
  return {
    tags: [],
    geo: [],
    languages: [],
    owner_tenant: null,
    kinds: [],
    predicate: null,
  }
}

// ---------------------------------------------------------------------------
// Client-side validators (no backend predicate-validate endpoint is frozen,
// so the subscription builder does first-pass structural validation here;
// the registry re-validates authoritatively on save). These mirror the
// pattern constraints from the pydantic schemas.
// ---------------------------------------------------------------------------

const GEO_RE = /^[A-Z]{2,3}$/
const TAG_RE = /^[a-z][a-z0-9_]*$/
const LANG_RE = /^[a-z]{2}(-[A-Z]{2})?$/
const SOURCE_ID_RE = /^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$/

export interface FieldIssue {
  field: string
  value: string
  message: string
}

/** Validate a list of tokens against a regex; returns one issue per bad token. */
function lintTokens(
  field: string,
  tokens: string[],
  re: RegExp,
  hint: string,
): FieldIssue[] {
  const out: FieldIssue[] = []
  for (const t of tokens) {
    if (!re.test(t)) out.push({ field, value: t, message: hint })
  }
  return out
}

/**
 * Lightweight Starlark-residual linter. The authoritative compile happens
 * server-side (legba.data.predicates.compile_predicate); this catches the
 * common foot-guns *before* a round-trip and explains the residual surface:
 *   - balanced parens / brackets
 *   - no statements (`=` assignment, `def`, `import`, `while`, `;`)
 *   - non-empty
 * Returns null when the residual is structurally plausible.
 */
export function lintPredicate(src: string | null | undefined): string | null {
  if (src == null) return null
  const s = src.trim()
  if (s === '') return null // empty == no residual, valid
  // Single-expression-only surface (per predicates/__init__.py §3).
  if (/(^|[^=!<>])=(?!=)/.test(s)) {
    return 'assignment ("=") not allowed — a predicate is a single boolean expression'
  }
  if (/\b(def|import|lambda|while|for|return|load)\b/.test(s)) {
    return 'statements / keywords (def, import, while, for, return, load) are not allowed'
  }
  if (s.includes(';')) return 'multiple statements (";") not allowed — single expression only'
  // Balanced brackets.
  const pairs: Record<string, string> = { ')': '(', ']': '[', '}': '{' }
  const stack: string[] = []
  for (const ch of s) {
    if (ch === '(' || ch === '[' || ch === '{') stack.push(ch)
    else if (ch in pairs) {
      if (stack.pop() !== pairs[ch]) return `unbalanced "${ch}"`
    }
  }
  if (stack.length > 0) return `unclosed "${stack[stack.length - 1]}"`
  return null
}

/** Validate a whole Subscription's structured fields + residual. */
export function lintSubscription(sub: Subscription): FieldIssue[] {
  const issues: FieldIssue[] = []
  issues.push(...lintTokens('geo', sub.geo, GEO_RE, 'geo must be a 2–3 letter UPPERCASE ISO code (e.g. BR, USA)'))
  issues.push(...lintTokens('languages', sub.languages, LANG_RE, 'language must be like "en" or "pt-BR"'))
  issues.push(...lintTokens('tags', sub.tags, TAG_RE, 'tag must be snake_case (start a–z)'))
  issues.push(...lintTokens('entity_classes', sub.entity_classes, TAG_RE, 'entity_class must be snake_case'))
  const pred = lintPredicate(sub.predicate)
  if (pred) issues.push({ field: 'predicate', value: sub.predicate ?? '', message: pred })
  return issues
}

/** Validate a SourceSelector's structured fields + residual. */
export function lintSelector(sel: SourceSelector): FieldIssue[] {
  const issues: FieldIssue[] = []
  issues.push(...lintTokens('tags', sel.tags, TAG_RE, 'tag must be snake_case (start a–z)'))
  issues.push(...lintTokens('geo', sel.geo, GEO_RE, 'geo must be a 2–3 letter UPPERCASE ISO code'))
  issues.push(...lintTokens('languages', sel.languages, LANG_RE, 'language must be like "en" or "pt-BR"'))
  const pred = lintPredicate(sel.predicate)
  if (pred) issues.push({ field: 'predicate', value: sel.predicate ?? '', message: pred })
  return issues
}

/** Validate an explicit source_id token. */
export function lintSourceId(id: string): string | null {
  if (!id.trim()) return 'source_id required when ref mode is "explicit"'
  return SOURCE_ID_RE.test(id.trim())
    ? null
    : 'source_id must be like "source.rss.brazil" (snake_case, dot-separated)'
}

/** Build the final SourceRef JSON to embed in a target descriptor. Sets
 *  exactly one of source_id / source_selector per the pydantic constraint. */
export function buildSourceRef(
  refMode: 'explicit' | 'selector',
  sourceId: string,
  selector: SourceSelector,
  sub: Subscription,
): SourceRef {
  return {
    source_id: refMode === 'explicit' ? sourceId.trim() : null,
    source_selector: refMode === 'selector' ? pruneSelector(selector) : null,
    subscription: pruneSubscription(sub),
  }
}

/** Drop empty/null fields so the emitted JSON is tidy (the schema defaults
 *  them anyway, but a clean ref is friendlier to paste into a descriptor). */
function pruneSubscription(sub: Subscription): Subscription {
  return {
    ...sub,
    predicate: sub.predicate && sub.predicate.trim() ? sub.predicate.trim() : null,
  }
}

function pruneSelector(sel: SourceSelector): SourceSelector {
  return {
    ...sel,
    owner_tenant: sel.owner_tenant && sel.owner_tenant.trim() ? sel.owner_tenant.trim() : null,
    predicate: sel.predicate && sel.predicate.trim() ? sel.predicate.trim() : null,
  }
}

/**
 * Unwrap a property-factory value. The registry stores config/cadence fields
 * as property-factory wrappers — `{ raw, ui_hint, factory_kind }` — so a body's
 * `cadence.schedule` is an OBJECT on the wire, not the bare string the schema
 * doc implies. This returns the human value (`.raw`) for either shape. */
export function unwrapFactory(v: unknown): string | null {
  if (v == null) return null
  if (typeof v === 'string') return v
  if (typeof v === 'object' && 'raw' in (v as Record<string, unknown>)) {
    const raw = (v as Record<string, unknown>).raw
    return raw == null ? null : String(raw)
  }
  return null
}

/** Parse a comma/space/newline-separated token field into a trimmed array. */
export function parseTokens(raw: string): string[] {
  return raw
    .split(/[\s,]+/)
    .map((t) => t.trim())
    .filter(Boolean)
}

// ---------------------------------------------------------------------------
// Starter descriptor — "less raw" registries (UI_ROADMAP §2). Clone-and-edit
// seed for a new source, pre-filled with a working RSS poll source. Handed to
// the DescriptorEditor as initialBody when the operator clicks "+ new source".
// ---------------------------------------------------------------------------

export function starterSourceDescriptor(owner: string): Record<string, unknown> {
  const now = new Date().toISOString()
  return {
    identity: {
      id: 'source.rss.example',
      name: 'Example RSS source',
      kind: 'rss',
      schema_uri: 'legba/source/1.0.0',
      version: '0'.repeat(16), // registry stamps the real content hash
      abstraction_level: 'L1',
      state: 'draft',
      owner,
      created: now,
    },
    scope: {
      owner_tenant: 'default',
      geo: ['BR'],
      languages: ['pt'],
      tags: ['news', 'osint'],
    },
    acquisition: 'poll',
    cadence: { schedule: '*/15 * * * *', cooldown_seconds: 0, jitter_seconds: 30 },
    output: {
      subject_prefix: '',
      retention: 'interest',
      max_age_seconds: 86400,
      delivery: 'lossy',
    },
    subscription_policy: 'open',
    config: { feed_url: 'https://example.com/rss.xml' },
  }
}
