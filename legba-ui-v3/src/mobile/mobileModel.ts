/**
 * mobileModel — the pure read-model layer behind the phone surface.
 *
 * THE CROSSROADS RULE (UI_V2 §"Selection Store"): one click filters every
 * panel. On a phone there are no panels, so the same rule lands as: one
 * selected report is the context for every section below it, and one selected
 * entity narrows all three sections at once. Everything on this page is
 * therefore derived from ONE selected row — never independently fetched, never
 * independently filtered.
 *
 * ── WHY NO EXTRA ENDPOINTS ────────────────────────────────────────────────
 * The `assembly.v1` payload already carries its own lineage. Each block IS a
 * cited head (`finding_id`, `desk`, `target_id`, `severity`, `verify`) and
 * carries the signals it cited (`marker`, `signal_id`, `title`, `url`,
 * `age_h`). So the Findings and Signals sections are PROJECTIONS of the report
 * the operator already downloaded, not second round trips. That is why opening
 * a report on a phone costs exactly one request, which is the whole point on a
 * cellular connection.
 *
 * The one thing the payload does NOT carry is a substrate entity list — see
 * `reportEntities`, which is honest about deriving from the blocks' targets
 * rather than inventing an entity resolution the API cannot do (there is no
 * server-side filter from a read to its entities; documented in the build
 * report, deliberately not stubbed).
 *
 * Everything here is pure and DOM-free so the selection semantics can be
 * tested without mounting React.
 */

import type {
  AssemblyBlock,
  AssemblyPayload,
  AssemblySignal,
  ReadFindingRow,
} from '@/lib/assemblyModel'

// ── the read producers ───────────────────────────────────────────────────────

/**
 * The analyst ids that produce reads, one per tier.
 *
 * These are the substrate's own producer ids (`world_assessor` per
 * `standing_auditor.WORLD_ANALYST_ID`; the three `*_composition` ids per
 * `composition_slice`; `world_assessment` per the assessment channel). They
 * are the ONLY tier discriminator the findings row carries — there is no
 * `tier` column on `/findings`, so the analyst id is what tells a country read
 * from a world read.
 */
export const READ_ANALYSTS = {
  world: 'world_assessor',
  assessment: 'world_assessment',
  region: 'region_composition',
  country: 'country_composition',
  thematic: 'escalation_composition',
} as const

export type ReportTier = keyof typeof READ_ANALYSTS

/** Display order — the way the operator reads down the page, widest first. */
export const TIER_ORDER: ReportTier[] = ['world', 'assessment', 'region', 'country', 'thematic']

export const TIER_LABEL: Record<ReportTier, string> = {
  world: 'World',
  assessment: 'Assessment',
  region: 'Region',
  country: 'Country',
  thematic: 'Thematic',
}

/** The CSV the Reports list sends as `analyst_id_in` — one request, all tiers. */
export const READ_ANALYST_CSV = TIER_ORDER.map((t) => READ_ANALYSTS[t]).join(',')

/** Reverse map, resolved once. */
const TIER_BY_ANALYST = new Map<string, ReportTier>(
  TIER_ORDER.map((t) => [READ_ANALYSTS[t], t] as const),
)

export function tierOf(row: ReadFindingRow): ReportTier | null {
  const id = row.analyst_id
  return id ? (TIER_BY_ANALYST.get(id) ?? null) : null
}

// ── the list row ─────────────────────────────────────────────────────────────

/**
 * One row of the Reports navigator.
 *
 * `leadThread` is the sentence the operator scans; `verifyScore`/`verifyState`
 * are read straight off the row's verification block (never recomputed — the
 * Morning Read's rule that the reader does not do arithmetic on a disclosure).
 */
export interface ReportRow {
  id: string
  tier: ReportTier
  /** The read's subject: a country/region name where the row names one. */
  target: string
  targetId: string | null
  /** ISO produced_at, verbatim off the row. */
  producedAt: string
  /** UTC calendar day, `YYYY-MM-DD` — the date-navigation key. */
  day: string
  title: string
  /** The one-line thread; empty string when the row carries none. */
  leadThread: string
  verifyScore: number | null
  verifyState: string | null
  /** Which region a country read belongs under, when derivable. */
  regionHint: string | null
}

function verification(row: ReadFindingRow): Record<string, unknown> | null {
  const v = row.verification
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function numOrNull(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** `YYYY-MM-DD` in UTC. Tolerates the substrate's space-separated stamps. */
export function dayKey(iso: string | null | undefined): string {
  if (!iso) return ''
  const normalized = iso.includes('T') ? iso : iso.replace(' ', 'T')
  const d = new Date(normalized)
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10)
  return d.toISOString().slice(0, 10)
}

/**
 * The lead thread — the sentence the list row shows.
 *
 * Preference order, each a REAL field on the record rather than a summary we
 * compose: the lead block's BLUF span (the record's own published lead), then
 * any first BLUF span, then the assessment body's first sentence, then the
 * title. Never fabricated: an empty string when the row genuinely carries no
 * prose, and the list renders the honest dash.
 */
export function leadThread(row: ReadFindingRow, assembly: AssemblyPayload | null): string {
  if (assembly) {
    const leadOrdinals = assembly.lead?.block_ordinals ?? []
    const ordered = [
      ...assembly.blocks.filter((b) => leadOrdinals.includes(b.ordinal)),
      ...assembly.blocks.filter((b) => !leadOrdinals.includes(b.ordinal)),
    ]
    for (const b of ordered) {
      const bluf = b.spans.find((s) => s.role === 'bluf') ?? b.spans[0]
      if (bluf?.text) return bluf.text.trim()
    }
  }
  const body = typeof row.body === 'string' ? row.body.trim() : ''
  if (body) return bodyLead(body)
  return ''
}

/**
 * The lead sentence of a markdown body (the assessment channel's shape).
 *
 * The body opens with an italic as-of slice, then a `**BLUF:**` line, then
 * `##` sections. The thread the operator wants is the BLUF — which a naive
 * "first line that isn't italic" rule SKIPS, because `**BLUF:**` also starts
 * with an asterisk, landing on the `## The picture` heading instead. So the
 * BLUF is sought explicitly, headings are never eligible, and the emphasis
 * markers are stripped so a navigator row shows prose rather than syntax.
 */
function bodyLead(body: string): string {
  const lines = body.split('\n').map((l) => l.trim())
  const isAsOf = (l: string) => /^\*[^*].*\*$/.test(l)
  const isHeading = (l: string) => l.startsWith('#')
  const strip = (l: string) => l.replace(/\*\*/g, '').replace(/^BLUF:\s*/i, '').trim()

  const bluf = lines.find((l) => /^\*\*BLUF:?\*\*/i.test(l))
  if (bluf) return strip(bluf)

  const first = lines.find((l) => l && !isAsOf(l) && !isHeading(l))
  return first ? strip(first) : strip(lines[0] ?? '')
}

/**
 * Project a `/findings` row into a navigator row.
 *
 * Returns `null` for a row produced by an analyst outside the read set, so a
 * widened query can never smuggle a non-read into the Reports list.
 */
export function toReportRow(
  row: ReadFindingRow,
  assembly: AssemblyPayload | null = null,
): ReportRow | null {
  const tier = tierOf(row)
  if (!tier) return null
  const ver = verification(row)
  const blocks = assembly?.blocks ?? []
  const target =
    blocks.find((b) => b.target_name)?.target_name ??
    row.target_id ??
    (tier === 'world' || tier === 'assessment' ? 'World' : TIER_LABEL[tier])
  return {
    id: row.id,
    tier,
    target,
    targetId: row.target_id ?? blocks.find((b) => b.target_id)?.target_id ?? null,
    producedAt: row.produced_at,
    day: dayKey(row.produced_at),
    title: row.title ?? '',
    leadThread: leadThread(row, assembly),
    verifyScore: numOrNull(ver?.overall_score),
    verifyState: typeof ver?.score_state === 'string' ? (ver.score_state as string) : null,
    regionHint: null,
  }
}

// ── grouping + date navigation ───────────────────────────────────────────────

export interface TierGroup {
  tier: ReportTier
  label: string
  rows: ReportRow[]
}

/**
 * Group the navigator by tier, newest first inside each.
 *
 * Empty tiers are DROPPED rather than rendered as empty headings: a phone
 * screen has no room to spend on a heading that says nothing, and an absent
 * tier is already visible as an absence in the date strip.
 */
export function groupByTier(rows: ReportRow[]): TierGroup[] {
  const out: TierGroup[] = []
  for (const tier of TIER_ORDER) {
    const inTier = rows
      .filter((r) => r.tier === tier)
      .sort((a, b) => b.producedAt.localeCompare(a.producedAt))
    if (inTier.length > 0) out.push({ tier, label: TIER_LABEL[tier], rows: inTier })
  }
  return out
}

/** Distinct UTC days present in the rows, newest first — the date strip. */
export function daysPresent(rows: ReportRow[]): string[] {
  return [...new Set(rows.map((r) => r.day).filter(Boolean))].sort((a, b) => b.localeCompare(a))
}

export function rowsForDay(rows: ReportRow[], day: string | null): ReportRow[] {
  if (!day) return rows
  return rows.filter((r) => r.day === day)
}

// ── the sections under a selected report ─────────────────────────────────────

/** A cited head, projected from the block that quotes it. */
export interface ReportFinding {
  ordinal: number
  findingId: string
  desk: string
  targetId: string | null
  targetName: string | null
  question: string
  severity: string | null
  verifyScore: number | null
  /** The block's own BLUF, the sentence that head contributed. */
  bluf: string
  signalCount: number
}

export function reportFindings(a: AssemblyPayload | null): ReportFinding[] {
  if (!a) return []
  return a.blocks.map((b: AssemblyBlock) => ({
    ordinal: b.ordinal,
    findingId: b.finding_id,
    desk: b.desk,
    targetId: b.target_id,
    targetName: b.target_name,
    question: b.question,
    severity: b.severity,
    verifyScore: b.verify?.overall_score ?? null,
    bluf: (b.spans.find((s) => s.role === 'bluf') ?? b.spans[0])?.text?.trim() ?? '',
    signalCount: b.signals.length,
  }))
}

/** A cited signal, deduped across the blocks that share it. */
export interface ReportSignal {
  signalId: string | null
  marker: string
  title: string
  url: string | null
  sourceId: string | null
  ageH: number | null
  /** Which on-page blocks cite it — the "one selection, everything follows" link. */
  ordinals: number[]
  targetIds: string[]
}

/**
 * Every distinct signal the report cites.
 *
 * Deduped on `signal_id` where present; a signal with no id (the substrate
 * allows one) is kept under its marker rather than dropped, because losing a
 * cited source silently is worse than showing one twice.
 */
export function reportSignals(a: AssemblyPayload | null): ReportSignal[] {
  if (!a) return []
  const by = new Map<string, ReportSignal>()
  for (const b of a.blocks) {
    for (const s of b.signals as AssemblySignal[]) {
      const key = s.signal_id ?? `marker:${b.ordinal}:${s.marker}`
      const found = by.get(key)
      if (found) {
        if (!found.ordinals.includes(b.ordinal)) found.ordinals.push(b.ordinal)
        if (b.target_id && !found.targetIds.includes(b.target_id)) found.targetIds.push(b.target_id)
        continue
      }
      by.set(key, {
        signalId: s.signal_id,
        marker: s.marker,
        title: s.title,
        url: s.url,
        sourceId: s.source_id,
        ageH: s.age_h,
        ordinals: [b.ordinal],
        targetIds: b.target_id ? [b.target_id] : [],
      })
    }
  }
  return [...by.values()].sort(
    (x, y) => y.ordinals.length - x.ordinals.length || x.marker.localeCompare(y.marker),
  )
}

/**
 * The entities the report names.
 *
 * HONEST SCOPE — read this before extending it. `/api/v1/entities` takes only
 * `q`, `entity_class` and `limit`: there is NO filter from a read (or from a
 * signal id, or a geo box) to the entities it names, so a true "entities in
 * this report" list cannot be served today. What the record DOES carry, as
 * first-class data, is the target each block speaks about. Those targets are
 * what this returns — labelled as targets in the UI, never dressed up as a
 * resolved entity list. The missing filter is named precisely in the build
 * report rather than stubbed here.
 */
export interface ReportEntity {
  targetId: string
  name: string
  /** Blocks that speak about it — tapping filters findings + signals to these. */
  ordinals: number[]
  severity: string | null
}

export function reportEntities(a: AssemblyPayload | null): ReportEntity[] {
  if (!a) return []
  const by = new Map<string, ReportEntity>()
  for (const b of a.blocks) {
    if (!b.target_id) continue
    const found = by.get(b.target_id)
    if (found) {
      if (!found.ordinals.includes(b.ordinal)) found.ordinals.push(b.ordinal)
      continue
    }
    by.set(b.target_id, {
      targetId: b.target_id,
      name: b.target_name ?? b.target_id,
      ordinals: [b.ordinal],
      severity: b.severity,
    })
  }
  return [...by.values()].sort(
    (x, y) => y.ordinals.length - x.ordinals.length || x.name.localeCompare(y.name),
  )
}

// ── the selection filter (the Crossroads rule) ───────────────────────────────

/**
 * Narrow a report's three sections to one selected entity.
 *
 * This is the whole Selection Store idea in one function: the entity names a
 * set of block ordinals, and every section is filtered to that set. Passing
 * `null` restores the full report — selection is always reversible.
 */
export function filterToEntity(
  entityId: string | null,
  findings: ReportFinding[],
  signals: ReportSignal[],
  entities: ReportEntity[],
): { findings: ReportFinding[]; signals: ReportSignal[] } {
  if (!entityId) return { findings, signals }
  const entity = entities.find((e) => e.targetId === entityId)
  if (!entity) return { findings, signals }
  const ords = new Set(entity.ordinals)
  return {
    findings: findings.filter((f) => ords.has(f.ordinal)),
    signals: signals.filter((s) => s.ordinals.some((o) => ords.has(o))),
  }
}

// ── where (the map's cheap honest form) ──────────────────────────────────────

export interface WherePoint {
  targetId: string
  name: string
  severity: string | null
}

/** The report's geography, as the targets it actually speaks about. */
export function reportWhere(a: AssemblyPayload | null): WherePoint[] {
  return reportEntities(a).map((e) => ({
    targetId: e.targetId,
    name: e.name,
    severity: e.severity,
  }))
}
